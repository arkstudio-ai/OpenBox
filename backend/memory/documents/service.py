"""Authenticated upload, status, retry and versioned consumer editing."""
from datetime import timedelta
from pathlib import PurePath
import re

from sqlalchemy import String, delete as delete_rows, func, literal, or_, select

from core import config as runtime_config
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.memory_document import MemoryDocument, MemoryDocumentCleanup, MemoryDocumentRevision
from db.models.memory_v2 import MemoryIndexState, MemoryOutbox, MemorySource
from db.models.memory_wiki import MemoryWikiPage
from memory.documents.parser import DocumentError, EXTENSIONS, MAX_BYTES
from memory.documents.storage import document_storage, download_bytes
from memory.policy import resolve_access_scope
from memory.service import lock_memory_authority
from memory.wiki.organization import require_enabled, scoped_values
from memory.wiki.service import WikiStateError, domain_for, enqueue_page_outbox, now
from wiki_compiler.hashing import canonical_hash, text_hash
import hashlib

log = create_logger("memory.documents")


def clean_filename(value):
    name = re.split(r"[/\\]", value or "")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:255]
    if not name or PurePath(name).suffix.lower() not in EXTENSIONS:
        raise DocumentError("document_format_unsupported")
    return name


async def submit(*, user_id, workspace_id, project_id, filename, data, store=None):
    filename = clean_filename(filename)
    if not data or len(data) > MAX_BYTES:
        raise DocumentError("document_size_limit")
    config = runtime_config.get_config().memory
    require_enabled(config, user_id)
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        domain, digest = domain_for(scope, project_id), hashlib.sha256(data).hexdigest()
        existing = await db.scalar(select(MemoryDocument).where(MemoryDocument.domain == domain, MemoryDocument.file_hash == digest))
        if existing:
            return await document_view(db, existing, config)
    # Each upload gets its own object: an earlier delete of the same file, still
    # finishing in the background, must never remove this one.
    upload_id = ascending("upload")
    key = f"knowledge/{domain}/{digest}/{upload_id}/original{PurePath(filename).suffix.lower()}"
    if store:
        await store.upload(key, data)
    else:
        async with document_storage() as blob:
            await blob.upload(key, data)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        existing = await db.scalar(select(MemoryDocument).where(MemoryDocument.domain == domain, MemoryDocument.file_hash == digest))
        if not existing:
            row = MemoryDocument(id=ascending("memory_doc"), **scoped_values(scope), domain=domain,
                filename=filename, file_hash=digest, byte_count=len(data), storage_key=key, content_hash="",
                source_ids=[], page_ids=[], status="PENDING", attempts=0, lease_generation=0, available_at=now())
            db.add(row)
            await db.flush()
            return await document_view(db, row, config)
        # A concurrent upload of the same file won; this copy is unused.
        orphan = _cleanup(upload_id, scope, key)
        db.add(orphan)
        orphan_id, view = orphan.id, await document_view(db, existing, config)
    await remove_original(orphan_id, store=store)
    return view


