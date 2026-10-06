"""Confirmed memories and uploaded text through current SQL source authority.

Only a new search calls the existing BM25/Qdrant retrieval service. Replaying an
observation never runs retrieval or replaces its original references. Ordinary
memory services and the persistent assistant extraction policy are unchanged.
"""
from hashlib import sha256
import json
import re
import time

from sqlalchemy import and_, or_, select

from assistant import memory_documents
from assistant.commands import _authority, command_digest
from assistant.history import _cursor_key
from assistant.knowledge import _iso, _scope_view, _source_identities
from assistant.policy import AssistantError
from assistant.transactions import begin_snapshot
from core.config import get_config
from core.crypto import SecretsConfigError, decrypt_secret, encrypt_secret
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryRevision, MemorySource, MemorySourceLink
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from memory import retrieval, service
from memory.index.base import DocumentSnapshot
from memory.policy import MemoryAccessDenied, active_memory_predicates, resolve_access_scope
from memory.presentation import SOURCE_ORIGINS
from memory.redaction import redact_credentials, text_hash
from memory.wiki.service import WikiStateError

VERSION = 1
PROJECTION_VERSION = "credentials-v1"
MAX_ITEMS = 20
MAX_SCAN = 200
MAX_LINKS = 100
MAX_DIRECT = 12
MAX_RESPONSE_BYTES = 60000
MAX_READ_CHARS = 16000
CURSOR_TTL = 900
CURSOR_DOMAIN = "assistant-memory-read-v1"
SOURCE_KINDS = {"manual", "user_confirmation", "user_correction", "user_statement", "verified_memory_revision"}
# Key of the actor's all-project scope among the per-project local scopes.
_ALL_PROJECTS = ("all-projects",)


def _unavailable():
    return AssistantError(410, "ASSISTANT_MEMORY_UNAVAILABLE", "The original memory or its evidence is unavailable")


def _budget_error():
    return AssistantError(409, "ASSISTANT_CONTEXT_BUDGET", "Narrow the memory read to fit its evidence budget")


def _cursor_error():
    return AssistantError(409, "ASSISTANT_MEMORY_CURSOR", "Restart this exact memory read")


def _filters(project_id, include_all_projects):
    if (type(include_all_projects) is not bool or project_id is not None and (
            not isinstance(project_id, str) or not 1 <= len(project_id) <= 64 or include_all_projects)):
        raise ValueError("Invalid memory scope")


async def _access(db, *, user_id, workspace_id, main_id, project_id=None, include_all_projects=False):
    _filters(project_id, include_all_projects)
    await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    if not get_config().memory.enabled("retrieval_v2", user_id):
        raise _unavailable()
    try:
        return await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=include_all_projects)
    except MemoryAccessDenied:
        raise _unavailable() from None


async def _human_identity(db, scope, source):
    """A role=user row alone is not authenticated original human evidence."""
    if not all((source.session_id, source.message_id, source.part_id)):
        raise _unavailable()
    origin = and_(Session.kind == "normal", Session.memory_policy == "standard",
                  Session.project_id == source.project_id)
    if source.project_id is None:
        # Personal evidence from the person's own assistant main session.
        origin = or_(origin, Session.kind == "assistant")
    session = await db.scalar(select(Session).where(Session.id == source.session_id,
        Session.user_id == scope.user_id, Session.workspace_id == scope.workspace_id,
        Session.is_deleted.is_(False), Session.parent_id.is_(None), origin))
    pair = (await db.execute(select(Message, Part).join(Part, Part.message_id == Message.id).where(
        Message.id == source.message_id, Message.session_id == source.session_id, Message.user_id == scope.user_id,
        Message.role == "user", Part.id == source.part_id, Part.session_id == source.session_id,
        Part.user_id == scope.user_id, Part.type == "text"))).one_or_none()
    if session is None or pair is None:
        raise _unavailable()
    message, part = pair
    data = part.data or {}
    if (data.get("origin") != "human" or data.get("synthetic") or data.get("ignored")
            or not isinstance(data.get("origin_ref"), dict)
            or data["origin_ref"].get("actor_user_id") != scope.user_id or not isinstance(data.get("text"), str)):
        raise _unavailable()
    original = data["text"]
    metadata = source.source_metadata or {}
    start, end = metadata.get("span_start", 0), metadata.get("span_end", len(original))
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(original)
            or source.body != original[start:end]):
        raise _unavailable()
    current_revision = await db.scalar(select(AgentEvent.sequence).where(
        AgentEvent.session_id == source.session_id, AgentEvent.user_id == scope.user_id,
        AgentEvent.part_id == source.part_id, AgentEvent.kind.in_(("part.created", "part.updated")))
        .order_by(AgentEvent.sequence.desc()).limit(1))
    if current_revision is None or current_revision != source.source_revision:
        raise _unavailable()
    return {"session": {key: getattr(session, key) for key in (
                "id", "user_id", "workspace_id", "project_id", "kind", "parent_id", "visibility", "memory_policy")},
            "message": {"id": message.id, "role": message.role, "agent": message.agent},
            "part_revision": current_revision, "origin_ref": data["origin_ref"],
            "full_text_hash": text_hash(original),
            "source_span": {"start": start, "end": end, "total_chars": len(original),
                            "complete": start == 0 and end == len(original)}}


