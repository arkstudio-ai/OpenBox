"""Fenced SQL outbox delivery and compensating cleanup of late Qdrant writes."""
import asyncio
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_runtime import MemoryIndexGeneration
from db.models.memory_v2 import MemoryIndexState, MemoryOutbox, MemorySource, MemorySourceLink, MemoryTombstone
from memory.index.embedding import BailianEmbedding
from memory.index.qdrant import QdrantMemoryIndex, chunks_for, config_hash, index_config
from memory.policy import MemoryAccessDenied, active_memory_predicates, resolve_access_scope
from memory.providers.common import MemoryProviderError
from memory.redaction import json_hash
from memory.retrieval import authorized_documents

log = create_logger("memory.outbox")


def now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OutboxLease:
    id: str
    owner: str
    generation: int
    object_kind: str
    object_id: str
    revision: int
    operation: str
    index_generation: str
    user_id: str
    workspace_id: str
    project_id: str | None


def _claimable(instant):
    return or_(and_(MemoryOutbox.status.in_(["PENDING", "RETRY"]), MemoryOutbox.available_at <= instant),
               and_(MemoryOutbox.status == "RUNNING", MemoryOutbox.lease_until < instant))


def _delivery_eligible(config):
    # Destructive events are the continuation of an already authorized SQL
    # change. Turning off rollout must not leave its sensitive vectors behind.
    cleanup = MemoryOutbox.operation.in_(("DELETE", "REVOKE"))
    if not config.index_sync:
        return cleanup
    upserts = [MemoryOutbox.operation == "UPSERT"]
    if config.allowed_user_ids:
        upserts.append(MemoryOutbox.user_id.in_(config.allowed_user_ids))
    return or_(cleanup, and_(*upserts))


async def claim_outbox(owner: str, config) -> OutboxLease | None:
    async with get_db_session() as db:
        instant = now()
        eligibility = _delivery_eligible(config)
        exhausted = update(MemoryOutbox).where(_claimable(instant), eligibility,
                                             MemoryOutbox.attempts >= config.max_attempts)
        await db.execute(exhausted.values(status="DEAD", lease_until=None, last_error="lease_attempts_exhausted",
                                          updated_at=instant).execution_options(synchronize_session=False))
        stmt = select(MemoryOutbox).where(_claimable(instant), eligibility, MemoryOutbox.attempts < config.max_attempts)
        row = await db.scalar(stmt.order_by(MemoryOutbox.priority.desc(), MemoryOutbox.created_at, MemoryOutbox.id).limit(1))
        if row is None:
            return None
        generation = row.lease_generation + 1
        result = await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == row.id,
            MemoryOutbox.lease_generation == row.lease_generation, _claimable(instant), _delivery_eligible(config)).values(
            status="RUNNING", lease_owner=owner, lease_generation=generation,
            lease_until=instant + timedelta(seconds=config.worker_lease_seconds),
            attempts=MemoryOutbox.attempts + 1, updated_at=instant).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            return None
        return OutboxLease(row.id, owner, generation, row.object_kind, row.object_id, row.revision,
                           row.operation, row.index_generation, row.user_id, row.workspace_id, row.project_id)


def _fence(lease):
    return (MemoryOutbox.id == lease.id, MemoryOutbox.status == "RUNNING", MemoryOutbox.lease_owner == lease.owner,
            MemoryOutbox.lease_generation == lease.generation, MemoryOutbox.lease_until > now())


async def _has_lease(lease):
    async with get_db_session() as db:
        return bool(await db.scalar(select(MemoryOutbox.id).where(*_fence(lease))))


async def ensure_generation(config, generation=None):
    generation = generation or config.index_generation
    async with get_db_session() as db:
        row = await db.get(MemoryIndexGeneration, generation)
        if row and row.config_hash != config_hash(config):
            raise MemoryProviderError("index_generation_configuration_changed")
        if row is None:
            db.add(MemoryIndexGeneration(id=generation, config_hash=config_hash(config), config=index_config(config),
                                         status="BUILDING", created_at=now()))