async def document_view(db, row, config):
    states = (await db.scalars(select(MemoryIndexState).where(MemoryIndexState.object_kind == "source",
        MemoryIndexState.object_id.in_(row.source_ids), MemoryIndexState.index_generation == config.index_generation))).all()
    indexed = sum(s.status == "INDEXED" and s.indexed_revision == row.revision for s in states)
    failed = any(s.status in {"DEAD", "INELIGIBLE", "DELETED"} for s in states) or bool(await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.object_kind == "source",
        MemoryOutbox.object_id.in_(row.source_ids), MemoryOutbox.index_generation == config.index_generation,
        MemoryOutbox.operation == "UPSERT", MemoryOutbox.status == "DEAD")))
    stage = row.status.lower()
    if row.status == "READY" and indexed < len(row.source_ids):
        stage = "index_failed" if failed else "indexing" if config.enabled("index_sync", row.user_id) else "searchable"
    return {"id": row.id, "filename": row.filename, "revision": row.revision, "project_id": row.project_id,
        "bytes": row.byte_count, "status": stage, "reason_code": row.reason_code,
        "chunk_count": len(row.source_ids), "indexed_chunks": indexed, "page_ids": row.page_ids,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


async def list_documents(*, user_id, workspace_id, project_id=None, offset=0):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=project_id is None)
        rows = (await db.scalars(select(MemoryDocument).where(*scope.predicates(MemoryDocument))
            .order_by(MemoryDocument.created_at.desc(), MemoryDocument.id).offset(offset).limit(21))).all()
        # Deleted files whose originals are still being removed from storage.
        pending = select(func.count()).select_from(MemoryDocumentCleanup).where(
            MemoryDocumentCleanup.user_id == scope.user_id, MemoryDocumentCleanup.workspace_id == scope.workspace_id,
            MemoryDocumentCleanup.status == "PENDING")
        if project_id is not None:
            pending = pending.where(MemoryDocumentCleanup.project_id == project_id)
        return {"documents": [await document_view(db, row, runtime_config.get_config().memory) for row in rows[:20]],
                "next_offset": offset + 20 if len(rows) > 20 else None,
                "cleanup_pending": await db.scalar(pending) or 0}


async def owned(db, user_id, workspace_id, document_id, *, lock=False):
    scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
    stmt = select(MemoryDocument).where(*scope.predicates(MemoryDocument), MemoryDocument.id == document_id)
    return await db.scalar(stmt.with_for_update() if lock else stmt)


async def original(*, user_id, workspace_id, document_id):
    async with get_db_session() as db:
        row = await owned(db, user_id, workspace_id, document_id)
        if not row:
            return None
    async with document_storage() as blob:
        data = await download_bytes(blob, row.storage_key)
    if hashlib.sha256(data).hexdigest() != row.file_hash:
        raise DocumentError("document_file_changed")
    # Revalidate after blob IO. Membership can change during a slow download.
    async with get_db_session() as db:
        if not await owned(db, user_id, workspace_id, document_id):
            return None
    return row.filename, data


async def retry(*, user_id, workspace_id, document_id):
    config = runtime_config.get_config().memory
    require_enabled(config, user_id)
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        row = await owned(db, user_id, workspace_id, document_id, lock=True)
        if not row:
            return None
        if row.status in {"FAILED", "RETRY"}:
            row.status, row.attempts, row.reason_code, row.available_at = "PENDING", 0, None, now()
        states = (await db.scalars(select(MemoryIndexState).where(MemoryIndexState.object_kind == "source",
            MemoryIndexState.object_id.in_(row.source_ids), MemoryIndexState.index_generation == config.index_generation))).all()
        repair_ids = {state.object_id for state in states if state.status in {"DEAD", "INELIGIBLE", "DELETED"}}
        events = (await db.scalars(select(MemoryOutbox).where(MemoryOutbox.object_kind == "source",
            MemoryOutbox.object_id.in_(repair_ids if row.status == "READY" else []), MemoryOutbox.operation == "UPSERT",
            MemoryOutbox.index_generation == config.index_generation, MemoryOutbox.status.in_(["DEAD", "SUCCEEDED"])))).all()
        for event in events:
            event.status, event.attempts, event.available_at, event.last_error = "PENDING", 0, now(), None
        for state in states:
            if state.object_id in {event.object_id for event in events}:
                state.status, state.updated_at = "PENDING", now()
        return await document_view(db, row, config)


def _cleanup(document_id, owner, key, *, delay=0):
    instant = now()
    return MemoryDocumentCleanup(id=ascending("doc_cleanup"), document_id=document_id, user_id=owner.user_id,
        workspace_id=owner.workspace_id, project_id=owner.project_id, storage_key=key, status="PENDING",
        attempts=0, available_at=instant + timedelta(seconds=delay), created_at=instant, updated_at=instant)