async def _all_projects(db, scope, local_scopes):
    """The actor's all-project scope, resolved once per pass like the per-project ones."""
    if _ALL_PROJECTS not in local_scopes:
        local_scopes[_ALL_PROJECTS] = await resolve_access_scope(db, user_id=scope.user_id,
            workspace_id=scope.workspace_id, include_all_projects=True)
    return local_scopes[_ALL_PROJECTS]


async def _current(db, scope, main_id, row, local_scopes):
    if row is None:
        return None
    summary = (row.value or {}).get("summary") if isinstance(row.value, dict) else None
    if not isinstance(summary, str) or not summary or row.content_hash != service.content_hash(summary):
        return None
    try:
        if row.project_id not in local_scopes:
            local_scopes[row.project_id] = await resolve_access_scope(db, user_id=scope.user_id,
                workspace_id=scope.workspace_id, project_id=row.project_id)
        local = local_scopes[row.project_id]
        evidence = local
        revision = await db.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == row.id,
            MemoryRevision.revision == row.revision, *local.predicates(MemoryRevision)))
        if (revision is None or revision.value != row.value or revision.content_hash != row.content_hash
                or revision.status != "ACTIVE" or revision.confirmation_status != "CONFIRMED"):
            return None
        links = list((await db.scalars(select(MemorySourceLink).where(MemorySourceLink.memory_id == row.id,
            MemorySourceLink.revision == row.revision).order_by(MemorySourceLink.id).limit(MAX_LINKS + 1))).all())
        versions = {link.source_id: (link.source_revision, link.relation) for link in links}
        supported = {link.source_id: link.source_revision for link in links if link.relation == "SUPPORTS"}
        # Do not inner-join away missing sources or treat an empty source set as
        # authorization. Revisions freeze the entire support/superseded set.
        if (not 1 <= len(supported) <= MAX_DIRECT or len(links) > MAX_LINKS or len(versions) != len(links)
                or any(link.relation not in {"SUPPORTS", "SUPERSEDED"} for link in links)
                or sha256(json.dumps(sorted(versions.items()), sort_keys=True).encode()).hexdigest() != revision.source_set_hash):
            return None
        sources = {source.id: source for source in (await db.scalars(select(MemorySource).where(
            *evidence.predicates(MemorySource), MemorySource.id.in_(supported)))).all()}
        if set(sources) != set(supported) and row.project_id is None:
            # A personal fact can rest on what the person said inside one of
            # their projects: find that evidence by owner, and let source
            # availability decide. Its words stay readable only within ``local``.
            evidence = await _all_projects(db, scope, local_scopes)
            sources = {source.id: source for source in (await db.scalars(select(MemorySource).where(
                *evidence.predicates(MemorySource), MemorySource.id.in_(supported)))).all()}
        if set(sources) != set(supported):
            return None
        for source in sources.values():
            if (source.source_revision != supported[source.id] or not source.body
                    or not await service.source_is_available(db, evidence, source)):
                return None
        try:
            identities = await _source_identities(db, evidence, sources)
        except WikiStateError:
            if row.project_id is not None or evidence is not local:
                raise
            # A personal fact revised from project evidence: its original words
            # (the revision's leaves) are in that project.
            evidence = await _all_projects(db, scope, local_scopes)
            identities = await _source_identities(db, evidence, sources)
        all_sources = {source.id: source for source in (await db.scalars(select(MemorySource).where(
            *evidence.predicates(MemorySource), MemorySource.id.in_([entry["id"] for entry in identities])))).all()}
        provenance, bodies = [], {None: redact_credentials(summary)}
        for identity in identities:
            source = all_sources[identity["id"]]
            if source.source_kind not in SOURCE_KINDS or not source.body:
                return None
            if source.source_kind == "user_statement":
                identity["original_human"] = await _human_identity(db, evidence, source)
            elif source.session_id or source.message_id or source.part_id:
                # Manual confirmations/corrections have their own persisted
                # admission identity; do not turn an arbitrary chat into one.
                return None
            readable = await service.source_body_is_available(db, local, source)
            identity["body_readable"] = readable
            bodies[source.id] = redact_credentials(source.body) if readable else None
            provenance.append({"id": source.id, "revision": source.source_revision,
                "content_hash": source.content_hash, "kind": source.source_kind,
                "origin": SOURCE_ORIGINS[source.source_kind], "project_id": source.project_id,
                "session_id": source.session_id, "message_id": source.message_id, "part_id": source.part_id,
                "occurred_at": _iso(source.occurred_at) if source.occurred_at else None,
                "source_span": (identity.get("original_human") or {}).get("source_span"),
                "relation": "supports" if source.id in supported else "dependency", "body_available": readable})
        metadata = {key: getattr(row, key) for key in (
            "scope", "type", "owner", "confirmation_status", "confirmation_actor_id", "confidence",
            "fact_key", "policy_version", "content_hash", "acl_epoch")}
        metadata.update({key: _iso(getattr(row, key)) if getattr(row, key) else None for key in (
            "created_at", "updated_at", "occurred_at", "recorded_at", "valid_from", "valid_to", "ttl")})
        dependencies = {"links": versions, "sources": identities,
            "revision": {"id": revision.id, "source_set_hash": revision.source_set_hash,
                "actor_user_id": revision.actor_user_id, "reason": revision.reason, "value": revision.value}}
        if len(json.dumps({"metadata": metadata, "dependencies": dependencies}, ensure_ascii=False).encode()) > 2 * MAX_RESPONSE_BYTES:
            return None
        reference = {"version": VERSION, "kind": "memory", "id": row.id,
            "assistant_session_id": main_id, "user_id": scope.user_id, "workspace_id": scope.workspace_id,
            "project_id": row.project_id, "visibility": "PERSONAL", "revision": row.revision,
            "content_hash": text_hash(summary), "metadata_hash": command_digest(metadata),
            "dependencies_hash": command_digest(dependencies), "acl_epoch": scope.acl_epoch}
        text = bodies[None]
        item = {"id": row.id, "kind": "memory", "project_id": row.project_id, "revision": row.revision,
            "category": row.type, "summary": text[:400], "summary_truncated": len(text) > 400,
            "summary_chars": len(text), "projection_hash": text_hash(text),
            "updated_at": metadata["updated_at"], "valid_from": metadata["valid_from"],
            "valid_to": metadata["valid_to"], "expires_at": metadata["ttl"],
            "status": "current", "sources": provenance, "source_ref": reference}
        return item, bodies
    except (MemoryAccessDenied, WikiStateError, AssistantError, KeyError, TypeError, ValueError, AttributeError):
        return None


