"""Transactional journal: database sequence, idempotent facts, commit hints."""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
from sqlalchemy import event as sa_event, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as SyncSession
from db.base import get_db_session
from db.models.session import Session
from db.models.trajectory import SessionTrajectory, TrajectoryEvent
from trajectory.config import enabled, integer
from trajectory.context import TraceContext, current
from trajectory.payload import expand, store_json
from trajectory.redaction import sanitize
from trajectory.types import ID_FIELDS, IdempotencyConflict, OwnershipError, RecordingError, canonical, digest, iso, now, prepare


@dataclass(frozen=True)
class PendingRange:
    trajectory_id: str
    from_seq: str
    through_seq: str
    events: tuple[dict, ...]


def _insert(db, model):
    return (sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert)(model)


async def context_for_session(db, user_id: str, session_id: str, **ids) -> TraceContext:
    row = await db.get(Session, session_id)
    if row is None or row.user_id != user_id or row.is_deleted:
        raise OwnershipError("Trajectory source session is missing, deleted or belongs to another owner")
    inherited = current()
    if inherited is not None and inherited.user_id == user_id and inherited.source_session_id == session_id:
        return inherited.derive(**ids)
    return TraceContext(user_id=user_id, session_id=session_id, source_session_id=session_id,
                        workspace_id=row.workspace_id, **ids)


async def _validate_owner(db, context: TraceContext) -> Session:
    owner = await db.get(Session, context.session_id)
    if owner is None or owner.is_deleted or owner.user_id != context.user_id:
        raise OwnershipError("Trajectory owner session is missing, deleted or mismatched")
    if context.workspace_id is not None and context.workspace_id != owner.workspace_id:
        raise OwnershipError("Trajectory workspace does not match its owner")
    source = context.source_session_id
    seen = set()
    # Explicit parent chain validation prevents a context from joining an
    # arbitrary same-user session to a different trajectory.
    while source and source != owner.id:
        if source in seen or len(seen) >= 100:
            raise OwnershipError("Invalid source session ancestry")
        seen.add(source)
        row = await db.get(Session, source)
        if row is None or row.is_deleted or row.user_id != owner.user_id:
            raise OwnershipError("Trajectory child ownership does not match")
        source = row.parent_id
    if source != owner.id:
        raise OwnershipError("Source session is not delegated by the trajectory owner")
    return owner


async def ensure_trajectory_in_tx(db, context: TraceContext, baseline: dict | None = None) -> SessionTrajectory:
    owner = await _validate_owner(db, context)
    timestamp = now()
    candidate = f"trj_{uuid4().hex}"
    statement = _insert(db, SessionTrajectory).values(id=candidate, user_id=context.user_id,
        session_id=context.session_id, workspace_id=owner.workspace_id,
        started_at=timestamp, updated_at=timestamp, next_seq=1, committed_seq=0,
        projected_seq=0, schema_version=1, recording_status="recording")
    # Both session_id and (owner, session_id) are unique. PostgreSQL may detect
    # either index first during simultaneous first contact; handling only one
    # arbiter can still raise a unique violation in another worker.
    await db.execute(statement.on_conflict_do_nothing())
    trajectory = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == owner.id).with_for_update(key_share=True).execution_options(populate_existing=True))
    if trajectory.user_id != context.user_id or trajectory.deleted_at is not None:
        raise OwnershipError("Deleted or mismatched trajectory cannot be restarted")
    resumed = trajectory.recording_status == "paused" and enabled(context.user_id)
    if resumed:
        await _append_prepared(db, context, trajectory, [prepare(context, {"type": "recording.gap",
            "data": {"phase": "resumed", "reason": "recording_reenabled", "previous_committed_seq": str(trajectory.committed_seq)}})])
        trajectory.recording_status = "gap"
        db.sync_session.info.setdefault("trajectory_resume_baselines", {})[trajectory.id] = f"evt_baseline_{trajectory.id}_{trajectory.committed_seq}"
    if trajectory.committed_seq == 0:
        created_at = owner.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timestamp.tzinfo)
        initial = prepare(context, {"event_id": f"evt_start_{trajectory.id}", "type": "trajectory.started", "occurred_at": timestamp,
            "data": {"existing_session": baseline.get("legacy_session", baseline.get("existing_session")) if baseline is not None else None,
                     "coverage_start": iso(timestamp), "schema_version": 1}})
        await _append_prepared(db, context, trajectory, [initial])
    if baseline is not None:
        pending_baselines = db.sync_session.info.setdefault("trajectory_resume_baselines", {})
        baseline_id = pending_baselines.get(trajectory.id, f"evt_baseline_{trajectory.id}")
        existing = await db.get(TrajectoryEvent, baseline_id)
        if existing is None:
            await _append_prepared(db, context, trajectory, [prepare(context,
                {"event_id": baseline_id, "type": "baseline.captured", "data": baseline})])
        pending_baselines.pop(trajectory.id, None)
    return trajectory


