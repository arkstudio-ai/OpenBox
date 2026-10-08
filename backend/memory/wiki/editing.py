"""Simple edits change authoritative facts, not a competing copy of the truth."""
from sqlalchemy import select

from core import config as runtime_config
from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemorySource
from db.models.memory_wiki import MemoryWikiDependency, MemoryWikiPage
from db.models.wiki_platform import WikiConcept
from memory.policy import active_memory_predicates, resolve_access_scope
from memory.service import MAX_SUMMARY_CHARS, MemoryConflict, edit_note_in_session, lock_memory_authority
from memory.wiki import service
from wiki_compiler.hashing import canonical_hash, text_hash


async def _page(db, user_id, workspace_id, page_id, *, lock=False):
    scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
    stmt = select(MemoryWikiPage).where(MemoryWikiPage.id == page_id, *scope.predicates(MemoryWikiPage))
    page = await db.scalar(stmt.with_for_update() if lock else stmt)
    if page is None or await service.target_is_deleted(db, page):
        return None, None
    local = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=page.project_id)
    return page, local


async def _entries(db, scope, page):
    uploaded = [item for item in page.memory_manifest if item["kind"] == "document"]
    if uploaded:
        from memory.documents.authority import read_dependency
        document, revision = await read_dependency(db, scope, uploaded[0])
        section = revision.sections[document.page_ids.index(page.id)]
        return [{"id": document.id, "revision": document.revision, "text": section["body"], "max_length": 12000}], None
    imported = [item for item in page.memory_manifest if item["kind"] == "exchange"]
    if imported:
        from memory.wiki.exchange import read_import_dependency
        document = await read_import_dependency(db, scope, imported[0])
        if not (await service._page_view(db, scope, page))["body_available"]:
            raise service.WikiStateError("wiki_edit_source_unavailable")
        return [{"id": document.id, "revision": document.revision, "text": document.body, "max_length": 32000}], document
    ids = [item["id"] for item in page.memory_manifest if item["kind"] == "memory"]
    rows = (await db.scalars(select(UserMemory).where(UserMemory.id.in_(ids), *scope.predicates(UserMemory),
        *active_memory_predicates()).order_by(UserMemory.id))).all()
    entries = []
    for row in rows:
        try:
            await service.collect_compile_sources(db, scope, [row.id])
        except service.WikiStateError:
            continue
        entries.append({"id": row.id, "revision": row.revision, "text": row.value.get("summary", ""),
                        "max_length": MAX_SUMMARY_CHARS})
    if not entries:
        raise service.WikiStateError("wiki_edit_source_unavailable")
    return entries, None


async def snapshot(*, user_id, workspace_id, page_id):
    async with get_db_session() as db:
        page, scope = await _page(db, user_id, workspace_id, page_id)
        if page is None:
            return None
        entries, _ = await _entries(db, scope, page)
        return {"id": page.id, "revision": page.revision, "content_hash": page.content_hash,
                "title": page.title, "entries": entries}