def _all_revision_sources(db, row):
    """Every chunk made from this document: edits create new chunks and keep the old ones."""
    if db.get_bind().dialect.name == "postgresql":
        document_id = MemorySource.source_metadata.op("->>", return_type=String)(literal("document_id", String))
    else:
        document_id = func.json_extract(MemorySource.source_metadata, "$.document_id")
    return select(MemorySource).where(MemorySource.user_id == row.user_id, MemorySource.workspace_id == row.workspace_id,
        or_(MemorySource.id.in_(list(row.source_ids or [])),
            (MemorySource.source_kind == "document_chunk") & (document_id == row.id)))


async def delete(*, user_id, workspace_id, document_id, store=None):
    """Remove an uploaded file and everything built from it, on the owner's request.

    The original, its parsed text and the chunk text of every revision are
    deleted; the pages made from it leave the library and recall; search-index
    entries are withdrawn. Chats, memories and other files are not touched.
    Removing the original from storage is recorded in the same transaction
    and retried until done, so "pending" means it is still in storage.
    """
    config = runtime_config.get_config().memory
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        row = await owned(db, user_id, workspace_id, document_id, lock=True)
        if not row:
            return None
        instant, page_ids = now(), list(row.page_ids or [])
        sources = (await db.scalars(_all_revision_sources(db, row))).all()
        source_ids = [source.id for source in sources]
        for source in sources:
            if source.deleted_at:
                continue
            source.status, source.deleted_at, source.body, source.source_metadata = "DELETED", instant, None, {}
            source.acl_epoch += 1
            await enqueue_source(db, source, config, operation="DELETE")
        from memory.wiki.service import invalidate_memory_dependencies
        # Any page citing these chunks stops serving their text immediately.
        await invalidate_memory_dependencies(db, source_ids=source_ids, reason="document_deleted")
        pages = (await db.scalars(select(MemoryWikiPage).where(MemoryWikiPage.id.in_(page_ids)))).all() if page_ids else []
        for page in pages:
            published = page.status == "PUBLISHED"
            page.body, page.paragraphs, page.title = None, [], "deleted"
            page.status, page.invalidation_reason, page.deleted_at, page.updated_at = "STALE", "document_deleted", instant, instant
            if published:
                await enqueue_page_outbox(db, page, config, operation="DELETE")
        await db.execute(delete_rows(MemoryDocumentRevision).where(MemoryDocumentRevision.document_id == row.id))
        # The worker picks this up only if the attempt below fails or never runs.
        cleanup = _cleanup(row.id, row, row.storage_key, delay=60)
        db.add(cleanup)
        cleanup_id = cleanup.id
        await db.delete(row)
    removed = await remove_original(cleanup_id, store=store)
    return {"ok": True, "status": "deleted", "original_cleanup": "done" if removed else "pending"}


async def _delete_object(store, key):
    await store.delete(key)
    # Some storage clients swallow every delete error; confirm the object is gone.
    exists = getattr(store, "exists", None)
    if exists and await exists(key):
        raise DocumentError("document_original_still_present")


async def remove_original(cleanup_id, *, store=None) -> bool:
    """Remove one deleted document's original file; False leaves it for a later retry."""
    async with get_db_session() as db:
        job = await db.get(MemoryDocumentCleanup, cleanup_id)
        if job is None or job.status != "PENDING":
            return True
        key = job.storage_key
        # Older uploads shared one key per file; never remove one a live document uses.
        in_use = await db.scalar(select(MemoryDocument.id).where(MemoryDocument.storage_key == key).limit(1))
    try:
        if not in_use:
            if store:
                await _delete_object(store, key)
            else:
                async with document_storage() as blob:
                    await _delete_object(blob, key)
    except Exception as exc:
        log.warning("Original file cleanup deferred error_type=%s", type(exc).__name__)
        async with get_db_session() as db:
            job = await db.get(MemoryDocumentCleanup, cleanup_id)
            if job is not None and job.status == "PENDING":
                job.attempts += 1
                job.last_error = (str(exc) if isinstance(exc, DocumentError) else type(exc).__name__)[:80]
                job.available_at = now() + timedelta(seconds=min(3600, 30 * 2 ** min(job.attempts, 7)))
                job.updated_at = now()
        return False
    async with get_db_session() as db:
        job = await db.get(MemoryDocumentCleanup, cleanup_id)
        if job is not None:
            job.status, job.last_error, job.updated_at = "SUCCEEDED", None, now()
    return True