async def _state(db, lease):
    return await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_kind == lease.object_kind,
        MemoryIndexState.object_id == lease.object_id, MemoryIndexState.index_generation == lease.index_generation).with_for_update())


async def _snapshot(lease, config):
    async with get_db_session() as db:
        try:
            scope = await resolve_access_scope(db, user_id=lease.user_id, workspace_id=lease.workspace_id,
                                               project_id=lease.project_id)
        except MemoryAccessDenied:
            return None
        docs = await authorized_documents(db, scope, config, only={(lease.object_kind, lease.object_id)})
        doc = docs[0] if docs else None
        state = await _state(db, lease)
        if state and state.desired_revision != lease.revision:
            return None
        if doc and doc.revision != lease.revision:
            return None
        return doc


async def _related_sources(db, lease):
    if lease.object_kind != "memory":
        return
    sources = list((await db.scalars(select(MemorySource).join(MemorySourceLink,
        MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == lease.object_id))).unique().all())
    instant = now()
    for source in sources:
        eligible = await db.scalar(select(MemorySourceLink.id).join(UserMemory,
            UserMemory.id == MemorySourceLink.memory_id).where(MemorySourceLink.source_id == source.id,
            MemorySourceLink.revision == UserMemory.revision, MemorySourceLink.relation == "SUPPORTS",
            UserMemory.user_id == source.user_id, UserMemory.workspace_id == source.workspace_id,
            *active_memory_predicates()).limit(1))
        from memory.service import source_body_is_available
        readable = False
        if eligible and source.status == "ACTIVE":
            try:
                source_scope = await resolve_access_scope(db, user_id=source.user_id,
                    workspace_id=source.workspace_id, project_id=source.project_id)
                readable = await source_body_is_available(db, source_scope, source)
            except MemoryAccessDenied:
                pass
        operation = "UPSERT" if readable else "DELETE"
        if operation == "DELETE":
            await db.execute(update(MemoryOutbox).where(MemoryOutbox.object_kind == "source",
                MemoryOutbox.object_id == source.id, MemoryOutbox.operation == "UPSERT",
                MemoryOutbox.status.in_(["PENDING", "RETRY"])).values(status="CANCELLED",
                last_error="source_body_unavailable", updated_at=instant).execution_options(synchronize_session=False))
        event = "source:" + json_hash([source.id, source.source_revision, operation, lease.revision, lease.index_generation])
        existing = await db.scalar(select(MemoryOutbox).where(MemoryOutbox.event_id == event))
        if existing is None:
            db.add(MemoryOutbox(id=ascending("memory_outbox"), event_id=event, user_id=source.user_id,
                workspace_id=source.workspace_id, project_id=source.project_id, object_kind="source", object_id=source.id,
                revision=source.source_revision, operation=operation, index_generation=lease.index_generation,
                status="PENDING", attempts=0, lease_generation=0, priority=90 if operation == "DELETE" else 0,
                available_at=instant, created_at=instant, updated_at=instant))
        elif existing.status not in {"PENDING", "RETRY", "RUNNING"}:
            # Replaying a parent delivery must not reopen a derivative that
            # already completed (or manufacture a retry for a dead event).
            continue
        state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_kind == "source",
            MemoryIndexState.object_id == source.id, MemoryIndexState.index_generation == lease.index_generation))
        if state is None:
            db.add(MemoryIndexState(id=ascending("memory_index"), object_kind="source", object_id=source.id,
                index_generation=lease.index_generation, user_id=source.user_id, workspace_id=source.workspace_id, project_id=source.project_id,
                desired_revision=source.source_revision, indexed_revision=0, chunk_ids=[], status="PENDING", updated_at=instant))
        else:
            state.desired_revision, state.status, state.updated_at = source.source_revision, "PENDING", instant


