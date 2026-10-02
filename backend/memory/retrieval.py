"""Hybrid retrieval with SQL authorization before every model data boundary."""
import asyncio
import hashlib
import time
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, or_, select

from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryIndexState, MemorySource, MemorySourceLink, MemoryTombstone
from db.models.session import Session
from memory.index.base import DocumentSnapshot
from memory.index.embedding import BailianEmbedding
from memory.index.lexical import bm25
from memory.index.qdrant import QdrantMemoryIndex
from memory.policy import active_memory_predicates, resolve_access_scope
from memory.presentation import document_item
from memory.providers.common import MemoryProviderError
from memory.redaction import text_hash
from memory.source_time import source_occurred_at
from memory.time_context import document_matches_time, query_time_context


def _iso(value):
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat() if value is not None else None


async def _source_accessible(db, source, scope) -> bool:
    from memory.service import source_is_available
    return bool(source.body) and await source_is_available(db, scope, source)


async def authorized_documents(db, scope, config, *, only: set[tuple[str, str]] | None = None) -> list[DocumentSnapshot]:
    """Read current objects and immutable, currently readable source versions.

    A source associated only with a candidate/rejected/superseded fact does
    not become an alternate route around candidate admission or corrections.
    """
    stmt = select(UserMemory).where(*scope.predicates(UserMemory), *active_memory_predicates()).order_by(
        UserMemory.updated_at.desc(), UserMemory.id)
    if only is not None:
        requested_memories = [object_id for kind, object_id in only if kind == "memory"]
        requested_sources = [object_id for kind, object_id in only if kind == "source"]
        if requested_sources:
            linked_ids = select(MemorySourceLink.memory_id).where(MemorySourceLink.source_id.in_(requested_sources))
            stmt = stmt.where(or_(UserMemory.id.in_(requested_memories), UserMemory.id.in_(linked_ids)))
        else:
            stmt = stmt.where(UserMemory.id.in_(requested_memories))
    rows = list((await db.scalars(stmt.limit(config.lexical_scan_limit if only is None else max(1, len(only) * 8)))).all())
    documents, source_docs = [], {}
    for row in rows:
        links = (await db.execute(select(MemorySourceLink, MemorySource).join(MemorySource,
            MemorySource.id == MemorySourceLink.source_id).where(MemorySourceLink.memory_id == row.id,
            MemorySourceLink.revision == row.revision, MemorySourceLink.relation == "SUPPORTS"))).all()
        sources, invalid = [], False
        for link, source in links:
            if source.source_revision != link.source_revision or not await _source_accessible(db, source, scope):
                invalid = True
                break
            reference = {"id": source.id, "revision": source.source_revision, "content_hash": source.content_hash,
                         "kind": source.source_kind, "session_id": source.session_id, "message_id": source.message_id,
                         "occurred_at": _iso(await source_occurred_at(db, access=scope, source=source))}
            sources.append(reference)
            from memory.service import source_body_is_available
            if link.relation == "SUPPORTS" and await source_body_is_available(db, scope, source):
                source_docs[source.id] = DocumentSnapshot("source", source.id, source.source_revision, source.body,
                    source.user_id, source.workspace_id, source.project_id, scope.acl_epoch, source.content_hash,
                    (reference,))
        if invalid:
            continue
        summary = row.value.get("summary", "") if isinstance(row.value, dict) else ""
        if not summary:
            continue
        documents.append(DocumentSnapshot("memory", row.id, row.revision, summary, row.user_id, row.workspace_id,
            row.project_id, scope.acl_epoch, text_hash(summary), tuple(sources), _iso(row.valid_from),
            _iso(row.valid_to), _iso(row.ttl), row.confirmation_status, category=row.type))
    documents.extend(source_docs.values())
    if config.enabled("wiki", scope.actor_user_id):
        from memory.documents.authority import authorized_chunks
        documents.extend(await authorized_chunks(db, scope, config, only=only))
    if config.enabled("wiki", scope.actor_user_id):
        try:
            from memory.wiki.service import authorized_wiki_documents
            documents.extend(await authorized_wiki_documents(db, scope, config, only=only))
        except ImportError:
            pass
    if only is not None:
        documents = [doc for doc in documents if (doc.kind, doc.id) in only]
    return documents


async def index_lag(db, scope, generation: str) -> dict:
    now = datetime.now(timezone.utc)
    pending, oldest = (await db.execute(select(func.count(), func.min(MemoryIndexState.updated_at)).where(
        *scope.predicates(MemoryIndexState), MemoryIndexState.index_generation == generation,
        MemoryIndexState.desired_revision > MemoryIndexState.indexed_revision,
        MemoryIndexState.status != "DELETED"))).one()
    if oldest and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return {"pending": pending, "oldest_age_seconds": (now - oldest).total_seconds() if oldest else None}