async def retry_original_cleanups(*, store=None, limit=5) -> int:
    """Finish removals that failed or were interrupted; runs from the document worker."""
    async with get_db_session() as db:
        due = list((await db.scalars(select(MemoryDocumentCleanup.id).where(
            MemoryDocumentCleanup.status == "PENDING", MemoryDocumentCleanup.available_at <= now())
            .order_by(MemoryDocumentCleanup.available_at, MemoryDocumentCleanup.id).limit(limit))).all())
    return sum([await remove_original(cleanup_id, store=store) for cleanup_id in due])


async def enqueue_source(db, source, config, *, operation="UPSERT"):
    generations = set((await db.scalars(select(MemoryIndexState.index_generation).where(
        MemoryIndexState.object_kind == "source", MemoryIndexState.object_id == source.id))).all())
    generations.add(config.index_generation)
    for generation in generations:
        event_id = "document:" + canonical_hash([source.id, source.source_revision, generation, operation])
        if not await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.event_id == event_id)):
            db.add(MemoryOutbox(id=ascending("memory_outbox"), event_id=event_id, user_id=source.user_id,
                workspace_id=source.workspace_id, project_id=source.project_id, object_kind="source", object_id=source.id,
                revision=source.source_revision, operation=operation, index_generation=generation, status="PENDING",
                attempts=0, lease_generation=0, priority=90 if operation != "UPSERT" else 0,
                available_at=now(), created_at=now(), updated_at=now(), payload={}))
        state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_kind == "source",
            MemoryIndexState.object_id == source.id, MemoryIndexState.index_generation == generation))
        if not state:
            state = MemoryIndexState(id=ascending("memory_index"), object_kind="source", object_id=source.id,
                index_generation=generation, user_id=source.user_id, workspace_id=source.workspace_id,
                project_id=source.project_id, indexed_revision=0, chunk_ids=[])
            db.add(state)
        state.desired_revision, state.status, state.updated_at = source.source_revision, "PENDING", now()


async def edit_section(db, scope, page, title, entries, config):
    from memory.documents.authority import read_dependency
    ref = next(r for r in page.memory_manifest if r["kind"] == "document")
    document, previous = await read_dependency(db, scope, ref, lock=True)
    index = document.page_ids.index(page.id)
    entry = entries[0]
    if len(entries) != 1 or entry["id"] != document.id or entry["revision"] != document.revision:
        raise WikiStateError("wiki_edit_changed")
    sections = [dict(s) for s in previous.sections]
    sections[index] = {**sections[index], "body": entry["text"], "title": title, "edited": True}
    document.revision += 1
    document.content_hash, document.status, document.available_at = canonical_hash(sections), "PENDING", now()
    document.attempts, document.reason_code, document.updated_at = 0, None, now()
    db.add(MemoryDocumentRevision(id=ascending("doc_revision"), document_id=document.id, revision=document.revision,
        content_hash=document.content_hash, sections=sections, metadata_=previous.metadata_, origin="user_edit", created_at=now()))
    sources = (await db.scalars(select(MemorySource).where(MemorySource.id.in_(document.source_ids)))).all()
    for source in sources:
        await enqueue_source(db, source, config, operation="REVOKE")
    for item in (await db.scalars(select(MemoryWikiPage).where(MemoryWikiPage.id.in_(document.page_ids)))).all():
        item.status, item.invalidation_reason, item.updated_at = "STALE", "document_edited", now()
        item.revision += 1
        await enqueue_page_outbox(db, item, config, operation="REVOKE")