async def _rows(db, scope, *, ids=None, limit=MAX_SCAN):
    query = select(UserMemory).where(*scope.predicates(UserMemory), *active_memory_predicates())
    if ids is not None:
        query = query.where(UserMemory.id.in_(ids))
    return list((await db.scalars(query.order_by(UserMemory.updated_at.desc(), UserMemory.id).limit(limit))).all())


def _document(item, text, scope):
    # The existing retrieval protocol returns its final SQL materialization.
    # Bind all metadata/source authority, not just the summary's revision/text.
    sources = tuple({**source, "memory_authority_hash": command_digest(item["source_ref"])} for source in item["sources"])
    return DocumentSnapshot(item["kind"], item["id"], item["revision"], text, scope.user_id, scope.workspace_id,
        item["project_id"], scope.acl_epoch, text_hash(text), sources, item["valid_from"], item["valid_to"],
        item["expires_at"], confirmation_status="UPLOADED_DOCUMENT" if item["kind"] == "source" else "CONFIRMED",
        category=item["category"])


async def _entry_rows(db, scope, references):
    memories = [ref["id"] for ref in references if ref["kind"] == "memory"]
    sources = [ref["id"] for ref in references if ref["kind"] == "source"]
    rows = {("memory", row.id): row for row in await _rows(db, scope, ids=memories)} if memories else {}
    if sources and get_config().memory.enabled("wiki", scope.user_id):
        rows.update({("source", row.id): row for row in await memory_documents.rows(db, scope, sources)})
    return rows


async def _entry_current(db, scope, main_id, kind, row, local_scopes):
    if kind == "memory":
        return await _current(db, scope, main_id, row, local_scopes)
    if kind == "source" and get_config().memory.enabled("wiki", scope.user_id):
        return await memory_documents.current(db, scope, main_id, row, local_scopes)
    return None


