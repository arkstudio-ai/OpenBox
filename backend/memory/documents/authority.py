"""Document versions authorize chunk retrieval and Wiki publication together."""
from sqlalchemy import and_, or_, select

from db.models.memory_document import MemoryDocument, MemoryDocumentRevision
from db.models.memory_v2 import MemorySource
from memory.index.base import DocumentSnapshot
from memory.wiki.service import WikiStateError
from wiki_compiler.hashing import canonical_hash

_IN_BATCH = 200


def _revision_key(scope, document_id) -> tuple:
    return (document_id, scope.user_id, scope.workspace_id, scope.project_id, scope.include_all_projects, scope.acl_epoch)


def _revision_valid(document, revision) -> bool:
    return (revision is not None and revision.content_hash == document.content_hash
            and canonical_hash(revision.sections) == document.content_hash)


async def current_revision(db, scope, document_id, *, lock=False):
    # The session ends before every provider boundary. Immutable revision
    # validation is shared by its chunks within this one SQL transaction.
    key = _revision_key(scope, document_id)
    cached = db.info.setdefault("document_revisions", {}).get(key)
    if not lock and cached and cached[0].status == "READY" and cached[0].revision == cached[1].revision and cached[0].content_hash == cached[1].content_hash:
        return cached
    stmt = select(MemoryDocument).where(*scope.predicates(MemoryDocument), MemoryDocument.id == document_id)
    document = await db.scalar(stmt.with_for_update() if lock else stmt)
    if not document or document.status != "READY":
        raise WikiStateError("document_unavailable")
    revision = await db.scalar(select(MemoryDocumentRevision).where(
        MemoryDocumentRevision.document_id == document.id, MemoryDocumentRevision.revision == document.revision))
    if not _revision_valid(document, revision):
        raise WikiStateError("document_revision_changed")
    db.info["document_revisions"][key] = (document, revision)
    return document, revision


async def prefetch_revisions(db, scope, document_ids) -> None:
    """Validate many documents' current revisions in two statements.

    For read-only passes: the rows and checks are current_revision's own, and
    only documents that pass are cached, so one that fails is still read and
    rejected by current_revision itself.
    """
    cache = db.info.setdefault("document_revisions", {})
    missing = sorted({document_id for document_id in document_ids
                      if isinstance(document_id, str) and _revision_key(scope, document_id) not in cache})
    for start in range(0, len(missing), _IN_BATCH):
        documents = (await db.scalars(select(MemoryDocument).where(*scope.predicates(MemoryDocument),
            MemoryDocument.id.in_(missing[start:start + _IN_BATCH]), MemoryDocument.status == "READY"))).all()
        if not documents:
            continue
        current = {(document.id, document.revision): document for document in documents}
        for revision in (await db.scalars(select(MemoryDocumentRevision).where(or_(*(and_(
                MemoryDocumentRevision.document_id == document_id, MemoryDocumentRevision.revision == number)
                for document_id, number in current))))).all():
            document = current[(revision.document_id, revision.revision)]
            if _revision_valid(document, revision):
                cache[_revision_key(scope, document.id)] = (document, revision)


async def source_available(db, scope, source):
    metadata = source.source_metadata or {}
    try:
        document, _ = await current_revision(db, scope, metadata.get("document_id"))
    except WikiStateError:
        return False
    return (source.id in document.source_ids and source.source_revision == document.revision
            and metadata.get("document_hash") == document.content_hash)


async def read_dependency(db, scope, reference, *, lock=False):
    document, revision = await current_revision(db, scope, reference["id"], lock=lock)
    if (document.revision != reference["revision"] or document.content_hash != reference["content_hash"]
            or not set(reference["source_ids"]).issubset(document.source_ids)):
        raise WikiStateError("document_revision_changed")
    return document, revision


async def authorized_chunks(db, scope, config, *, only=None):
    from memory.service import source_body_is_available
    if only is None:
        documents = (await db.scalars(select(MemoryDocument).where(*scope.predicates(MemoryDocument),
            MemoryDocument.status == "READY").order_by(MemoryDocument.updated_at.desc(), MemoryDocument.id)
            .limit(config.lexical_scan_limit))).all()
        ids = [i for d in documents for i in d.source_ids][:config.lexical_scan_limit]
    else:
        ids = [i for kind, i in only if kind == "source"]
    rows = (await db.scalars(select(MemorySource).where(*scope.predicates(MemorySource),
        MemorySource.source_kind == "document_chunk", MemorySource.id.in_(ids))
        .order_by(MemorySource.id))).all()
    from memory.service import prefetch_source_facts
    await prefetch_source_facts(db, scope, rows)
    result = []
    for source in rows:
        if not await source_body_is_available(db, scope, source):
            continue
        metadata = source.source_metadata
        ref = {"id": source.id, "revision": source.source_revision, "content_hash": source.content_hash,
            "kind": "document_chunk", "document_id": metadata["document_id"], "filename": metadata["filename"],
            "page_id": metadata["page_id"], "start": metadata["start"], "end": metadata["end"],
            "original_pages": metadata.get("original_pages", [])}
        result.append(DocumentSnapshot("source", source.id, source.source_revision, source.body,
            source.user_id, source.workspace_id, source.project_id, scope.acl_epoch, source.content_hash,
            (ref,), confirmation_status="UPLOADED_DOCUMENT", category="DOCUMENT"))
    return result