def event_dict(row: TrajectoryEvent) -> dict:
    return {"event_id": row.event_id, "trajectory_id": row.trajectory_id,
            "user_id": row.user_id, "session_id": row.session_id,
            **row.context, "source_session_id": row.source_session_id,
            "seq": str(row.seq), "type": row.type, "version": row.version,
            "occurred_at": iso(row.occurred_at), "recorded_at": iso(row.recorded_at), "data": row.data}


async def _append_prepared(db, context, trajectory, prepared):
    # Atomic UPDATE serializes SQLite writers and locks one PostgreSQL row.
    # The lock and counter changes are part of the caller's transaction.
    await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == trajectory.id)
                     .values(next_seq=SessionTrajectory.next_seq).execution_options(synchronize_session=False))
    unique = {}
    event_hashes = {}
    for item in prepared:
        item["data"] = sanitize(item["data"])
        hash_value = digest({key: value for key, value in item.items() if key not in {"event_id", "occurred_at"}})
        old = unique.get(item["event_id"])
        if old is not None and event_hashes[item["event_id"]] != hash_value:
            raise IdempotencyConflict("Repeated event ID has conflicting content")
        unique[item["event_id"]] = item
        event_hashes[item["event_id"]] = hash_value
    existing = {}
    if unique:
        for row in (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.event_id.in_(unique)))).all():
            if row.trajectory_id != trajectory.id or row.content_hash != event_hashes[row.event_id]:
                raise IdempotencyConflict("Event ID was already committed with different content or ownership")
            existing[row.event_id] = row
    fresh = [item for event_id, item in unique.items() if event_id not in existing]
    serialized = {key: event_dict(row) for key, row in existing.items()}
    if fresh:
        count = len(fresh)
        after = (await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == trajectory.id)
            .values(next_seq=SessionTrajectory.next_seq + count,
                    committed_seq=SessionTrajectory.next_seq + count - 1, updated_at=now())
            .returning(SessionTrajectory.next_seq, SessionTrajectory.committed_seq)
            .execution_options(synchronize_session=False))).one()
        start = after.next_seq - count
        projection_events = []
        for offset, item in enumerate(fresh):
            seq = start + offset
            data = item["data"]
            stored_data = data
            if len(canonical(data)) > integer("TRAJECTORY_INLINE_BYTES", 65536):
                stored_data = await store_json(db, trajectory.id, data, first_seq=seq)
            context_data = {key: item[key] for key in ID_FIELDS if item.get(key) is not None}
            row = TrajectoryEvent(event_id=item["event_id"], trajectory_id=trajectory.id, seq=seq,
                type=item["type"], version=item["version"], user_id=context.user_id,
                session_id=context.session_id, source_session_id=item.get("source_session_id") or context.session_id,
                request_id=item.get("request_id"), call_id=item.get("call_id"), agent_id=item.get("agent_id"),
                context=context_data, data=stored_data, content_hash=event_hashes[item["event_id"]],
                occurred_at=datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")), recorded_at=now())
            db.add(row)
            serialized[row.event_id] = event_dict(row)
            projection_events.append({**serialized[row.event_id], "data": data})
        trajectory.next_seq = after.next_seq
        trajectory.committed_seq = after.committed_seq
        await db.flush()
        from trajectory.repository import project_events_in_tx
        await project_events_in_tx(db, trajectory, projection_events)
        pending = db.sync_session.info.setdefault("trajectory_notifications", {})
        pending[trajectory.id] = {"user_id": context.user_id, "owner_user_id": context.user_id,
            "session_id": context.session_id, "trajectory_id": trajectory.id, "committed_seq": str(after.committed_seq)}
    values = tuple(sorted(serialized.values(), key=lambda value: int(value["seq"])))
    return PendingRange(trajectory.id, values[0]["seq"] if values else str(trajectory.committed_seq),
                        values[-1]["seq"] if values else str(trajectory.committed_seq), values)


async def append_events_in_tx(db, context: TraceContext, events: list[dict]) -> PendingRange:
    await _retire_previous_failed_run(context)
    prepared = [prepare(context, item) for item in events]
    # Validate per-event source overrides, not only the outer context.
    for item in prepared:
        if item.get("source_session_id") != context.source_session_id:
            await _validate_owner(db, context.derive(source_session_id=item.get("source_session_id")))
        if item["type"] in {"request.started", "request.delta", "tool.started", "tool.output", "step.started"} and item.get("run_id") and item.get("generation") is not None:
            from db.models.question import SessionExecution
            execution = await db.get(SessionExecution, item["source_session_id"])
            auxiliary_after_run = execution is not None and item["data"].get("purpose") in {"title", "suggestions"} and execution.run_id is None and execution.generation == item["generation"]
            if execution is not None and (execution.user_id != context.user_id or not auxiliary_after_run and (execution.run_id != item["run_id"] or execution.run_generation != item["generation"])):
                raise OwnershipError("Execution lease is stale; new side effects and output cannot be recorded")
    trajectory = await ensure_trajectory_in_tx(db, context)
    failure_key = (id(asyncio.get_running_loop()), context.user_id, context.session_id)
    failure = _failed_streams.get(failure_key)
    if failure is not None and failure_key not in db.sync_session.info.get("trajectory_recovered_streams", set()):
        await _append_prepared(db, failure["context"], trajectory, [prepare(failure["context"], {
            "event_id": failure["event_id"], "type": "recording.gap", "data": {
                "reason": "stream_persistence_failed", "phase": "recovered", "error_type": failure["error_type"],
                "last_committed_seq": str(trajectory.committed_seq), "last_stream_receipt_seq": failure["watermark"],
                "uncommitted_output": "not_recorded"}})])
        db.sync_session.info.setdefault("trajectory_recovered_streams", set()).add(failure_key)
    return await _append_prepared(db, context, trajectory, prepared)


async def record(type: str, data: dict, *, context: TraceContext | None = None,
                 db=None, event_id: str | None = None, occurred_at=None, **ids):
    context = context or current()
    if context is None:
        return None
    if not enabled(context.user_id):
        if db is not None:
            await mark_capture_paused_in_tx(db, context.user_id, context.session_id)
        else:
            async with get_db_session() as transaction:
                await mark_capture_paused_in_tx(transaction, context.user_id, context.session_id)
        return None
    item = {"type": type, "data": data, **ids}
    if event_id is not None:
        item["event_id"] = event_id
    if occurred_at is not None:
        item["occurred_at"] = occurred_at
    if db is not None:
        return await append_events_in_tx(db, context, [item])
    await _retire_previous_failed_run(context)
    await flush(context)
    async with get_db_session() as transaction:
        result = await append_events_in_tx(transaction, context, [item])
    return result.events[-1] if result.events else None


async def mark_capture_paused_in_tx(db, user_id: str, session_id: str) -> None:
    """Record only an existing container's coverage boundary while disabled."""
    trajectory = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == session_id,
        SessionTrajectory.user_id == user_id, SessionTrajectory.deleted_at.is_(None)).with_for_update(key_share=True))
    if trajectory is None or trajectory.recording_status == "paused":
        return
    context = TraceContext(user_id=user_id, session_id=session_id, workspace_id=trajectory.workspace_id)
    await _append_prepared(db, context, trajectory, [prepare(context, {"type": "recording.gap",
        "data": {"phase": "paused", "reason": "recording_disabled", "last_recorded_seq": str(trajectory.committed_seq)}})])
    trajectory.recording_status = "paused"
    from db.models.trajectory import TrajectorySessionSummary
    summary = await db.get(TrajectorySessionSummary, trajectory.id)
    if summary is not None:
        summary.recording_status = "paused"


