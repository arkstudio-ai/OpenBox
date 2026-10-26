"""Hybrid retrieval with SQL authorization before every model data boundary."""
from array import array
import asyncio
import hashlib
import json
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select

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


# Parameters per IN (...) list; well under every supported SQLite/PostgreSQL limit.
_IN_BATCH = 500
# Keyword candidates per kind that go on to full authorization. RRF then keeps
# candidate_limit_per_kind of them; the margin absorbs candidates that fail
# authorization or the time filter.
LEXICAL_POOL_FACTOR = 2


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
    The per-source checks are unchanged; their inputs are read for the whole
    batch at once, so the cost no longer grows by several queries per source.
    """
    from memory.service import source_authority
    async with source_authority(db, scope):
        return await _authorized_documents(db, scope, config, only=only)


async def _authorized_documents(db, scope, config, *, only):
    from memory.service import prefetch_source_facts
    if only is not None and not only:
        return []
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
    supporting = defaultdict(list)
    for start in range(0, len(rows), _IN_BATCH):
        batch = {row.id: row.revision for row in rows[start:start + _IN_BATCH]}
        for link, source in (await db.execute(select(MemorySourceLink, MemorySource).join(MemorySource,
                MemorySource.id == MemorySourceLink.source_id).where(MemorySourceLink.memory_id.in_(batch),
                MemorySourceLink.relation == "SUPPORTS").order_by(MemorySourceLink.id))).all():
            if batch[link.memory_id] == link.revision:
                supporting[link.memory_id].append((link, source))
    await prefetch_source_facts(db, scope, [source for links in supporting.values() for _, source in links])
    documents, source_docs = [], {}
    for row in rows:
        links = supporting[row.id]
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
    if config.enabled("wiki", scope.actor_user_id) and (only is None or any(kind == "source" for kind, _ in only)):
        from memory.documents.authority import authorized_chunks
        documents.extend(await authorized_chunks(db, scope, config, only=only))
    if config.enabled("wiki", scope.actor_user_id) and (only is None or any(kind == "wiki" for kind, _ in only)):
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


async def _wiki_pool(db, scope) -> list[tuple[str, str]]:
    """Published page texts in scope, newest first (the full scan's page limit)."""
    from db.models.memory_wiki import MemoryWikiPage
    return list((await db.execute(select(MemoryWikiPage.id, MemoryWikiPage.body).where(
        *scope.predicates(MemoryWikiPage), MemoryWikiPage.status == "PUBLISHED", MemoryWikiPage.deleted_at.is_(None),
        MemoryWikiPage.body.is_not(None)).order_by(MemoryWikiPage.updated_at.desc()).limit(100))).all())


async def lexical_candidates(db, scope, config, query, time_context) -> tuple[dict, int]:
    """Rank cheap SQL texts by keyword, inside the caller's own scope.

    Only the best few per kind go on to full authorization; nothing read here is
    returned, shown or sent anywhere before that. The candidate set is the same
    as the full scan's (active memories, their supporting sources, ready file
    chunks, published pages), so the cost is a handful of queries however much
    a person has stored. Returns ranked (id, score) lists per kind, pool size.
    """
    from db.models.memory_document import MemoryDocument
    limit, scan = config.candidate_limit_per_kind * LEXICAL_POOL_FACTOR, config.lexical_scan_limit
    timed = None
    if time_context.get("hard_filter_applied"):
        # A superset of the exact filter applied after authorization: a day of
        # slack covers storage time zones, and an unknown chat time stays in
        # because it may still be derived from the original message.
        start = datetime.fromisoformat(time_context["start_at"]).astimezone(timezone.utc) - timedelta(days=1)
        end = datetime.fromisoformat(time_context["end_at"]).astimezone(timezone.utc) + timedelta(days=1)
        timed = or_(and_(MemorySource.occurred_at >= start, MemorySource.occurred_at < end),
                    and_(MemorySource.occurred_at.is_(None), MemorySource.source_kind == "user_statement"))
    memory_stmt = select(UserMemory.id, UserMemory.value).where(*scope.predicates(UserMemory), *active_memory_predicates())
    if timed is not None:
        memory_stmt = memory_stmt.where(UserMemory.id.in_(select(MemorySourceLink.memory_id).join(
            MemorySource, MemorySource.id == MemorySourceLink.source_id).where(MemorySourceLink.relation == "SUPPORTS", timed)))
    memories = [(row_id, value.get("summary", "") if isinstance(value, dict) else "") for row_id, value in
                (await db.execute(memory_stmt.order_by(UserMemory.updated_at.desc(), UserMemory.id).limit(scan))).all()]
    # A personal memory can rest on words said in another project; those words
    # are not candidates here (source_body_is_available keeps them out anyway).
    source_stmt = select(MemorySource.id, MemorySource.body).join(MemorySourceLink,
        MemorySourceLink.source_id == MemorySource.id).join(UserMemory, UserMemory.id == MemorySourceLink.memory_id).where(
        *scope.predicates(UserMemory), *scope.predicates(MemorySource), *active_memory_predicates(),
        MemorySourceLink.revision == UserMemory.revision,
        MemorySourceLink.relation == "SUPPORTS", MemorySourceLink.source_revision == MemorySource.source_revision,
        MemorySource.status == "ACTIVE", MemorySource.deleted_at.is_(None), MemorySource.body.is_not(None))
    if timed is not None:
        source_stmt = source_stmt.where(timed)
    sources = dict((await db.execute(source_stmt.order_by(UserMemory.updated_at.desc(), MemorySource.id).limit(scan))).all())
    pages = []
    if config.enabled("wiki", scope.actor_user_id):
        chunk_ids = [i for (ids,) in (await db.execute(select(MemoryDocument.source_ids).where(
            *scope.predicates(MemoryDocument), MemoryDocument.status == "READY").order_by(
            MemoryDocument.updated_at.desc(), MemoryDocument.id).limit(scan))).all() for i in ids or []][:scan]
        for start in range(0, len(chunk_ids), _IN_BATCH):
            sources.update((await db.execute(select(MemorySource.id, MemorySource.body).where(
                *scope.predicates(MemorySource), MemorySource.source_kind == "document_chunk",
                MemorySource.id.in_(chunk_ids[start:start + _IN_BATCH]), MemorySource.status == "ACTIVE",
                MemorySource.deleted_at.is_(None), MemorySource.body.is_not(None)))).all())
        pages = await _wiki_pool(db, scope)
    pools = {"memory": memories, "source": list(sources.items()), "wiki": [tuple(row) for row in pages]}
    return rank_lexical_pool(query, pools, limit), sum(len(pool) for pool in pools.values())


def rank_lexical_pool(query, pools, limit):
    """Use one corpus so BM25 scores are comparable across storage kinds."""
    documents = [(kind, doc_id, text or "") for kind, pool in pools.items() for doc_id, text in pool]
    ranked = {kind: [] for kind in pools}
    for position, score in bm25(query, [text for _, _, text in documents]):
        kind, doc_id, _ = documents[position]
        if len(ranked[kind]) < limit:
            ranked[kind].append((doc_id, score))
    return ranked


async def _has_searchable_content(db, scope, config) -> bool:
    """Cheap scoped existence check before starting a billable query embedding.

    This is not content authorization. All candidates still pass the full
    source/version checks after the parallel keyword and vector searches.
    """
    predicates = [select(UserMemory.id).where(
        *scope.predicates(UserMemory), *active_memory_predicates()).exists()]
    if config.enabled("wiki", scope.actor_user_id):
        from db.models.memory_document import MemoryDocument
        from db.models.memory_wiki import MemoryWikiPage
        predicates.extend([
            select(MemoryDocument.id).where(*scope.predicates(MemoryDocument),
                MemoryDocument.status == "READY").exists(),
            select(MemoryWikiPage.id).where(*scope.predicates(MemoryWikiPage),
                MemoryWikiPage.status == "PUBLISHED", MemoryWikiPage.deleted_at.is_(None),
                MemoryWikiPage.body.is_not(None)).exists(),
        ])
    return bool(await db.scalar(select(or_(*predicates))))


def _item(document, scores):
    return {**document_item(document), "conflict_status": None, **scores}


# Repeated questions (quick prompts, retries) need not wait for the same
# embedding again. Only the default provider is cached; vectors stay in memory.
QUERY_VECTOR_CACHE_SIZE = 128
_query_vector_cache: "OrderedDict[tuple, array]" = OrderedDict()


async def _query_vectors(embedding, query: str, config) -> tuple[list[list[float]], dict]:
    if embedding is not None:
        return await embedding.embed([query])
    key = (config.embedding_url, config.embedding_model, config.embedding_dimensions, query)
    cached = _query_vector_cache.get(key)
    if cached is not None:
        _query_vector_cache.move_to_end(key)
        return [list(cached)], {"total_tokens": 0, "model": config.embedding_model, "cached": True,
                                "currency": config.embedding_currency, "price_version": config.price_version,
                                "estimated_cost": 0.0}
    vectors, usage = await BailianEmbedding(config).embed([query])
    _query_vector_cache[key] = array("d", vectors[0])
    while len(_query_vector_cache) > QUERY_VECTOR_CACHE_SIZE:
        _query_vector_cache.popitem(last=False)
    return vectors, usage


async def search_memory(*, query: str, user_id: str, workspace_id: str | None = None,
                        project_id: str | None = None, config=None, limit: int | None = None,
                        request_id: str | None = None, include_all_projects=False,
                        force_rerank=False, index=None, embedding=None,
                        document_loader=None, kinds=None, io_budget_seconds=None,
                        prefer_document_source=False, diagnostics=None, time_query=None, time_intent=None,
                        include_background=False, on_background=None, on_checkpoint=None) -> dict:
    from core.config import get_config
    from core.identifier import ascending
    config = config or get_config().memory
    kinds = tuple(kinds) if kinds is not None else ("memory", "source", "wiki")
    if not kinds or len(set(kinds)) != len(kinds) or set(kinds) - {"memory", "source", "wiki"}:
        raise ValueError("Invalid memory retrieval kinds")
    load_documents = document_loader or authorized_documents
    request_id = request_id or ascending("memoryrequest")
    query = query.strip()[:config.route_input_max_chars]
    if not query:
        raise ValueError("query must not be empty")
    started = time.monotonic()
    io_deadline = started + io_budget_seconds if io_budget_seconds is not None else None

    async def bounded_io(operation, *, network=False):
        from agent.latency import provider_wait, network_wait
        remaining = None if io_deadline is None else io_deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            # Do not dispatch (or construct a gather of) requests after their
            # budget expired. One millisecond can still send a billable call.
            raise TimeoutError
        with network_wait() if network else provider_wait():
            if remaining is None:
                return await operation()
            return await asyncio.wait_for(operation(), timeout=remaining)
    # Milliseconds per stage, kept with the debug step: where a slow recall waited.
    timings, mark = {}, [started]
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(stage="keywords", timings=timings)

    def lap(stage):
        now = time.monotonic()
        timings[stage] = round((now - mark[0]) * 1000)
        mark[0] = now

    degraded, usage, dense_hits = [], {}, {}
    background_ids = []
    per_kind = config.candidate_limit_per_kind

    async def dense_search(scope):
        try:
            diagnostics["stage"] = "embedding"
            before = time.monotonic()
            vectors, usage["query_embedding"] = await bounded_io(lambda: _query_vectors(embedding, query, config))
            timings["embedding"] = round((time.monotonic() - before) * 1000)
            adapter = index or QdrantMemoryIndex(config)
            diagnostics["stage"] = "index"
            before = time.monotonic()
            results = await bounded_io(lambda: asyncio.gather(*(adapter.search(vectors[0], scope, kind=kind, limit=per_kind)
                for kind in kinds), return_exceptions=True), network=True)
            timings["index"] = round((time.monotonic() - before) * 1000)
            for kind, hits in zip(kinds, results, strict=True):
                if isinstance(hits, Exception):
                    degraded.append(hits.code if isinstance(hits, MemoryProviderError) else "dense_unavailable")
                    continue
                dense_hits[kind] = hits
        except MemoryProviderError as exc:
            degraded.append(exc.code)
        except TimeoutError:
            degraded.append("dense_deadline")

    dense_task = None
    try:
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
                project_id=project_id, include_all_projects=include_all_projects)
            time_context = await query_time_context(db, user_id, query if time_query is None else time_query,
                                                    config, intent=time_intent)
            diagnostics["time_context"] = time_context
            if include_background:
                from memory.orchestrator import core_memory_candidates
                background_ids = await core_memory_candidates(db, scope,
                    include_style=config.enabled("semantic_recall", scope.user_id))
            pooled = {}
            if document_loader is None:
                # Only the current query goes to the embedding provider. After
                # membership and scoped existence are checked, keyword SQL need
                # not wait for this independent network round trip (or vice versa).
                if config.enabled("retrieval_v2", user_id) and await _has_searchable_content(db, scope, config):
                    dense_task = asyncio.create_task(dense_search(scope))
                # Keyword candidates are authorized after the network waits below,
                # in the same read as the dense hits.
                lexical, _ = await lexical_candidates(db, scope, config, query, time_context)
                lexical_keys = {(kind, doc_id) for kind, ranked in lexical.items() if kind in kinds for doc_id, _ in ranked}
            else:
                # A stricter consumer can authorize complete source lineages before
                # even keyword ranking. SQL remains authoritative after dense IO too.
                pool = [doc for doc in await load_documents(db, scope, config) if doc.kind in kinds]
                lexical = rank_lexical_pool(query,
                    {kind: [(doc.id, doc.text) for doc in pool if doc.kind == kind] for kind in kinds},
                    config.candidate_limit_per_kind * LEXICAL_POOL_FACTOR)
                lexical_keys = {(kind, doc_id) for kind, ranked in lexical.items() for doc_id, _ in ranked}
                pooled = {(doc.kind, doc.id): doc for doc in pool if (doc.kind, doc.id) in lexical_keys}
            lag = await index_lag(db, scope, config.index_generation)
        lap("keywords")
        # Custom loaders can impose stricter lineage checks before any provider
        # call; retain that ordering instead of using the generic existence gate.
        if document_loader is not None and config.enabled("retrieval_v2", user_id):
            dense_task = asyncio.create_task(dense_search(scope))
        if dense_task is not None:
            await dense_task
    finally:
        if dense_task is not None:
            if not dense_task.done():
                dense_task.cancel()
            await asyncio.gather(dense_task, return_exceptions=True)
    # Keyword and embedding/index durations overlap. Start the next sequential
    # stage after both finish; do not count the network wait as authorization.
    mark[0] = time.monotonic()
    scope_summary = {"workspace_id": scope.workspace_id, "project_id": scope.project_id,
                     "visibility": "PERSONAL", "acl_epoch": scope.acl_epoch}
    limit = min(limit or config.retrieval_limit, config.retrieval_limit)
    scores = defaultdict(lambda: {"score": 0.0, "lexical_score": None, "dense_score": None, "rerank_score": None})
    if not config.enabled("retrieval_v2", user_id):
        degraded.append("dense_disabled")
    # One authorized read after the network waits serves keyword and dense
    # candidates alike; it is the current text a reranker may receive.
    wanted = lexical_keys | {(kind, hit.id) for kind, hits in dense_hits.items() for hit in hits}
    background_keys = {("memory", doc_id) for doc_id in background_ids}
    wanted |= background_keys
    diagnostics["stage"] = "authorize"
    current = []
    current_all = []
    current_scope = scope
    if wanted:
        async with get_db_session() as db:
            current_scope = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id,
                project_id=project_id, include_all_projects=include_all_projects)
            current_all = await load_documents(db, current_scope, config, only=wanted)
            current = [doc for doc in current_all
                       if document_matches_time(doc, time_context)]
    from memory.orchestrator import background_snapshot
    background = background_snapshot(background_ids, current_all, config)
    if include_background and on_background:
        # Publish only authorized evidence before optional network ranking. If
        # that call times out, the packet still revalidates this checkpoint
        # before sending anything to the model.
        on_background(background)
    current_by_key = {(doc.kind, doc.id): doc for doc in current}
    lap("authorize")
    # What the stricter loader read before the waits must not have changed since.
    by_identity = {key: doc for key, doc in current_by_key.items()
                   if key not in pooled or pooled[key].revision == doc.revision}
    lexical_ranked = [((kind, doc_id), score) for kind in kinds for doc_id, score in
        [(doc_id, score) for doc_id, score in lexical.get(kind, []) if (kind, doc_id) in by_identity][:per_kind]]
    # Storage kinds are partitions of a channel, not independent votes. A
    # weak wiki hit must not get the same rank as a strong personal fact just
    # because both happen to be first in their own partition.
    for rank, (key, score) in enumerate(sorted(lexical_ranked, key=lambda hit: (-hit[1], hit[0])), 1):
        scores[key]["lexical_score"] = score
        scores[key]["score"] += 1 / (60 + rank)
    dense_ranked = {}
    for kind, hits in dense_hits.items():
        for hit in hits:
            key = (kind, hit.id)
            # Qdrant can have old, delayed or foreign payloads: no text
            # leaves SQL until current ACL/version has been verified.
            document = by_identity.get(key)
            if not document or document.revision != hit.revision:
                continue
            dense_ranked[key] = max(dense_ranked.get(key, float("-inf")), hit.score)
    for rank, (key, score) in enumerate(sorted(dense_ranked.items(), key=lambda hit: (-hit[1], hit[0])), 1):
        scores[key]["dense_score"] = score
        scores[key]["score"] += 1 / (60 + rank)
    diagnostics["fusion"] = "retrieval_channel_rrf_v2"
    keys = [key for key in scores if key in by_identity]
    keys.sort(key=lambda key: (-scores[key]["score"], key))
    if prefer_document_source:
        # Reserve the strongest retrieved primary file BEFORE both candidate
        # caps. Reordering only at packing time is too late: repeated questions
        # and their derived pages can occupy every reranker slot, leaving the
        # actual file unscored and therefore rejected by the relevance filter.
        # This reserves a chance to be scored, not a right to be exposed. Live
        # authority/version checks and the normal relevance threshold still apply.
        primary = next((key for key in keys if by_identity[key].kind == "source"
                        and by_identity[key].category == "DOCUMENT"), None)
        if primary is not None:
            capacity = len(keys)
            if config.enabled("semantic_recall", user_id):
                capacity = min(capacity, config.merged_candidate_limit)
            if config.enabled("rerank", user_id):
                capacity = min(capacity, config.rerank_max_documents)
            if keys.index(primary) >= capacity:
                keys.remove(primary)
                keys.insert(capacity - 1, primary)
    if config.enabled("semantic_recall", user_id):
        keys = keys[:config.merged_candidate_limit]
    # Different channels can still disagree. Resolve ties semantically before
    # deduplicating evidence; fusion alone does not prove relevance.
    tied = [key for key in keys if keys and abs(scores[key]["score"] - scores[keys[0]]["score"]) < 1e-9]
    ambiguous = len({key[0] for key in tied}) > 1 and len({current_by_key[key].text for key in tied}) > 1
    rerank_info = {"called": False, "reason_code": "not_needed", "model": config.rerank_model,
                   "min_score": config.rerank_min_score, "filter_applied": False, "filtered_count": 0}
    rerank_io_started = False
    # Even a single nearest neighbour can be unrelated. When relevance
    # filtering is enabled, small candidate sets need the same check as ties.
    if config.enabled("rerank", user_id) and (force_rerank or ambiguous or len(keys) > 4 or config.rerank_min_score > 0):
        from memory.rerank import rerank
        rerank_keys = keys[:config.rerank_max_documents]
        if rerank_keys:
            try:
                diagnostics["stage"] = "rerank"
                async def rank_current():
                    nonlocal rerank_io_started
                    rerank_io_started = rerank_info["called"] = True
                    return await rerank(query, [current_by_key[key].text for key in rerank_keys], config)
                ranking, usage["rerank"] = await bounded_io(rank_current)
                for position, score in ranking:
                    scores[rerank_keys[position]]["rerank_score"] = score
                keys = [rerank_keys[position] for position, _ in ranking] + keys[len(rerank_keys):]
                rerank_info["reason_code"] = "ambiguity_or_candidate_count" if ambiguous or len(keys) > 4 else "relevance_filter"
                rerank_info["filter_applied"] = config.rerank_min_score > 0
            except MemoryProviderError as exc:
                degraded.append("rerank_" + exc.code)
                rerank_info["reason_code"] = exc.code
            except TimeoutError:
                degraded.append("rerank_deadline")
                rerank_info["reason_code"] = "deadline"
    if rerank_info["called"]:
        lap("rerank")
    def pack(final, background, acl_epoch):
        result_rerank = {**rerank_info, "filtered_count": 0}
        eligible_keys = [key for key in keys if key in final and
                         final[key].revision == current_by_key[key].revision and final[key].text == current_by_key[key].text]
        if prefer_document_source:
            # A semantic knowledge need reserves one relevant primary file before
            # a derived topic page consumes the whole item/character budget. This
            # only reorders already-authorized, currently valid candidates; it must
            # never revive a source rejected by the rerank relevance threshold.
            primary = next((key for key in eligible_keys
                if final[key].kind == "source" and final[key].category == "DOCUMENT"
                and (not rerank_info["filter_applied"] or scores[key]["rerank_score"] is not None
                     and scores[key]["rerank_score"] >= config.rerank_min_score)), None)
            if primary is not None:
                eligible_keys = [primary] + [key for key in eligible_keys if key != primary]
        items, used, trimmed, duplicate_sources, used_evidence = [], 0, 0, set(), set()
        for key in eligible_keys:
            relevance = scores[key]["rerank_score"]
            if rerank_info["filter_applied"] and (relevance is None or relevance < config.rerank_min_score):
                # Keep rejected candidates in ACL-checked diagnostic evidence,
                # but never backfill final results with low or unscored entries.
                result_rerank["filtered_count"] += 1
                continue
            document = final.get(key)
            if not document or document.revision != current_by_key[key].revision:
                continue
            # A raw source and its paraphrased memory are not separate votes.
            identity = document.content_hash
            if identity in duplicate_sources:
                continue
            evidence = {(source["id"], source["revision"]) for source in document.sources}
            # Shared evidence makes a raw source or a page redundant, never a second
            # fact: one message can state a budget and a date, both worth keeping.
            # Shared provenance does not prove shared fact coverage. A source can
            # contain conditions that none of its extracted memories retained.
            if len(items) >= limit or used + len(document.text) > config.context_max_chars:
                trimmed += 1
                continue
            item = _item(document, {**scores[key], "rank": len(items) + 1})
            items.append(item)
            duplicate_sources.add(identity)
            used_evidence.update(evidence)
            used += len(document.text)
        items = _without_repeated_lineage(items)
        used = sum(len(item["text"]) for item in items)
        return {"request_id": request_id, "route_attempt_id": None, "scope": {**scope_summary, "acl_epoch": acl_epoch},
                "items": items, "budget": {"characters": used, "max_characters": config.context_max_chars,
                    "estimated_tokens": (used + 1) // 2, "trimmed": trimmed, "max_items": limit},
                "index_generation": config.index_generation, "lag": lag, "time_context": time_context,
                "degraded_reasons": sorted(set(degraded)), "rerank": result_rerank, "usage": usage,
                "duration_ms": round((time.monotonic() - started) * 1000), "timings": dict(timings),
                **({"stable_background": background} if include_background else {}),
                "candidates": [_item(final[key], {**scores[key], "rank": rank}) for rank, key in enumerate(eligible_keys, 1)]}

    if on_checkpoint is not None:
        # Provisional after ranking: consumers MUST reauthorize before use.
        # Publish synchronously, so cancellation during the final SQL read
        # cannot erase candidates already filtered by relevance and budget.
        checkpoint = pack(current_by_key, background, current_scope.acl_epoch)
        checkpoint["candidates"] = []  # Only final SQL evidence enters diagnostics.
        on_checkpoint(checkpoint)
    # Recheck after every external wait. If no IO occurred after the authorized
    # read, reuse that same pass rather than opening an identical transaction.
    # This is never a cross-request authority cache.
    diagnostics["stage"] = "final_check"
    if rerank_io_started:
        async with get_db_session() as db:
            final_scope = await resolve_access_scope(db, user_id=user_id, workspace_id=scope.workspace_id,
                project_id=project_id, include_all_projects=include_all_projects)
            final_all = await load_documents(db, final_scope, config, only=set(keys) | background_keys)
            final_docs = [doc for doc in final_all if document_matches_time(doc, time_context)]
    else:
        final_scope, final_docs, final_all = current_scope, current, current_all
    background = background_snapshot(background_ids, final_all, config)
    if include_background and on_background:
        on_background(background)
    diagnostics["reused_authorized_pass"] = not rerank_io_started
    final = {(doc.kind, doc.id): doc for doc in final_docs}
    lap("final_check")
    scope_summary["acl_epoch"] = final_scope.acl_epoch
    diagnostics["stage"] = "complete"
    return pack(final, background, final_scope.acl_epoch)



def _without_repeated_lineage(items: list[dict]) -> list[dict]:
    """Deduplicate identical content only; provenance alone cannot prove coverage."""
    seen, kept = set(), []
    for item in items:
        identity = (str(item.get("text", "")).strip(), json.dumps(item.get("storage_scope", {}), sort_keys=True))
        if identity not in seen:
            kept.append(item)
            seen.add(identity)
    return [{**item, "rank": rank} for rank, item in enumerate(kept, 1)]


async def read_task_state(scope, *, session_id=None) -> dict:
    """Current business rows, never an old memory saying a task is finished."""
    from db.models.todo import Todo
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.actor_user_id, workspace_id=scope.workspace_id,
            project_id=scope.project_id, include_all_projects=scope.include_all_projects)
        # Read by tools of ordinary, possibly workspace-visible chats. The
        # private assistant main session stays out: its words feed personal
        # memory, but its task list is not another chat's business state.
        stmt = select(Session).where(*current.predicates(Session, personal_visibility=False),
            Session.is_deleted.is_(False), Session.memory_policy == "standard", Session.kind != "assistant")
        if session_id:
            stmt = stmt.where(Session.id == session_id)
        rows = list((await db.scalars(stmt.order_by(Session.updated_at.desc()).limit(10))).all())
        sessions = []
        for row in rows:
            todo = await db.scalar(select(Todo).where(Todo.session_id == row.id, Todo.user_id == scope.actor_user_id))
            sessions.append({"id": row.id, "title": row.title, "status": row.status,
                             "updated_at": _iso(row.updated_at), "todos": todo.items if todo else []})
    return {"source": "business_sql", "observed_at": datetime.now(timezone.utc).isoformat(), "sessions": sessions}