async def deliver_outbox(lease, config, *, index=None, embedding=None):
    if not await _has_lease(lease):
        return False
    adapter = index or QdrantMemoryIndex(config, generation=lease.index_generation)
    document = await _snapshot(lease, config) if lease.operation == "UPSERT" else None
    ids, usage, status = [], {}, "INELIGIBLE"
    if document is not None:
        if not config.enabled("index_sync", lease.user_id):
            return False
        await ensure_generation(config, lease.index_generation)
        chunks = chunks_for(document.text, config.source_chunk_chars)
        vectors, usage = await (embedding or BailianEmbedding(config)).embed(chunks)
        if not await _has_lease(lease):
            return False
        # Do not send a snapshot if its ACL/version changed during embedding.
        current = await _snapshot(lease, config)
        if current is None or current != document:
            document = None
        else:
            if not config.enabled("index_sync", lease.user_id):
                return False
            if not await _has_lease(lease):
                return False
            ids = await adapter.upsert_version(document, vectors, chunks)
            current = await _snapshot(lease, config)
            if current is None or current != document:
                # A stale worker must not delete a newer revision written by
                # another worker. Remove only the version it just wrote.
                await adapter.delete_revision(lease.object_kind, lease.object_id, lease.revision)
                ids, document = [], None
            else:
                if not await _has_lease(lease):
                    return False
                await adapter.delete_object_versions(lease.object_kind, lease.object_id, keep_revision=lease.revision)
                status = "INDEXED"
    if document is None:
        if lease.operation != "UPSERT":
            await adapter.delete_object_versions(lease.object_kind, lease.object_id)
            # Tombstones suppress every known index generation, including a
            # retired generation that might later be selected for rollback.
            async with get_db_session() as db:
                generations = list((await db.scalars(select(MemoryIndexGeneration.id))).all())
            for generation in generations:
                if generation != lease.index_generation:
                    await QdrantMemoryIndex(config, generation=generation).delete_object_versions(lease.object_kind, lease.object_id)
            status = "DELETED"
        else:
            # Only delete this outbox version; latest SQL revision may be in
            # flight and needs its own event. Old rows are never returned.
            await adapter.delete_revision(lease.object_kind, lease.object_id, lease.revision)
    async with get_db_session() as db:
        row = await db.scalar(select(MemoryOutbox).where(*_fence(lease)).with_for_update())
        if row is None:
            return False
        state = await _state(db, lease)
        preserve_deleted = bool(state and state.status == "DELETED" and lease.operation == "UPSERT" and status == "INELIGIBLE")
        if state and state.desired_revision == lease.revision and not preserve_deleted:
            state.indexed_revision, state.chunk_ids = lease.revision, ids
            state.chunk_manifest_hash, state.config_hash = json_hash(ids), adapter.fingerprint
            state.status, state.last_error, state.indexed_at, state.updated_at = status, None, now(), now()
        row.status, row.delivered_at, row.updated_at = "SUCCEEDED", now(), now()
        row.lease_until, row.last_error = None, None
        row.payload = {"usage": usage, "index_status": status, "chunk_count": len(ids)}
        await _related_sources(db, lease)
        tombstone = await db.scalar(select(MemoryTombstone).where(MemoryTombstone.object_kind == lease.object_kind,
            MemoryTombstone.object_id == lease.object_id).with_for_update())
        if tombstone and status == "DELETED":
            outstanding = await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.object_kind == lease.object_kind,
                MemoryOutbox.object_id == lease.object_id, MemoryOutbox.id != lease.id,
                MemoryOutbox.status.in_(["PENDING", "RETRY", "RUNNING"])).limit(1))
            tombstone.purge_status = "PENDING" if outstanding else "SUCCEEDED"
    return True


