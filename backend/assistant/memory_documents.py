"""Uploaded parsed-text chunks with their current immutable document authority.

Index hits select candidates. They never authorize a chunk or turn an uploaded
file into a personal statement. No blob, parser, index or background IO runs here.
"""
import json

from sqlalchemy import select

from assistant.commands import command_digest
from assistant.knowledge import _iso, _source_identities
from db.models.memory_v2 import MemorySource
from memory import service
from memory.documents.authority import current_revision
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.redaction import redact_credential_ranges, redact_credentials, text_hash
from memory.wiki.service import WikiStateError


async def rows(db, scope, ids):
    if not ids:
        return []
    return list((await db.scalars(select(MemorySource).where(*scope.predicates(MemorySource),
        MemorySource.id.in_(ids), MemorySource.source_kind == "document_chunk"))).all())


def _context(db, document, revision):
    # Only immutable text/projection work is reused within this SQL snapshot.
    # Current source/body/ACL checks still run for every selected chunk.
    key = (document.id, document.revision, document.content_hash)
    cache = db.info.setdefault("assistant_document_text", {})
    if key not in cache:
        sections = revision.sections
        if (not isinstance(sections, list) or not 1 <= len(sections) <= 128
                or any(not isinstance(s, dict) or not isinstance(s.get("body"), str)
                       or not s["body"] or type(s.get("start")) is not int or s["start"] < 0 for s in sections)
                or sum(len(s["body"]) for s in sections) > 1_000_000):
            raise ValueError("Invalid document sections")
        text = "".join(s["body"] for s in sections)
        cache[key] = (text, redact_credentials(text) == text)
    return cache[key]


async def current(db, scope, main_id, source, local_scopes):
    if source is None or source.source_kind != "document_chunk" or any((source.session_id, source.message_id, source.part_id)):
        return None
    try:
        if source.project_id not in local_scopes:
            local_scopes[source.project_id] = await resolve_access_scope(db, user_id=scope.user_id,
                workspace_id=scope.workspace_id, project_id=source.project_id)
        local = local_scopes[source.project_id]
        if not source.body or not await service.source_body_is_available(db, local, source):
            return None
        metadata = source.source_metadata
        document, revision = await current_revision(db, local, metadata["document_id"])
        if (document.project_id != source.project_id or revision.origin not in {"upload", "user_edit"}
                or not isinstance(document.source_ids, list) or not 1 <= len(document.source_ids) <= 5000
                or len(set(document.source_ids)) != len(document.source_ids)
                or not isinstance(document.page_ids, list) or len(document.page_ids) != len(revision.sections)
                or len(set(document.page_ids)) != len(document.page_ids)
                or metadata.get("filename") != document.filename):
            return None
        text, unchanged = _context(db, document, revision)
        index, start, end = metadata.get("section"), metadata.get("start"), metadata.get("end")
        if (type(index) is not int or not 0 <= index < len(revision.sections)
                or type(start) is not int or type(end) is not int):
            return None
        section = revision.sections[index]
        local_start, local_end = start - section["start"], end - section["start"]
        if (not 0 <= local_start < local_end <= len(section["body"])
                or source.body != section["body"][local_start:local_end]
                or metadata.get("page_id") != document.page_ids[index]
                or metadata.get("edited") is not bool(section.get("edited", False))):
            return None
        # Edited sections retain their original parser offsets. Derive the
        # current parsed-text range from actual preceding section lengths.
        preceding = sum(len(s["body"]) for s in revision.sections[:index])
        span = {"start": preceding + local_start, "end": preceding + local_end,
                "total_chars": len(text), "complete": preceding + local_start == 0 and preceding + local_end == len(text)}
        projected = (source.body if unchanged else
                     redact_credential_ranges(text, [(span["start"], span["end"])])[0])
        identity = (await _source_identities(db, local, {source.id: source}))[0]
        document_identity = {key: getattr(document, key) for key in (
            "id", "user_id", "workspace_id", "project_id", "visibility", "domain", "filename", "revision",
            "file_hash", "byte_count", "content_hash", "storage_key")}
        document_identity.update(created_at=_iso(document.created_at), updated_at=_iso(document.updated_at),
            source_ids_hash=command_digest(document.source_ids), page_ids_hash=command_digest(document.page_ids))
        dependencies = {"source": identity, "document": document_identity,
            "revision": {"id": revision.id, "origin": revision.origin, "content_hash": revision.content_hash,
                         "metadata": revision.metadata_, "created_at": _iso(revision.created_at)},
            "section": {"index": index, "hash": command_digest(section)}, "source_span": span}
        if len(json.dumps(dependencies, ensure_ascii=False).encode()) > 120000:
            return None
        ref = {"version": 1, "kind": "source", "id": source.id,
            "assistant_session_id": main_id, "user_id": scope.user_id, "workspace_id": scope.workspace_id,
            "project_id": source.project_id, "visibility": "PERSONAL", "revision": source.source_revision,
            "content_hash": source.content_hash, "metadata_hash": command_digest(document_identity),
            "dependencies_hash": command_digest(dependencies), "acl_epoch": scope.acl_epoch}
        original = {"id": source.id, "revision": source.source_revision, "content_hash": source.content_hash,
            "kind": "document_chunk", "origin": "uploaded_file", "project_id": source.project_id,
            "session_id": None, "message_id": None, "part_id": None, "occurred_at": None,
            "source_span": span, "relation": "original", "body_available": True,
            "document_id": document.id, "document_revision": document.revision,
            "document_hash": document.content_hash, "file_hash": document.file_hash,
            "filename": redact_credentials(document.filename), "page_id": document.page_ids[index],
            "section": index, "section_count": len(revision.sections), "document_origin": revision.origin}
        item = {"id": source.id, "kind": "source", "project_id": source.project_id, "revision": source.source_revision,
            "category": "DOCUMENT", "summary": projected[:400], "summary_truncated": len(projected) > 400,
            "summary_chars": len(projected), "projection_hash": text_hash(projected),
            "updated_at": _iso(document.updated_at), "valid_from": None, "valid_to": None, "expires_at": None,
            "status": "current", "sources": [original], "source_ref": ref}
        return item, {None: projected, source.id: projected}
    except (MemoryAccessDenied, WikiStateError, KeyError, TypeError, ValueError, AttributeError):
        return None
