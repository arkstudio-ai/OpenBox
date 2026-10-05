"""Read-only source authority for the assistant knowledge directory.

The tool adapter binds this data through the existing business observation
and exact provider-request provenance. This module alone never authorizes a
provider request, and does not relax assistant_isolated or legacy memory paths.

The default selection is personal background only. All owned projects require
an explicit include_all_projects=True; the main Session's storage project is
never inferred as the knowledge scope. No read creates a job, index or source.
"""
from datetime import timezone
from hashlib import sha256
import json
import re
import time

from sqlalchemy import select

from assistant.commands import _authority, command_digest
from assistant.history import _cursor_key
from assistant.policy import AssistantError
from assistant.transactions import begin_snapshot
from core.config import get_config
from core.crypto import SecretsConfigError, decrypt_secret, encrypt_secret
from db.base import get_db_session
from db.models.memory_v2 import MemorySource
from db.models.memory_wiki import MemoryWikiPage
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.redaction import redact_credentials, text_hash
from memory.wiki import service

VERSION = 1
MAX_PAGE_SIZE = 50
MAX_SCAN = 200
MAX_DEPENDENCIES = 12
MAX_LEAF_SOURCES = 100
MAX_RESPONSE_BYTES = 60000
CURSOR_TTL = 900
CURSOR_DOMAIN = "assistant-knowledge-directory-v1"


def _unavailable():
    return AssistantError(410, "ASSISTANT_KNOWLEDGE_UNAVAILABLE", "The knowledge source is unavailable")


def _cursor_error():
    return AssistantError(409, "ASSISTANT_KNOWLEDGE_CURSOR", "Restart this knowledge directory selection")


def _budget_error():
    return AssistantError(409, "ASSISTANT_CONTEXT_BUDGET", "The knowledge directory exceeds its read budget")


def _cursor(value, *, selection, check_expiry=True):
    # An examined row may have failed source validation. Encrypt its position,
    # rather than exposing its ID in a merely signed/base64 cursor.
    key = sha256(CURSOR_DOMAIN.encode() + b":" + _cursor_key()).hexdigest()
    aad = CURSOR_DOMAIN + ":" + selection
    if isinstance(value, dict):
        return encrypt_secret(json.dumps(value, sort_keys=True, separators=(",", ":")), aad, key)
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError()
        state = json.loads(decrypt_secret(value, aad, key))
        if (state.get("version") != VERSION or type(state.get("expires")) is not int
                or (check_expiry and state["expires"] <= time.time()) or not isinstance(state.get("after"), str)
                or not 1 <= len(state["after"]) <= 64):
            raise ValueError()
        return state
    except (SecretsConfigError, ValueError, TypeError, AttributeError):
        raise _cursor_error() from None


def _iso(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


async def _access(db, *, user_id, workspace_id, main_id, project_id, include_all_projects):
    await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    if not get_config().memory.enabled("wiki", user_id):
        raise _unavailable()
    try:
        return await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=include_all_projects)
    except MemoryAccessDenied:
        raise _unavailable() from None


def _selection(scope, main_id, query, limit):
    return command_digest({"version": VERSION, "user_id": scope.user_id,
        "workspace_id": scope.workspace_id, "main_id": main_id, "project_id": scope.project_id,
        "include_all_projects": scope.include_all_projects, "project_ids": sorted(scope.project_ids),
        "acl_epoch": scope.acl_epoch, "query": query, "limit": limit})