async def fail_outbox(lease, config, code):
    obsolete = lease.operation == "UPSERT" and await _snapshot(lease, config) is None
    async with get_db_session() as db:
        row = await db.scalar(select(MemoryOutbox).where(*_fence(lease)).with_for_update())
        if row is None:
            return
        row.status = "CANCELLED" if obsolete else "DEAD" if row.attempts >= config.max_attempts else "RETRY"
        row.last_error = code[:64]
        row.available_at = now() + timedelta(seconds=min(300, 2 ** row.attempts) + random.random())
        row.lease_until, row.updated_at = None, now()
        state = await _state(db, lease)
        if state and not obsolete and state.desired_revision == lease.revision and state.status != "DELETED":
            state.last_error = row.last_error
            state.status = "DEAD" if row.status == "DEAD" else "RETRY"


class MemoryIndexWorker:
    def __init__(self, config=None, *, index=None, embedding=None):
        from core.config import get_config
        self.config = config or get_config().memory
        self.owner = "memory-index-" + str(uuid.uuid4())
        self.index, self.embedding, self.task = index, embedding, None
        self._last_reconcile, self._reconcile_offset = 0.0, 0
        self._reconcile_cursors = {}

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def run_once(self):
        lease = await claim_outbox(self.owner, self.config)
        if lease is None:
            return False
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(lease, heartbeat_stop))
        try:
            await deliver_outbox(lease, self.config, index=self.index, embedding=self.embedding)
        except MemoryProviderError as exc:
            await fail_outbox(lease, self.config, exc.code)
        except Exception as exc:
            # No request body, provider error body or credential in logs.
            await fail_outbox(lease, self.config, type(exc).__name__)
            log.warning("Memory index delivery failed (%s)", type(exc).__name__)
        finally:
            heartbeat_stop.set()
            await heartbeat
        return True

    async def _heartbeat(self, lease, stop):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1, self.config.worker_lease_seconds / 3))
            except asyncio.TimeoutError:
                async with get_db_session() as db:
                    result = await db.execute(update(MemoryOutbox).where(*_fence(lease)).values(
                        lease_until=now() + timedelta(seconds=self.config.worker_lease_seconds), updated_at=now()
                    ).execution_options(synchronize_session=False))
                if result.rowcount != 1:
                    return

    async def _reconcile_scopes(self):
        if not self.config.index_sync or time.monotonic() - self._last_reconcile < 300:
            return
        self._last_reconcile = time.monotonic()
        from memory.reconcile import reconcile
        from db.models.memory_document import MemoryDocument
        async with get_db_session() as db:
            scopes = select(UserMemory.user_id, UserMemory.workspace_id, UserMemory.project_id).union(
                select(MemoryDocument.user_id, MemoryDocument.workspace_id, MemoryDocument.project_id)).subquery()
            stmt = select(scopes.c.user_id, scopes.c.workspace_id, scopes.c.project_id)
            if self.config.allowed_user_ids:
                stmt = stmt.where(scopes.c.user_id.in_(self.config.allowed_user_ids))
            groups = list((await db.execute(stmt.order_by(scopes.c.user_id, scopes.c.workspace_id,
                scopes.c.project_id).offset(self._reconcile_offset).limit(20))).all())
        self._reconcile_offset = self._reconcile_offset + len(groups) if len(groups) == 20 else 0
        for user_id, workspace_id, project_id in groups:
            try:
                async with get_db_session() as db:
                    scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
                scope_key = (user_id, workspace_id, project_id)
                result = await reconcile(scope, self.config, limit=100, cursor=self._reconcile_cursors.get(scope_key))
                self._reconcile_cursors[scope_key] = result["next_cursor"]
            except MemoryAccessDenied:
                continue
            except MemoryProviderError:
                return

    async def _run(self):
        from memory.reconcile import expire_memories, reconcile_pending_deletions
        from memory.observability import purge_expired_debug_snapshots
        tick = 0
        while True:
            try:
                worked = await self.run_once()
                tick += 1
                if tick % 30 == 1:
                    await purge_expired_debug_snapshots()
                    await expire_memories(self.config)
                    await reconcile_pending_deletions(self.config)
                    await self._reconcile_scopes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Memory index worker iteration failed (%s)", type(exc).__name__)
                worked = False
            if not worked:
                await asyncio.sleep(self.config.worker_interval_seconds)