@sa_event.listens_for(SyncSession, "after_commit")
def _after_commit(session):
    if session.in_nested_transaction():
        return
    notifications = session.info.pop("trajectory_notifications", {})
    for key in session.info.pop("trajectory_recovered_streams", set()):
        _failed_streams.pop(key, None)
    if notifications:
        from bus.bus import publish
        for notification in notifications.values():
            publish("trajectory.available", notification)


@sa_event.listens_for(SyncSession, "after_soft_rollback")
def _after_rollback(session, previous_transaction):
    # Dropping an outer hint after a savepoint rollback is conservative. The
    # periodic watermark check recovers it; uncommitted hints never escape.
    session.info.pop("trajectory_notifications", None)
    session.info.pop("trajectory_resume_baselines", None)
    session.info.pop("trajectory_recovered_streams", None)


class _Capacity:
    def __init__(self):
        self.used = 0
        self.condition = asyncio.Condition()

    async def acquire(self, size):
        maximum = integer("TRAJECTORY_TOTAL_PENDING_BYTES", 64 * 1024 * 1024)
        async with self.condition:
            while self.used and self.used + size > maximum:
                await self.condition.wait()
            self.used += size

    async def release(self, size):
        async with self.condition:
            self.used -= size
            self.condition.notify_all()


_capacities = {}


