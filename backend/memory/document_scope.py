"""SQL-bounded uploaded-document reads, with no access to personal history.

The current utterance's semantic contract grants the document category and,
when present, an upload-time range. Generated search text and source IDs can
only narrow it. Unknown dates fail closed instead of broadening the read.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from db.base import get_db_session
from db.models.memory_document import MemoryDocument
from db.models.memory_v2 import MemorySource
from memory.redaction import redact_credentials


async def _scope(db, turn, config):
    from memory.readers import authority
    from memory.time_context import query_time_context
    scope = await authority(db, turn)
    decision = turn.time_intent or {}
    temporal = turn.read_time_context or await query_time_context(db, turn.user_id, turn.query, config, intent=decision)
    choice, confidence = decision.get("choice"), decision.get("confidence", 0)
    # `none` is an explicit absence of a time condition, not an uncertain
    # date estimate. Some typed dialogue plans use zero for a non-applicable
    # confidence. Actual date interpretations still need strong evidence.
    if (choice not in {"none", "event_time", "source_time"} or choice != "none" and confidence < .9
            or choice == "source_time" and not temporal.get("hard_filter_applied")):
        return scope, None, temporal
    predicates = [*scope.predicates(MemoryDocument), MemoryDocument.status != "DELETED"]
    if choice == "source_time":
        # Upload time belongs to the document, not its subsequent ingestion,
        # indexing, editing, or the events described in its body.
        for name, lower in (("start_at", True), ("end_at", False)):
            instant = datetime.fromisoformat(temporal[name]).astimezone(timezone.utc).replace(tzinfo=None)
            predicates.append(MemoryDocument.created_at >= instant if lower else MemoryDocument.created_at < instant)
    return scope, predicates, temporal


async def read_documents(request, turn, config):
    if request.kind not in {"documents", "sources", "search"}:
        return {"kind": request.kind, "status": "restricted", "items": []}
    async with get_db_session() as db:
        scope, predicates, temporal = await _scope(db, turn, config)
        if predicates is None:
            return {"kind": request.kind, "status": "unavailable", "reason": "document_time_scope_unresolved",
                    "items": [], "time_context": temporal}
        if request.kind in {"documents", "search"}:
            # A bounded inventory gives exact names/IDs without mixing Wiki
            # summaries or unrelated chat. Reading bodies is an exact second
            # step; a rewritten query never changes the authorized date range.
            stmt = select(MemoryDocument).where(*predicates)
            rows = list((await db.scalars(stmt.order_by(MemoryDocument.created_at.desc(), MemoryDocument.id)
                .offset(request.offset).limit(21))).all())
            return {"kind": request.kind, "status": "complete", "time_context": temporal,
                "items": [{"id": row.id, "revision": row.revision, "filename": row.filename,
                    "project_id": row.project_id, "status": row.status, "uploaded_at": row.created_at.isoformat(),
                    "sources": [{"id": identity, "revision": row.revision} for identity in row.source_ids]}
                    for row in rows[:20]],
                "next_offset": request.offset + 20 if len(rows) > 20 else None,
                "instruction": "Only uploaded documents are authorized. This is a paginated inventory, not a relevance search. Read exact sources with memory_read_more; no matching file on this page does not prove absence."}
        from memory.service import read_source_in_scope
        items = []
        for ref in request.refs[:8]:
            # Read only the source's linkage before its body. A guessed ID
            # cannot escape owner/workspace/project/upload-time or revision.
            metadata = await db.scalar(select(MemorySource.source_metadata).where(
                *scope.predicates(MemorySource), MemorySource.id == ref["id"],
                MemorySource.source_kind == "document_chunk"))
            document = await db.scalar(select(MemoryDocument).where(*predicates,
                MemoryDocument.id == (metadata or {}).get("document_id"))) if metadata else None
            if document is None:
                items.append({"id": ref["id"], "revision": ref["revision"], "available": False})
                continue
            value = await read_source_in_scope(db, access=scope, source_id=ref["id"], source_revision=ref["revision"])
            if not value.get("available"):
                items.append({"id": ref["id"], "revision": ref["revision"], "available": False})
                continue
            body = value.pop("body", "") or ""
            end = min(len(body), request.offset + request.max_chars)
            items.append({**value, "revision": ref["revision"], "filename": document.filename,
                "text": redact_credentials(body[request.offset:end]), "offset": request.offset,
                "next_offset": end if end < len(body) else None, "total_chars": len(body)})
        return {"kind": request.kind, "status": "complete", "items": items, "time_context": temporal}
