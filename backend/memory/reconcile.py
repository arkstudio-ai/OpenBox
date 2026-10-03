"""SQL-led expiry, deletion verification and scoped index generation rebuild."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import aliased

from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_runtime import MemoryIndexGeneration
from db.models.memory_v2 import MemoryIndexState, MemoryOutbox, MemorySource, MemorySourceLink, MemoryTombstone
from memory.index.qdrant import QdrantMemoryIndex, config_hash
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.providers.common import MemoryProviderError


def _now():
    return datetime.now(timezone.utc)


async def expire_memories(config):
    from memory.service import _cas, _revision, enqueue_memory_outbox
    async with get_db_session() as db:
        stmt = select(UserMemory).where(UserMemory.status.in_(["ACTIVE", "CANDIDATE"]),
            UserMemory.ttl.is_not(None), UserMemory.ttl <= _now()).limit(100)
        if config.allowed_user_ids:
            stmt = stmt.where(UserMemory.user_id.in_(config.allowed_user_ids))
        rows = list((await db.scalars(stmt)).all())
        for row in rows:
            prior = await _cas(db, row, {"status": "EXPIRED"})
            await _revision(db, row, reason="ttl_expired", actor_user_id=None, prior_revision=prior)
            await enqueue_memory_outbox(db, row, "DELETE")
        return len(rows)


async def reconcile_pending_deletions(config):
    """Finish authorized cleanup even after rollout is disabled.

    A crashed UPSERT may outlive an earlier successful DELETE. Wait for its
    lease plus a network grace period, then verify every known generation
    before cancelling that old write and marking the derivative deleted.
    """
    from memory.outbox import OutboxLease, _related_sources
    from memory.redaction import json_hash
    from memory.retrieval import authorized_documents
    instant = _now()
    grace = timedelta(seconds=max(config.worker_lease_seconds, config.provider_timeout_seconds))
    kinds = ("memory", "source", "wiki")

    def identity_predicates(target):
        kind, object_id, user_id, workspace_id, _ = target
        return (MemoryOutbox.object_kind == kind, MemoryOutbox.object_id == object_id,
                MemoryOutbox.user_id == user_id, MemoryOutbox.workspace_id == workspace_id)

    def lease_blocks(rows):
        for row in rows:
            if row.status != "RUNNING":
                continue
            until = row.lease_until
            if until is None:
                return True  # Unknown in-flight ownership cannot be declared clear.
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until + grace > _now():
                return True
        return False

    async def ineligible(db, target):
        kind, object_id, user_id, workspace_id, project_id = target
        try:
            scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        except MemoryAccessDenied:
            return True
        # Feature flags govern provider work, not whether SQL still permits a
        # particular object. Disabling Wiki alone is not deletion authority.
        sql_policy_config = config.model_copy(update={"wiki": True, "allowed_user_ids": []})
        return not await authorized_documents(db, scope, sql_policy_config, only={(kind, object_id)})

    async with get_db_session() as db:
        object_debt = select(MemoryIndexState.id).where(MemoryIndexState.object_kind == MemoryTombstone.object_kind,
            MemoryIndexState.object_id == MemoryTombstone.object_id,
            MemoryIndexState.user_id == MemoryTombstone.user_id,
            MemoryIndexState.workspace_id == MemoryTombstone.workspace_id,
            MemoryIndexState.status.notin_(("DELETED", "INELIGIBLE"))).exists()
        source_debt = select(MemorySourceLink.id).join(MemoryIndexState,
            MemoryIndexState.object_id == MemorySourceLink.source_id).where(
            MemoryTombstone.object_kind == "memory", MemorySourceLink.memory_id == MemoryTombstone.object_id,
            MemoryIndexState.user_id == MemoryTombstone.user_id,
            MemoryIndexState.workspace_id == MemoryTombstone.workspace_id,
            MemoryIndexState.object_kind == "source", MemoryIndexState.status.notin_(("DELETED", "INELIGIBLE"))).exists()
        tombstones = list((await db.scalars(select(MemoryTombstone).where(
            MemoryTombstone.object_kind.in_(kinds), or_(MemoryTombstone.purge_status != "SUCCEEDED", object_debt, source_debt)
        ).order_by(MemoryTombstone.deleted_at, MemoryTombstone.id).limit(30))).all())
        memory_ids = [row.object_id for row in tombstones if row.object_kind == "memory"]
        related_sources = list((await db.scalars(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).join(MemoryIndexState,
            MemoryIndexState.object_id == MemorySource.id).where(MemorySourceLink.memory_id.in_(memory_ids),
            MemoryIndexState.object_kind == "source", MemoryIndexState.user_id == MemorySource.user_id,
            MemoryIndexState.workspace_id == MemorySource.workspace_id,
            MemoryIndexState.status.notin_(("DELETED", "INELIGIBLE"))).distinct().order_by(MemorySource.id).limit(30))).all())
        cleanup = list((await db.scalars(select(MemoryOutbox).where(
            MemoryOutbox.object_kind.in_(kinds), MemoryOutbox.operation.in_(("DELETE", "REVOKE")),
            MemoryOutbox.status.in_(("PENDING", "RETRY", "RUNNING", "DEAD"))
        ).order_by(MemoryOutbox.created_at, MemoryOutbox.id).limit(30))).all())
        expired = list((await db.scalars(select(MemoryOutbox).where(
            MemoryOutbox.object_kind.in_(kinds), MemoryOutbox.operation == "UPSERT", MemoryOutbox.status == "RUNNING",
            MemoryOutbox.lease_until <= instant - grace
        ).order_by(MemoryOutbox.lease_until, MemoryOutbox.id).limit(30))).all())
        # An old UPSERT that died after a later DELETE or REVOKE already
        # succeeded: nothing else revisits it, yet cleanup still counts it.
        later = aliased(MemoryOutbox)
        superseded = select(later.id).where(later.object_kind == MemoryOutbox.object_kind,
            later.object_id == MemoryOutbox.object_id, later.user_id == MemoryOutbox.user_id,
            later.workspace_id == MemoryOutbox.workspace_id, later.operation.in_(("DELETE", "REVOKE")),
            later.status == "SUCCEEDED", later.revision >= MemoryOutbox.revision).exists()
        dead = list((await db.scalars(select(MemoryOutbox).where(
            MemoryOutbox.object_kind.in_(kinds), MemoryOutbox.operation == "UPSERT", MemoryOutbox.status == "DEAD",
            superseded).order_by(MemoryOutbox.updated_at, MemoryOutbox.id).limit(30))).all())
        generations = list((await db.scalars(select(MemoryIndexGeneration.id))).all())
        targets = list(dict.fromkeys((row.object_kind, row.object_id, row.user_id, row.workspace_id, row.project_id)
                                    for row in [*tombstones, *cleanup, *expired, *dead]))
        targets.extend(target for target in [("source", source.id, source.user_id, source.workspace_id, source.project_id)
            for source in related_sources] if target not in targets)
    completed = 0
    for target in targets:
        kind, object_id, user_id, workspace_id, project_id = target
        async with get_db_session() as db:
            rows = list((await db.scalars(select(MemoryOutbox).where(*identity_predicates(target)))).all())
            if lease_blocks(rows) or not await ineligible(db, target):
                continue
            states = list((await db.scalars(select(MemoryIndexState).where(MemoryIndexState.object_kind == kind,
                MemoryIndexState.object_id == object_id, MemoryIndexState.user_id == user_id,
                MemoryIndexState.workspace_id == workspace_id))).all())
            target_generations = set(generations) | {config.index_generation} | {
                row.index_generation for row in [*rows, *states]}
            related_generations = {config.index_generation} | {row.index_generation for row in [*rows, *states]}
        try:
            for generation in sorted(target_generations):
                index = QdrantMemoryIndex(config, generation=generation)
                await index.delete_object_versions(kind, object_id)
                if await index.point_ids(kind, object_id):
                    raise MemoryProviderError("deletion_verification_failed")
        except MemoryProviderError:
            continue
        async with get_db_session() as db:
            # Workers acknowledge/fail an event before locking its index state.
            # Use the same order and refresh after any awaited network work.
            rows = list((await db.scalars(select(MemoryOutbox).where(*identity_predicates(target)).order_by(
                MemoryOutbox.id).with_for_update().execution_options(populate_existing=True))).all())
            if lease_blocks(rows) or not await ineligible(db, target):
                continue
            states = list((await db.scalars(select(MemoryIndexState).where(MemoryIndexState.object_kind == kind,
                MemoryIndexState.object_id == object_id, MemoryIndexState.user_id == user_id,
                MemoryIndexState.workspace_id == workspace_id).order_by(MemoryIndexState.id).with_for_update())).all())
            if lease_blocks(rows) or not await ineligible(db, target):
                continue
            current_generations = set((await db.scalars(select(MemoryIndexGeneration.id))).all()) | {
                config.index_generation} | {row.index_generation for row in [*rows, *states]}
            if not current_generations <= target_generations:
                continue  # A generation added during verification needs its own check.
            for row in rows:
                if row.operation == "UPSERT" and row.status in {"PENDING", "RETRY", "RUNNING", "DEAD"}:
                    row.status, row.lease_until, row.updated_at = "CANCELLED", None, _now()
                    row.last_error = "cleanup_verified"
                elif row.operation in {"DELETE", "REVOKE"} and row.status in {"PENDING", "RETRY", "RUNNING", "DEAD"}:
                    row.status, row.lease_until = "SUCCEEDED", None
                    row.last_error, row.delivered_at, row.updated_at = None, _now(), _now()
                    row.payload = {**(row.payload or {}), "index_status": "DELETED", "verified_cleanup": True}
            for state in states:
                state.status, state.indexed_revision, state.chunk_ids = "DELETED", state.desired_revision, []
                state.chunk_manifest_hash, state.last_error = json_hash([]), None
                state.indexed_at, state.updated_at = _now(), _now()
            tombstone = await db.scalar(select(MemoryTombstone).where(MemoryTombstone.object_kind == kind,
                MemoryTombstone.object_id == object_id, MemoryTombstone.user_id == user_id,
                MemoryTombstone.workspace_id == workspace_id).with_for_update())
            if tombstone:
                tombstone.purge_status = "SUCCEEDED"
            if kind == "memory":
                memory = await db.get(UserMemory, object_id)
                for generation in related_generations:
                    await _related_sources(db, OutboxLease("verified-cleanup", "reconcile", 0, kind, object_id,
                        memory.revision if memory else tombstone.revision if tombstone else 1, "DELETE", generation,
                        user_id, workspace_id, project_id))
            completed += 1
    return completed


async def reconcile(scope, config, *, generation=None, limit=200, rebuild=False, cursor=None) -> dict:
    from memory.outbox import ensure_generation
    from memory.retrieval import authorized_documents
    generation = generation or config.index_generation
    await ensure_generation(config, generation)
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
            project_id=scope.project_id, include_all_projects=scope.include_all_projects)
        documents = await authorized_documents(db, current, config)
        states = list((await db.scalars(select(MemoryIndexState).where(*current.predicates(MemoryIndexState),
            MemoryIndexState.index_generation == generation))).all())
    index = QdrantMemoryIndex(config, generation=generation)
    by_key = {(doc.kind, doc.id): doc for doc in documents}
    state_by_key = {(state.object_kind, state.object_id): state for state in states}
    repaired = 0
    keys = sorted(set(by_key) | set(state_by_key))
    if cursor:
        cursor_kind, _, cursor_id = cursor.partition(":")
        keys = [key for key in keys if key > (cursor_kind, cursor_id)]
    bounded_limit = max(1, min(limit, 1000))
    batch = keys[:bounded_limit]
    for key in batch:
        document, state = by_key.get(key), state_by_key.get(key)
        if document is None and state is not None:
            # A bounded lexical scan omits older objects; omission is not
            # deletion authority. Recheck this exact indexed identity in SQL.
            async with get_db_session() as db:
                exact_scope = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
                    project_id=scope.project_id, include_all_projects=scope.include_all_projects)
                exact = await authorized_documents(db, exact_scope, config, only={key})
            document = exact[0] if exact else None
        actual = set(await index.point_ids(*key))
        if document is None and state and state.status in {"DELETED", "INELIGIBLE"} and not actual:
            continue
        matches = bool(document and state and state.status == "INDEXED" and state.indexed_revision == document.revision
                       and set(state.chunk_ids or []) == actual and state.config_hash == config_hash(config))
        if matches and not rebuild:
            continue
        operation = "UPSERT" if document else "DELETE"
        revision = document.revision if document else state.desired_revision
        async with get_db_session() as db:
            # A reconciliation occurrence is explicit and independently
            # idempotent by its immutable desired version + actual manifest.
            from memory.redaction import json_hash
            event = "reconcile:" + json_hash([key[0], key[1], revision, generation, sorted(actual)])
            existing = await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.event_id.startswith(event + ":"),
                                      MemoryOutbox.status.in_(["PENDING", "RETRY", "RUNNING"])))
            if existing:
                continue
            event += ":" + ascending("attempt")
            db.add(MemoryOutbox(id=ascending("memory_outbox"), event_id=event, user_id=scope.user_id,
                workspace_id=scope.workspace_id, project_id=document.project_id if document else state.project_id,
                object_kind=key[0], object_id=key[1], revision=revision, operation=operation,
                index_generation=generation, status="PENDING", priority=0 if document else 100,
                attempts=0, lease_generation=0, available_at=_now(), created_at=_now(), updated_at=_now()))
            found = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_kind == key[0],
                MemoryIndexState.object_id == key[1], MemoryIndexState.index_generation == generation))
            if found is None:
                db.add(MemoryIndexState(id=ascending("memory_index"), object_kind=key[0], object_id=key[1],
                    index_generation=generation, user_id=scope.user_id, workspace_id=scope.workspace_id, project_id=document.project_id if document else state.project_id,
                    desired_revision=revision, indexed_revision=0, chunk_ids=[], status="PENDING", updated_at=_now()))
            else:
                found.desired_revision, found.status, found.updated_at = revision, "PENDING", _now()
            repaired += 1
    next_cursor = ":".join(batch[-1]) if len(keys) > bounded_limit else None
    async with get_db_session() as db:
        manifest = await db.get(MemoryIndexGeneration, generation)
        if manifest:
            manifest.reconciled_at = _now()
    return {"generation": generation, "examined": len(batch), "next_cursor": next_cursor,
            "repair_events": repaired, "complete": repaired == 0 and next_cursor is None,
            "scope": {"user_id": scope.user_id, "workspace_id": scope.workspace_id, "project_id": scope.project_id}}


async def rebuild_generation(scope, config, *, generation, limit=200, cursor=None) -> dict:
    """Bounded, explicit rebuild; never scans another user's history."""
    return await reconcile(scope, config, generation=generation, limit=limit, rebuild=True, cursor=cursor)