def _capacity():
    key = id(asyncio.get_running_loop())
    return _capacities.setdefault(key, _Capacity())


class _StreamQueue:
    def __init__(self, context):
        self.context = context
        self.items = []
        self.pending_bytes = 0
        self.condition = asyncio.Condition()
        self.task = None
        self.failure = None
        self.watermark = "0"
        self.receipts = set()

    async def enqueue(self, item):
        size = len(canonical(item))
        maximum = integer("TRAJECTORY_PENDING_BYTES", 4 * 1024 * 1024)
        future = asyncio.get_running_loop().create_future()
        capacity = _capacity()
        await capacity.acquire(size)
        try:
            async with self.condition:
                while self.pending_bytes and self.pending_bytes + size > maximum:
                    if self.failure is not None:
                        raise self.failure
                    await self.condition.wait()
                if self.failure is not None:
                    raise self.failure
                self.items.append((item, size, future))
                self.pending_bytes += size
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self.commit())
        except BaseException:
            await capacity.release(size)
            raise
        return await asyncio.shield(future)

    async def commit(self):
        await asyncio.sleep(integer("TRAJECTORY_BATCH_MS", 50) / 1000)
        while True:
            async with self.condition:
                batch, self.items = self.items, []
            if not batch:
                return
            try:
                async with get_db_session() as db:
                    result = await append_events_in_tx(db, self.context, [item for item, _, _ in batch])
                events = {item["event_id"]: item for item in result.events}
                self.watermark = result.through_seq
                for item, _, future in batch:
                    if not future.done():
                        future.set_result(events[item["event_id"]])
            except BaseException as exc:
                self.failure = exc
                for _, _, future in batch:
                    if not future.done():
                        future.set_exception(exc)
                async with self.condition:
                    abandoned, self.items = self.items, []
                    for _, _, future in abandoned:
                        if not future.done():
                            future.set_exception(exc)
                    self.pending_bytes = 0
                    self.condition.notify_all()
                await _capacity().release(sum(size for _, size, _ in batch + abandoned))
                return
            async with self.condition:
                self.pending_bytes -= sum(size for _, size, _ in batch)
                self.condition.notify_all()
                await _capacity().release(sum(size for _, size, _ in batch))
                if not self.items:
                    return