def _item(document, scores):
    return {**document_item(document), "conflict_status": None, **scores}


async def search_memory(*, query: str, user_id: str, workspace_id: str | None = None,
                        project_id: str | None = None, config=None, limit: int | None = None,
                        request_id: str | None = None, include_all_projects=False,
                        force_rerank=False, index=None, embedding=None) -> dict:
    from core.config import get_config
    from core.identifier import ascending
    config = config or get_config().memory
    request_id = request_id or ascending("memoryrequest")
    query = query.strip()[:config.route_input_max_chars]
    if not query:
        raise ValueError("query must not be empty")
    started = time.monotonic()
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
            project_id=project_id, include_all_projects=include_all_projects)
        documents = await authorized_documents(db, scope, config)
        time_context = await query_time_context(db, user_id, query, config)
        documents = [doc for doc in documents if document_matches_time(doc, time_context)]
        lag = await index_lag(db, scope, config.index_generation)
    scope_summary = {"workspace_id": scope.workspace_id, "project_id": scope.project_id,
                     "visibility": "PERSONAL", "acl_epoch": scope.acl_epoch}
    limit = min(limit or config.retrieval_limit, config.retrieval_limit)
    by_identity = {(doc.kind, doc.id): doc for doc in documents}
    scores = defaultdict(lambda: {"score": 0.0, "lexical_score": None, "dense_score": None, "rerank_score": None})
    degraded, usage = [], {}
    per_kind = config.candidate_limit_per_kind
    for kind in ("memory", "source", "wiki"):
        candidates = [doc for doc in documents if doc.kind == kind]
        ranked = bm25(query, [doc.text for doc in candidates])[:per_kind]
        for rank, (position, score) in enumerate(ranked, 1):
            key = (kind, candidates[position].id)
            scores[key]["lexical_score"] = score
            scores[key]["score"] += 1 / (60 + rank)
    if config.enabled("retrieval_v2", user_id) and documents:
        try:
            vectors, usage["query_embedding"] = await (embedding or BailianEmbedding(config)).embed([query])
            adapter = index or QdrantMemoryIndex(config)
            results = await asyncio.gather(*(adapter.search(vectors[0], scope, kind=kind, limit=per_kind)
                for kind in ("memory", "source", "wiki")), return_exceptions=True)
            dense_keys = {(kind, hit.id) for kind, hits in zip(("memory", "source", "wiki"), results, strict=True)
                          if not isinstance(hits, Exception) for hit in hits}
            if dense_keys:
                async with get_db_session() as db:
                    dense_scope = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id,
                        project_id=project_id, include_all_projects=include_all_projects)
                    dense_docs = await authorized_documents(db, dense_scope, config, only=dense_keys)
                by_identity.update({(doc.kind, doc.id): doc for doc in dense_docs if document_matches_time(doc, time_context)})
            for kind, hits in zip(("memory", "source", "wiki"), results, strict=True):
                if isinstance(hits, Exception):
                    degraded.append(hits.code if isinstance(hits, MemoryProviderError) else "dense_unavailable")
                    continue
                seen = set()
                for hit in hits:
                    key = (kind, hit.id)
                    # Qdrant can have old, delayed or foreign payloads: no text
                    # leaves SQL until current ACL/version has been verified.
                    document = by_identity.get(key)
                    if not document or document.revision != hit.revision or key in seen:
                        continue
                    seen.add(key)
                    scores[key]["dense_score"] = hit.score
                    scores[key]["score"] += 1 / (60 + len(seen))
        except MemoryProviderError as exc:
            degraded.append(exc.code)
    elif not config.enabled("retrieval_v2", user_id):
        degraded.append("dense_disabled")
    # Refresh current rows before an external reranker receives any text.
    async with get_db_session() as db:
        current_scope = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id,
            project_id=project_id, include_all_projects=include_all_projects)
        current = [doc for doc in await authorized_documents(db, current_scope, config, only=set(scores))
                   if document_matches_time(doc, time_context)]
    current_by_key = {(doc.kind, doc.id): doc for doc in current}
    keys = [key for key in scores if key in current_by_key and by_identity[key].revision == current_by_key[key].revision]
    keys.sort(key=lambda key: (-scores[key]["score"], key))
    # RRF ranks are computed in each source channel. With one memory and one
    # derived page both can rank first even when their wording answers very
    # different questions. Resolve that tie before deduplicating evidence.
    tied = [key for key in keys if keys and abs(scores[key]["score"] - scores[keys[0]]["score"]) < 1e-9]
    ambiguous = len({key[0] for key in tied}) > 1 and len({current_by_key[key].text for key in tied}) > 1
    rerank_info = {"called": False, "reason_code": "not_needed", "model": config.rerank_model,
                   "min_score": config.rerank_min_score, "filter_applied": False, "filtered_count": 0}
    # Even a single nearest neighbour can be unrelated. When relevance
    # filtering is enabled, small candidate sets need the same check as ties.
    if config.enabled("rerank", user_id) and (force_rerank or ambiguous or len(keys) > 4 or config.rerank_min_score > 0):
        from memory.rerank import rerank
        rerank_keys = keys[:config.rerank_max_documents]
        if rerank_keys:
            try:
                rerank_info["called"] = True
                ranking, usage["rerank"] = await rerank(query, [current_by_key[key].text for key in rerank_keys], config)
                for position, score in ranking:
                    scores[rerank_keys[position]]["rerank_score"] = score
                keys = [rerank_keys[position] for position, _ in ranking] + keys[len(rerank_keys):]
                rerank_info["reason_code"] = "ambiguity_or_candidate_count" if ambiguous or len(keys) > 4 else "relevance_filter"
                rerank_info["filter_applied"] = config.rerank_min_score > 0
            except MemoryProviderError as exc:
                degraded.append("rerank_" + exc.code)
                rerank_info["reason_code"] = exc.code
    # Final bundle ACL and revision checkpoint, after network waits.
    async with get_db_session() as db:
        final_scope = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id,
            project_id=project_id, include_all_projects=include_all_projects)
        final_docs = [doc for doc in await authorized_documents(db, final_scope, config, only=set(keys))
                      if document_matches_time(doc, time_context)]
    final = {(doc.kind, doc.id): doc for doc in final_docs}
    scope_summary["acl_epoch"] = final_scope.acl_epoch
    eligible_keys = [key for key in keys if key in final and
                     final[key].revision == current_by_key[key].revision and final[key].text == current_by_key[key].text]
    items, used, trimmed, duplicate_sources, used_evidence = [], 0, 0, set(), set()
    for key in eligible_keys:
        relevance = scores[key]["rerank_score"]
        if rerank_info["filter_applied"] and (relevance is None or relevance < config.rerank_min_score):
            # Keep rejected candidates in ACL-checked diagnostic evidence,
            # but never backfill final results with low or unscored entries.
            rerank_info["filtered_count"] += 1
            continue
        document = final.get(key)
        if not document or document.revision != current_by_key[key].revision:
            continue
        # A raw source and its paraphrased memory are not separate votes.
        identity = document.content_hash
        if identity in duplicate_sources:
            continue
        evidence = {(source["id"], source["revision"]) for source in document.sources}
        if evidence and evidence <= used_evidence:
            continue
        if len(items) >= limit or used + len(document.text) > config.context_max_chars:
            trimmed += 1
            continue
        item = _item(document, {**scores[key], "rank": len(items) + 1})
        items.append(item)
        duplicate_sources.add(identity)
        used_evidence.update(evidence)
        used += len(document.text)
    return {"request_id": request_id, "route_attempt_id": None, "scope": scope_summary,
            "items": items, "budget": {"characters": used, "max_characters": config.context_max_chars,
                "estimated_tokens": (used + 1) // 2, "trimmed": trimmed, "max_items": limit},
            "index_generation": config.index_generation, "lag": lag, "time_context": time_context,
            "degraded_reasons": sorted(set(degraded)), "rerank": rerank_info, "usage": usage,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "candidates": [_item(final[key], {**scores[key], "rank": rank}) for rank, key in enumerate(eligible_keys, 1)]}


async def read_task_state(scope, *, session_id=None) -> dict:
    """Current business rows, never an old memory saying a task is finished."""
    from db.models.todo import Todo
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.actor_user_id, workspace_id=scope.workspace_id,
            project_id=scope.project_id, include_all_projects=scope.include_all_projects)
        stmt = select(Session).where(*current.predicates(Session), Session.is_deleted.is_(False))
        if session_id:
            stmt = stmt.where(Session.id == session_id)
        rows = list((await db.scalars(stmt.order_by(Session.updated_at.desc()).limit(10))).all())
        sessions = []
        for row in rows:
            todo = await db.scalar(select(Todo).where(Todo.session_id == row.id, Todo.user_id == scope.actor_user_id))
            sessions.append({"id": row.id, "title": row.title, "status": row.status,
                             "updated_at": _iso(row.updated_at), "todos": todo.items if todo else []})
    return {"source": "business_sql", "observed_at": datetime.now(timezone.utc).isoformat(), "sessions": sessions}