async def save(*, user_id, workspace_id, page_id, expected_revision, content_hash, title, entries, request_id):
    config = runtime_config.get_config().memory
    if not config.enabled("wiki", user_id):
        raise service.WikiStateError("wiki_disabled")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        page, scope = await _page(db, user_id, workspace_id, page_id, lock=True)
        if page is None:
            return None
        if page.revision != expected_revision or page.content_hash != content_hash:
            raise service.WikiStateError("wiki_edit_changed")
        current, document = await _entries(db, scope, page)
        by_id = {item["id"]: item for item in entries}
        if len(by_id) != len(entries) or set(by_id) != {item["id"] for item in current}:
            raise service.WikiStateError("wiki_edit_changed")
        for item in current:
            edit = by_id[item["id"]]
            if edit["revision"] != item["revision"]:
                raise service.WikiStateError("wiki_edit_changed")
            if not edit["text"].strip() or len(edit["text"]) > item["max_length"]:
                raise service.WikiStateError("wiki_edit_invalid_text")
        if not title.strip():
            raise service.WikiStateError("wiki_edit_invalid_text")
        changed = title.strip() != page.title or any(by_id[item["id"]]["text"] != item["text"] for item in current)
        if not changed:
            return {"id": page.id, "status": "unchanged"}
        if any(item["kind"] == "document" for item in page.memory_manifest):
            from memory.documents.service import edit_section
            await edit_section(db, scope, page, title.strip(), entries, config)
            return {"id": page.id, "status": "updating"}
        if document is not None:
            await _edit_import(db, scope, page, document, title.strip(), entries[0]["text"], config)
            return {"id": page.id, "status": "saved"}
        for item in current:
            edit = by_id[item["id"]]
            if edit["text"] == item["text"]:
                continue
            try:
                result = await edit_note_in_session(db, access=scope, memory_id=item["id"], summary=edit["text"],
                    expected_revision=item["revision"], request_id=f"{request_id}:{item['id']}")
            except MemoryConflict as exc:
                raise service.WikiStateError("wiki_edit_changed") from exc
            if result is None:
                raise service.WikiStateError("wiki_edit_changed")
        if page.title != title.strip():
            concepts = (await db.scalars(select(WikiConcept).where(WikiConcept.page_id == page.id,
                *scope.predicates(WikiConcept)))).all()
            for concept in concepts:
                concept.title, concept.user_edited = title.strip(), True
                concept.revision, concept.updated_at = concept.revision + 1, service.now()
            page.title, page.revision = title.strip(), page.revision + 1
        page.status, page.invalidation_reason, page.updated_at = "STALE", "user_edited", service.now()
        await service.enqueue_page_outbox(db, page, config, operation="REVOKE")
        from memory.wiki.maintenance import ensure_automatic, request_recheck
        await ensure_automatic(db, scope, config)
        await request_recheck(db, scope)
        return {"id": page.id, "status": "updating"}


async def _edit_import(db, scope, page, document, title, body, config):
    instant = service.now()
    source = MemorySource(id=ascending("memory_source"), user_id=scope.user_id, workspace_id=scope.workspace_id,
        project_id=scope.project_id, source_revision=1, source_kind="wiki_import", content_hash=text_hash(body),
        body=body, source_metadata={"bundle_id": document.bundle_id, "path": document.path,
            "import_document_id": document.id, "edited_by": scope.user_id, "previous_source_id": document.source_id,
            "previous_document_hash": document.content_hash}, acl_epoch=scope.acl_epoch, status="ACTIVE", created_at=instant)
    db.add(source)
    document.source_ids = [source.id if item == document.source_id else item for item in document.source_ids]
    document.source_id, document.body, document.title = source.id, body, title
    document.frontmatter = {**document.frontmatter, "title": title}
    document.content_hash = canonical_hash({"frontmatter": document.frontmatter, "body": body})
    document.revision, document.updated_at = document.revision + 1, instant
    await db.flush()
    sources = (await db.scalars(select(MemorySource).where(MemorySource.id.in_(document.source_ids)))).all()
    refs = [service._source_ref(item, scope) for item in sources]
    dependency = {"kind": "exchange", "id": document.id, "revision": document.revision,
                  "content_hash": document.content_hash, "source_ids": document.source_ids}
    page.revision, page.title, page.body, page.content_hash = page.revision + 1, title, body, text_hash(body)
    page.source_manifest, page.memory_manifest = refs, [dependency]
    page.paragraphs = [{"text": body, "citations": [{"source_id": source.id, "revision": 1,
        "content_hash": source.content_hash, "quote": body[:2400]}]}]
    page.input_hash, page.status, page.updated_at = document.content_hash, "PUBLISHED", instant
    page.policy_version, page.invalidation_reason = "user-edited-import-v1", None
    for item in [*refs, dependency]:
        db.add(MemoryWikiDependency(id=ascending("wiki_dep"), page_id=page.id, page_revision=page.revision,
            object_kind=item["kind"], object_id=item["id"], object_revision=item["revision"], content_hash=item["content_hash"]))
    await service.enqueue_page_outbox(db, page, config)