_streams: dict[tuple, _StreamQueue] = {}
_failed_streams: dict[tuple, dict] = {}


def _remember_failed_stream(key, stream):
    _failed_streams.setdefault(key, {"context": stream.context, "event_id": f"evt_gap_{uuid4().hex}",
        "error_type": type(stream.failure).__name__, "watermark": stream.watermark})
    if _streams.get(key) is stream:
        _streams.pop(key, None)


async def _retire_previous_failed_run(context):
    key = (id(asyncio.get_running_loop()), context.user_id, context.session_id)
    stream = _streams.get(key)
    if stream is None or stream.failure is None:
        return
    old = stream.context
    fresh_run = (context.run_id, context.generation) != (old.run_id, old.generation)
    fresh_unleased_request = context.run_id is None and old.run_id is None and context.request_id != old.request_id
    if fresh_run or fresh_unleased_request:
        if stream.task is not None:
            await asyncio.shield(stream.task)
        _remember_failed_stream(key, stream)


def record_stream(context: TraceContext, event: dict):
    """Queue immediately when scheduled; receipt resolves only after commit.

    Producers can enqueue several chunks and await their receipts in order.
    Memory pressure blocks producers rather than discarding observed chunks.
    """
    loop = asyncio.get_running_loop()
    if not enabled(context.user_id):
        return asyncio.create_task(record("recording.gap", {}, context=context))
    key = (id(loop), context.user_id, context.session_id)
    item = prepare(context, event)
    size = len(canonical(item))
    if size > min(integer("TRAJECTORY_PENDING_BYTES", 4 * 1024 * 1024), integer("TRAJECTORY_TOTAL_PENDING_BYTES", 64 * 1024 * 1024)):
        # One oversized observed chunk goes directly to durable payload staging
        # after the stream barrier; it is never buffered behind other chunks.
        return asyncio.create_task(record(item["type"], item["data"], context=context,
            event_id=item["event_id"], occurred_at=item["occurred_at"],
            **{key: value for key, value in item.items() if key in ID_FIELDS}))
    stream = _streams.setdefault(key, _StreamQueue(context))
    from trajectory.types import recording_boundary
    task = asyncio.create_task(recording_boundary(stream.enqueue)(item))
    stream.receipts.add(task)
    task.add_done_callback(stream.receipts.discard)
    return task


async def flush(context: TraceContext | None = None) -> str:
    context = context or current()
    if context is None:
        streams = [stream for key, stream in list(_streams.items()) if key[0] == id(asyncio.get_running_loop())]
        for stream in streams:
            await flush(stream.context)
        return "0"
    stream = _streams.get((id(asyncio.get_running_loop()), context.user_id, context.session_id))
    if stream is not None:
        # Include enqueue tasks that have not had their first event-loop turn
        # yet. Structural events cannot race ahead of scheduled chunks.
        pending = list(stream.receipts)
        try:
            if pending:
                await asyncio.gather(*(asyncio.shield(task) for task in pending))
            if stream.task is not None:
                await asyncio.shield(stream.task)
            if stream.failure is not None:
                raise stream.failure
        except BaseException:
            if stream.failure is not None:
                if stream.task is not None:
                    await asyncio.shield(stream.task)
                key = (id(asyncio.get_running_loop()), context.user_id, context.session_id)
                _remember_failed_stream(key, stream)
            raise
        if not stream.receipts and not stream.items:
            _streams.pop((id(asyncio.get_running_loop()), context.user_id, context.session_id), None)
    async with get_db_session() as db:
        value = await db.scalar(select(SessionTrajectory.committed_seq).where(
            SessionTrajectory.user_id == context.user_id, SessionTrajectory.session_id == context.session_id))
    return str(value or 0)

from trajectory.types import recording_boundary
ensure_trajectory_in_tx = recording_boundary(ensure_trajectory_in_tx)
append_events_in_tx = recording_boundary(append_events_in_tx)
record = recording_boundary(record)
flush = recording_boundary(flush)