async def search(*, user_id, workspace_id, main_id, query, project_id=None, include_all_projects=False, limit=10):
    if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("Invalid memory search")
    identity = dict(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                    project_id=project_id, include_all_projects=include_all_projects)
    async with get_db_session() as db:
        await begin_snapshot(db)
        await _access(db, **identity)

    async def loader(db, scope, config, *, only=None):
        await _access(db, **identity)
        ids = [object_id for kind, object_id in only if kind == "memory"] if only is not None else None
        rows = await _rows(db, scope, ids=ids, limit=min(config.lexical_scan_limit, MAX_SCAN) if ids is None else max(1, len(ids)))
        documents, local_scopes = [], {}
        for row in rows:
            current = await _current(db, scope, main_id, row, local_scopes)
            if current:
                documents.append(_document(current[0], current[1][None], scope))
        if config.enabled("wiki", scope.user_id):
            from memory.documents.authority import authorized_chunks
            chunks = await authorized_chunks(db, scope, config, only=only)
            for row in await memory_documents.rows(db, scope, [chunk.id for chunk in chunks]):
                current = await memory_documents.current(db, scope, main_id, row, local_scopes)
                if current:
                    documents.append(_document(current[0], current[1][None], scope))
        return documents

    # No debug writer, orchestration, router, hit counter, index writes or
    # background work. Only this explicit search may call query embedding.
    config = get_config().memory.model_copy(update={"rerank": False, "debug_view": False, "debug_replay": False,
        "lexical_scan_limit": min(get_config().memory.lexical_scan_limit, MAX_SCAN)})
    bundle = await retrieval.search_memory(query=redact_credentials(query), user_id=user_id,
        workspace_id=workspace_id, project_id=project_id, include_all_projects=include_all_projects,
        limit=limit, config=config, document_loader=loader, kinds=("memory", "source"))
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, **identity)
        rows = await _entry_rows(db, scope, bundle["items"])
        items, local_scopes = [], {}
        for entry in bundle["items"]:
            current = await _entry_current(db, scope, main_id, entry["kind"], rows.get((entry["kind"], entry["id"])), local_scopes)
            if not current:
                continue
            item, bodies = current
            document = _document(item, bodies[None], scope)
            if (entry["revision"] != item["revision"] or entry["text"] != document.text
                    or entry["sources"] != list(document.sources)):
                continue
            if len(json.dumps([*items, item], ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES - 2048:
                if not items:
                    raise _budget_error()
                break
            items.append(item)
        value = {"items": items, "status": "evidence" if items else "no_available_evidence",
            "scope": _scope_view(scope), "untrusted_data": True,
            "search": {"method": "bm25+qdrant", "bounded": True, "exhaustive": False,
                "lexical_scan_limit": min(config.lexical_scan_limit, MAX_SCAN), "limit": min(limit, config.retrieval_limit),
                "degraded_reasons": [reason if re.fullmatch(r"[a-z_]{1,64}", reason) else "retrieval_unavailable"
                                     for reason in bundle["degraded_reasons"]],
                "truncated": bool(bundle["budget"]["trimmed"] or len(items) < len(bundle["items"]))}}
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
            raise _budget_error()
        return value


def valid_reference(ref):
    if not isinstance(ref, dict) or set(ref) != {"version", "kind", "id", "assistant_session_id", "user_id",
            "workspace_id", "project_id", "visibility", "revision", "content_hash", "metadata_hash", "dependencies_hash", "acl_epoch"}:
        return False
    return (type(ref["version"]) is int and ref["version"] == VERSION and ref["kind"] in ("memory", "source")
        and ref["visibility"] == "PERSONAL"
        and all(type(ref[key]) is int and 1 <= ref[key] <= 0x7FFFFFFF for key in ("revision", "acl_epoch"))
        and all(isinstance(ref[key], str) and 1 <= len(ref[key]) <= 64 for key in ("id", "assistant_session_id", "user_id", "workspace_id"))
        and (ref["project_id"] is None or isinstance(ref["project_id"], str) and 1 <= len(ref["project_id"]) <= 64)
        and all(isinstance(ref[key], str) and re.fullmatch(r"[0-9a-f]{64}", ref[key])
                for key in ("content_hash", "metadata_hash", "dependencies_hash")))


async def revalidate_items(db, scope, main_id, references, *, local_scopes=None):
    if (not isinstance(references, list) or len(references) > MAX_ITEMS
            or any(not valid_reference(ref) for ref in references)
            or len({(ref["kind"], ref["id"]) for ref in references}) != len(references)):
        raise _unavailable()
    rows = await _entry_rows(db, scope, references)
    result = []
    local_scopes = {} if local_scopes is None else local_scopes
    for ref in references:
        current = await _entry_current(db, scope, main_id, ref["kind"], rows.get((ref["kind"], ref["id"])), local_scopes)
        if not current or current[0]["source_ref"] != ref:
            raise _unavailable()
        result.append(current[0])
    return result


def _selection(scope, main_id, reference, source_id, max_chars, projection_hash):
    return command_digest({"version": VERSION, "projection_version": PROJECTION_VERSION,
        "user_id": scope.user_id, "main_id": main_id, "scope": _scope_view(scope), "source_ref": reference,
        "source_id": source_id, "max_chars": max_chars, "projection_hash": projection_hash})


def _cursor(value, *, selection, check_expiry=True):
    key = sha256(CURSOR_DOMAIN.encode() + b":" + _cursor_key()).hexdigest()
    aad = CURSOR_DOMAIN + ":" + selection
    if isinstance(value, dict):
        return encrypt_secret(json.dumps(value, sort_keys=True, separators=(",", ":")), aad, key)
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            raise ValueError()
        state = json.loads(decrypt_secret(value, aad, key))
        if (not isinstance(state, dict) or set(state) != {"version", "offset", "expires"}
                or type(state["version"]) is not int or state["version"] != VERSION
                or type(state["offset"]) is not int or state["offset"] <= 0
                or type(state["expires"]) is not int or check_expiry and state["expires"] <= time.time()):
            raise ValueError()
        return state
    except (SecretsConfigError, ValueError, TypeError, AttributeError):
        raise _cursor_error() from None


async def read(*, user_id, workspace_id, main_id, source_ref, project_id=None, include_all_projects=False,
               source_id=None, max_chars=8000, cursor=None):
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                              project_id=project_id, include_all_projects=include_all_projects)
        return await _read_locked(db, scope, main_id, source_ref=source_ref, source_id=source_id,
                                  max_chars=max_chars, cursor=cursor)


async def _read_locked(db, scope, main_id, *, source_ref, source_id, max_chars, cursor, check_expiry=True,
                       local_scopes=None):
    if (not valid_reference(source_ref) or type(max_chars) is not int or not 1 <= max_chars <= MAX_READ_CHARS
            or source_id is not None and (not isinstance(source_id, str) or not 1 <= len(source_id) <= 64)):
        raise _unavailable()
    rows = await _entry_rows(db, scope, [source_ref])
    current = await _entry_current(db, scope, main_id, source_ref["kind"],
        rows.get((source_ref["kind"], source_ref["id"])), {} if local_scopes is None else local_scopes)
    if not current or current[0]["source_ref"] != source_ref or not current[1].get(source_id):
        raise _unavailable()
    item, bodies = current
    body = bodies[source_id]
    selected = next((source for source in item["sources"] if source["id"] == source_id), None)
    projection_hash = text_hash(body)
    selection = _selection(scope, main_id, source_ref, source_id, max_chars, projection_hash)
    state = _cursor(cursor, selection=selection, check_expiry=check_expiry) if cursor is not None else None
    offset = state["offset"] if state else 0
    if offset >= len(body):
        raise _cursor_error()
    end = min(offset + max_chars, len(body))
    value = {"item": item, "selected_source": selected, "text": "", "offset": offset, "end_offset": end,
        "total_chars": len(body), "projection_version": PROJECTION_VERSION, "projection_hash": projection_hash,
        "chunk_hash": "0" * 64, "next_cursor": None, "truncated": end < len(body),
        "untrusted_data": True, "scope": _scope_view(scope)}
    room = MAX_RESPONSE_BYTES - 4096 - len(json.dumps(value, ensure_ascii=False).encode())
    low, high = 0, end - offset
    while low < high:
        size = (low + high + 1) // 2
        if len(json.dumps(body[offset:offset + size], ensure_ascii=False).encode()) - 2 <= room:
            low = size
        else:
            high = size - 1
    if not low:
        raise _budget_error()
    end = offset + low
    value.update(text=body[offset:end], end_offset=end, truncated=end < len(body), chunk_hash=text_hash(body[offset:end]))
    if value["truncated"]:
        value["next_cursor"] = _cursor({"version": VERSION, "offset": end,
            "expires": state["expires"] if state else int(time.time()) + CURSOR_TTL}, selection=selection)
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
        raise _budget_error()
    return value