async def _source_identities(db, scope, sources):
    """Freeze direct sources and the bounded original evidence of revisions.

    Reconciliation stores flattened leaves, never a recursive authority graph.
    A leaf's id/revision/body hash does not freeze its Session/Part identity or
    metadata. Preserve those bindings too, while keeping the original page's
    project policy and the existing source availability checks.
    """
    from memory.reconciliation import POLICY, SOURCE_KIND
    from memory.service import source_is_available

    references = {}
    for source in sources.values():
        if source.source_kind != SOURCE_KIND:
            continue
        metadata = source.source_metadata or {}
        refs = metadata.get("dependencies")
        if metadata.get("policy") != POLICY or not isinstance(refs, list) or not 1 <= len(refs) <= MAX_LEAF_SOURCES:
            raise service.WikiStateError("wiki_source_unavailable")
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {"id", "revision", "content_hash"}
                    or not isinstance(ref["id"], str) or not 1 <= len(ref["id"]) <= 64
                    or type(ref["revision"]) is not int or not 1 <= ref["revision"] <= 0x7FFFFFFF
                    or not isinstance(ref["content_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["content_hash"])
                    or ref["id"] in references and references[ref["id"]] != ref):
                raise service.WikiStateError("wiki_source_unavailable")
            references[ref["id"]] = ref
            if len(references) > MAX_LEAF_SOURCES:
                raise service.WikiStateError("wiki_source_budget_exceeded")
    leaves = (await db.scalars(select(MemorySource).where(*scope.predicates(MemorySource),
        MemorySource.id.in_(references)))).all() if references else []
    if len(leaves) != len(references):
        raise service.WikiStateError("wiki_source_unavailable")
    all_sources = dict(sources)
    for leaf in leaves:
        ref = references[leaf.id]
        if (leaf.source_kind == SOURCE_KIND or leaf.source_revision != ref["revision"]
                or leaf.content_hash != ref["content_hash"] or not await source_is_available(db, scope, leaf)):
            raise service.WikiStateError("wiki_source_unavailable")
        all_sources[leaf.id] = leaf
    return [{**{key: getattr(source, key) for key in (
        "id", "user_id", "workspace_id", "project_id", "visibility", "source_kind", "session_id", "message_id",
        "part_id", "branch_id", "turn_id", "start_seq", "end_seq", "source_metadata", "source_revision",
        "content_hash", "acl_epoch")}, "occurred_at": _iso(source.occurred_at) if source.occurred_at else None,
        "created_at": _iso(source.created_at)} for source in sorted(all_sources.values(), key=lambda source: source.id)]


async def _current_item(db, scope, main_id, page, local_scopes):
    if (page.status != "PUBLISHED" or not page.body or page.content_hash != text_hash(page.body)
            or not isinstance(page.source_manifest, list) or not 1 <= len(page.source_manifest) <= MAX_DEPENDENCIES
            or not isinstance(page.memory_manifest, list) or not 1 <= len(page.memory_manifest) <= MAX_DEPENDENCIES
            or await service.target_is_deleted(db, page)):
        return None
    try:
        # The all-project scope discovers candidates, but cannot validate a
        # project's page against sources borrowed from a different project.
        if page.project_id not in local_scopes:
            local_scopes[page.project_id] = await resolve_access_scope(db, user_id=scope.user_id,
                workspace_id=scope.workspace_id, project_id=page.project_id)
        local = local_scopes[page.project_id]
        sources = await service.read_sources(db, local, page.source_manifest, page.memory_manifest,
                                             acl_epoch=page.acl_epoch)
        identities = await _source_identities(db, local, sources)
    except (MemoryAccessDenied, service.WikiStateError, KeyError, TypeError, ValueError, AttributeError):
        # Invalid/stale rows do not disclose their title, project, source count
        # or body, and are not searchable through the directory.
        return None
    metadata = {"title": page.title, "slug": page.slug, "updated_at": _iso(page.updated_at),
        "target_identity": page.target_identity, "policy_version": page.policy_version,
        "candidate_id": page.candidate_id, "input_hash": page.input_hash, "model": page.model}
    dependencies = {"sources": page.source_manifest, "memories": page.memory_manifest,
        "source_identity": identities}
    reference = {"version": VERSION, "kind": "wiki", "id": page.id,
        "assistant_session_id": main_id, "user_id": scope.user_id, "workspace_id": scope.workspace_id,
        "project_id": page.project_id, "visibility": "PERSONAL", "revision": page.revision,
        "content_hash": page.content_hash, "metadata_hash": command_digest(metadata),
        "dependencies_hash": command_digest(dependencies), "acl_epoch": scope.acl_epoch}
    return {"id": page.id, "title": redact_credentials(page.title), "project_id": page.project_id,
        "revision": page.revision, "content_hash": page.content_hash, "acl_epoch": scope.acl_epoch,
        "updated_at": metadata["updated_at"], "status": "current", "source_ref": reference}


def _filters(project_id, include_all_projects, query, limit):
    if (type(include_all_projects) is not bool or type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE
            or not isinstance(query, str) or len(query) > 200
            or project_id is not None and (not isinstance(project_id, str) or not 1 <= len(project_id) <= 64)
            or project_id is not None and include_all_projects):
        raise ValueError("Invalid knowledge directory selection")
    return query.strip().casefold()


async def directory(*, user_id, workspace_id, main_id, project_id=None, include_all_projects=False,
                    query="", limit=20, cursor=None):
    """Return a bounded page of current Wiki titles, searched after validation.

    No total/match count or unavailable entry is returned. An empty page can
    have a continuation when the bounded scan has not exhausted candidates.
    The cursor binds identity, selection, ACL/project set and schema version;
    every continuation still resolves all current authority from SQL.
    """
    query = _filters(project_id, include_all_projects, query, limit)
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                              project_id=project_id, include_all_projects=include_all_projects)
        return await _directory_locked(db, scope, main_id, query=query, limit=limit, cursor=cursor)


def _scope_view(scope):
    return {"workspace_id": scope.workspace_id, "project_id": scope.project_id,
        "include_all_projects": scope.include_all_projects, "visibility": "PERSONAL", "acl_epoch": scope.acl_epoch}


async def _directory_locked(db, scope, main_id, *, query, limit, cursor):
    """The caller owns a new read-only transaction and a freshly resolved scope."""
    selection = _selection(scope, main_id, query, limit)
    state = _cursor(cursor, selection=selection) if cursor is not None else None
    after = state["after"] if state else ""
    rows = list((await db.scalars(select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage),
        MemoryWikiPage.id > after, MemoryWikiPage.deleted_at.is_(None), MemoryWikiPage.status == "PUBLISHED")
        .order_by(MemoryWikiPage.id).limit(MAX_SCAN + 1))).all())
    items, local_scopes, consumed = [], {}, 0
    for page in rows[:MAX_SCAN]:
        item = await _current_item(db, scope, main_id, page, local_scopes)
        if item and query in item["title"].casefold():
            if len(json.dumps([*items, item], ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES - 4096:
                if not items:
                    raise _budget_error()
                break
            items.append(item)
        consumed += 1
        after = page.id
        if len(items) == limit:
            break
    more = consumed < len(rows)
    next_cursor = _cursor({"version": VERSION, "after": after,
        "expires": state["expires"] if state else int(time.time()) + CURSOR_TTL}, selection=selection) if more else None
    result = {"items": items, "next_cursor": next_cursor, "untrusted_data": True, "scope": _scope_view(scope)}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
        raise _budget_error()
    return result


def _valid_reference(ref):
    if not isinstance(ref, dict) or set(ref) != {"version", "kind", "id", "assistant_session_id", "user_id",
            "workspace_id", "project_id", "visibility", "revision", "content_hash", "metadata_hash",
            "dependencies_hash", "acl_epoch"}:
        return False
    if (type(ref["version"]) is not int or ref["version"] != VERSION or ref["kind"] != "wiki"
            or ref["visibility"] != "PERSONAL" or type(ref["revision"]) is not int or not 1 <= ref["revision"] <= 0x7FFFFFFF
            or type(ref["acl_epoch"]) is not int or not 1 <= ref["acl_epoch"] <= 0x7FFFFFFF):
        return False
    if any(not isinstance(ref[key], str) or not 1 <= len(ref[key]) <= 64
           for key in ("id", "assistant_session_id", "user_id", "workspace_id")):
        return False
    if ref["project_id"] is not None and (not isinstance(ref["project_id"], str) or not 1 <= len(ref["project_id"]) <= 64):
        return False
    return all(isinstance(ref[key], str) and re.fullmatch(r"[0-9a-f]{64}", ref[key])
               for key in ("content_hash", "metadata_hash", "dependencies_hash"))


async def revalidate_directory_refs(*, user_id, workspace_id, main_id, source_refs,
                                    project_id=None, include_all_projects=False):
    """Reread exact directory references in a new transaction, or fail closed.

    This is evidence validation only, not a capability or provider dispatch
    grant. Callers cannot retain this result to authorize a later operation.
    """
    _filters(project_id, include_all_projects, "", MAX_PAGE_SIZE)
    async with get_db_session() as db:
        await begin_snapshot(db)
        scope = await _access(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                              project_id=project_id, include_all_projects=include_all_projects)
        return await _revalidate_refs_locked(db, scope, main_id, source_refs)


async def _revalidate_refs_locked(db, scope, main_id, source_refs):
    if (not isinstance(source_refs, list) or len(source_refs) > MAX_PAGE_SIZE
            or any(not _valid_reference(ref) for ref in source_refs)
            or len({ref["id"] for ref in source_refs}) != len(source_refs)
            or len(json.dumps(source_refs, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES):
        raise _unavailable()
    rows = (await db.scalars(select(MemoryWikiPage).where(*scope.predicates(MemoryWikiPage),
        MemoryWikiPage.id.in_([ref["id"] for ref in source_refs]), MemoryWikiPage.deleted_at.is_(None)))).all()
    by_id, local_scopes, result = {row.id: row for row in rows}, {}, []
    for reference in source_refs:
        row = by_id.get(reference["id"])
        item = await _current_item(db, scope, main_id, row, local_scopes) if row else None
        if item is None or reference != item["source_ref"]:
            raise _unavailable()
        result.append(item)
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
            raise _budget_error()
    return result
