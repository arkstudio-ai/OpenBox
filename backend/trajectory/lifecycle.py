"""Deletion and expiry of trajectory content in the trace database (SPEC §8.5, §8.11).

The trajectory worker applies these helpers inside trace transactions:
tombstones for deleted sessions, content expiry, revocation of asset-bound
payloads and the export rows that go with them. The rows that keep deletion
final stay behind: the trajectory row and payload rows reject late events and
resurrection.

Object storage is never touched here. A transaction enqueues the keys and
prefixes it stopped referencing in ``trajectory_gc_queue``, and
RetentionService.process_gc_queue deletes the objects afterwards.
"""
from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import delete, func, select, text, update
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SyncSession

from core.log import create_logger
from trajectory.config import integer
from trajectory.storage import check_key, key_prefix, trajectory_prefix
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryEvent, TrajectoryExport,
    TrajectoryGcQueue, TrajectoryPayload, TrajectoryRecord, TrajectoryRecordEvent, TrajectorySegment,
    TrajectorySessionSummary, TrajectoryWorkerState)
from trajectory.types import now

log = create_logger("trajectory.lifecycle")

GC_KEY = "key"
GC_PREFIX = "prefix"
GC_RETRY_BASE_SECONDS = 30
GC_RETRY_MAX_SECONDS = 6 * 3600
#: A second pass over a deleted trajectory's prefix removes objects that
#: uploads already in flight (ingest retries, an archive segment) wrote after
#: the first pass.
PREFIX_SWEEP_DELAY = timedelta(minutes=15)
NOTIFICATIONS_KEY = "trajectory_worker_notifications"
#: Statement timeout of purge and maintenance transactions; far above the 5 s request timeout.
LONG_STATEMENT_TIMEOUT = "60s"


def utc(value: datetime) -> datetime:
    """A stored timestamp as an aware UTC datetime (SQLite hands back naive UTC values)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def worker_setting(settings, name: str, env: str, default: int) -> int:
    """An integer worker setting: the WorkerSettings attribute when it holds one, else the environment (SPEC §13)."""
    value = getattr(settings, name, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return integer(env, default)


def gc_retry_delay(attempts: int) -> timedelta:
    """Backoff after the ``attempts``-th failure of a GC entry: 30 s, doubling, at most 6 h."""
    exponent = min(max(attempts, 1) - 1, 20)
    return timedelta(seconds=min(GC_RETRY_MAX_SECONDS, GC_RETRY_BASE_SECONDS * 2 ** exponent))


def archive_marker_key(trajectory_id: str) -> str:
    """``trajectory_worker_state`` key of the segment upload in flight for a trajectory."""
    return f"archive:{trajectory_id}"


async def allow_long_statements(db) -> None:
    """PostgreSQL: lift the request-serving statement timeout for the rest of this transaction.

    Purges, pruning and partition maintenance may run statements far longer
    than the 5 s the trace engine grants per statement; ``SET LOCAL`` ends
    with the transaction. SQLite: nothing to do.
    """
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text(f"SET LOCAL statement_timeout = '{LONG_STATEMENT_TIMEOUT}'"))


async def enqueue_gc(db, kind: str, storage_key: str, reason: str, *, at: datetime | None = None,
                     delay: timedelta | None = None) -> None:
    """Queue an object key (``key``) or a directory prefix ending in "/" (``prefix``) for deletion.

    Only objects in the trajectory namespace qualify: the bucket may be the
    business assets bucket, whose user files GC must never touch.
    """
    if kind == GC_KEY:
        check_key(storage_key)
        if not storage_key.startswith(key_prefix()):
            raise ValueError(f"A GC key must lie in the trajectory namespace: {storage_key!r}")
    elif kind == GC_PREFIX:
        # Only one trajectory's directory: a shorter prefix would empty the whole namespace.
        namespace = key_prefix()
        name = storage_key[len(namespace):-1] if storage_key.startswith(namespace) else ""
        if not name or "/" in name or trajectory_prefix(name) != storage_key:
            raise ValueError(f"A GC prefix must name one trajectory directory: {storage_key!r}")
    else:
        raise ValueError(f"Unknown GC entry kind: {kind!r}")
    timestamp = at or now()
    db.add(TrajectoryGcQueue(kind=kind, storage_key=storage_key, reason=reason[:64], attempts=0,
                             next_attempt_at=timestamp + (delay or timedelta()), created_at=timestamp))


async def lock_trajectory(db, trajectory_id: str) -> SessionTrajectory | None:
    """The trajectory row under ``SELECT ... FOR UPDATE`` (the writer serializes SQLite)."""
    return await db.get(SessionTrajectory, trajectory_id, with_for_update=True)


async def tombstone_trajectory(db, trajectory, *, reason: str, at: datetime | None = None) -> bool:
    """Tombstone a trajectory whose session was deleted; False when it already is one.

    Sets ``deleted_at`` and ``recording_status=deleted``; deletes hot events,
    records, record links, checkpoints, the summary and segment rows; marks
    payloads and exports deleted; queues the trajectory prefix (twice, see
    PREFIX_SWEEP_DELAY) and export objects for GC. The trajectory and payload
    rows remain as tombstones. Idempotency keys hold no content and age out
    through ArchiveService.prune_event_keys (the key table has no index to
    delete them by trajectory inside the caller's transaction). The deleted
    notification is published after the caller's transaction commits. On
    PostgreSQL the caller's transaction keeps the long statement timeout
    from here on (``allow_long_statements``).
    """
    await allow_long_statements(db)
    trajectory = await lock_trajectory(db, trajectory.id)
    if trajectory is None or trajectory.deleted_at is not None:
        return False
    timestamp = at or now()
    trajectory.deleted_at = timestamp
    trajectory.recording_status = "deleted"
    trajectory.stored_bytes = 0
    trajectory.updated_at = timestamp
    await _purge_content(db, trajectory, availability="deleted", reason=reason, at=timestamp)
    await db.execute(delete(TrajectorySessionSummary).where(TrajectorySessionSummary.trajectory_id == trajectory.id))
    publish_after_commit(db, trajectory, deleted=True)
    return True


async def expire_trajectory_content(db, trajectory, *, at: datetime | None = None) -> bool:
    """Expire a trajectory's content; False when it is already expired or tombstoned.

    Same as a tombstone except that the trajectory keeps
    ``recording_status=expired`` with ``content_expired_at``, the summary row
    and its statistics.
    """
    await allow_long_statements(db)
    trajectory = await lock_trajectory(db, trajectory.id)
    if trajectory is None or trajectory.deleted_at is not None or trajectory.content_expired_at is not None:
        return False
    timestamp = at or now()
    trajectory.content_expired_at = timestamp
    trajectory.recording_status = "expired"
    trajectory.stored_bytes = 0
    trajectory.updated_at = timestamp
    await _purge_content(db, trajectory, availability="expired", reason="content_expired", at=timestamp)
    await db.execute(update(TrajectorySessionSummary).where(TrajectorySessionSummary.trajectory_id == trajectory.id)
                     .values(recording_status="expired"))
    return True


async def _purge_content(db, trajectory, *, availability: str, reason: str, at: datetime) -> None:
    trajectory_id = trajectory.id
    for model in (TrajectoryEvent, TrajectoryRecordEvent, TrajectoryRecord, TrajectoryCheckpoint, TrajectorySegment):
        await db.execute(delete(model).where(model.trajectory_id == trajectory_id))
    # Deleted content is never downgraded to expired, and never resurrected.
    revocable = ("available",) if availability == "expired" else ("available", "expired")
    await db.execute(
        update(TrajectoryPayload)
        .where(TrajectoryPayload.trajectory_id == trajectory_id, TrajectoryPayload.availability.in_(revocable))
        .values(availability=availability, deleted_at=func.coalesce(TrajectoryPayload.deleted_at, at))
        .execution_options(synchronize_session="fetch")
    )
    exports = (await db.scalars(select(TrajectoryExport).where(
        TrajectoryExport.trajectory_id == trajectory_id, TrajectoryExport.status != "deleted"))).all()
    await delete_exports(db, exports, reason=reason, at=at)
    await db.execute(delete(TrajectoryWorkerState).where(TrajectoryWorkerState.key == archive_marker_key(trajectory_id)))
    prefix = trajectory_prefix(trajectory_id)
    await enqueue_gc(db, GC_PREFIX, prefix, reason, at=at)
    await enqueue_gc(db, GC_PREFIX, prefix, reason, at=at, delay=PREFIX_SWEEP_DELAY)


async def delete_exports(db, exports, *, reason: str, at: datetime | None = None) -> int:
    """Mark export rows deleted and queue their objects (a running build's in-flight object too).

    A build that is still running notices the status at completion and
    discards its archive: deletion wins. Returns the rows changed.
    """
    timestamp = at or now()
    changed = 0
    for export in exports:
        if export.status == "deleted":
            continue
        if export.storage_key:
            await enqueue_gc(db, GC_KEY, export.storage_key, reason, at=timestamp)
        export.status = "deleted"
        export.storage_key = None
        export.lease_owner = None
        export.lease_until = None
        export.updated_at = timestamp
        changed += 1
    return changed


@dataclass(frozen=True)
class AssetRevocation:
    """A live trajectory that referenced a deleted asset, with the ``artifact.removed`` event it needs."""
    trajectory: SessionTrajectory
    event_id: str
    data: dict


def artifact_removed_event_id(root_session_id: str, asset_id: str) -> str:
    """Deterministic ``artifact.removed`` id, so repeated deletions dedupe (SPEC §8.5)."""
    return "asset:" + hashlib.sha256(f"{root_session_id}:{asset_id}:deleted".encode()).hexdigest()


def artifact_removed_data(asset_id: str) -> dict:
    return {"artifact_id": asset_id, "availability": "deleted", "reason": "explicitly_deleted"}


async def revoke_asset(db, asset_id: str, *, at: datetime | None = None) -> list[AssetRevocation]:
    """Apply an ``asset.deleted`` control to the payloads of every trajectory (SPEC §8.5).

    Payload rows bound to the asset become ``deleted``. A copied blob is
    queued for GC once no available payload of the same trajectory still
    uses its key; asset references own no bytes. Returns the live
    trajectories that referenced the asset, each with the id and data of the
    ``artifact.removed`` event ingest appends to it. Meta asset rows are the
    caller's.
    """
    timestamp = at or now()
    # Revocation may touch the payloads of many trajectories; the rest of the caller's transaction gets the
    # long statement timeout (PostgreSQL).
    await allow_long_statements(db)
    rows = (await db.scalars(select(TrajectoryPayload).where(TrajectoryPayload.source_asset_id == asset_id)
                             .order_by(TrajectoryPayload.trajectory_id, TrajectoryPayload.payload_id))).all()
    if not rows:
        return []
    revoked = []
    for row in rows:
        if row.availability != "deleted":
            if row.availability == "available":
                revoked.append(row)
            row.availability = "deleted"
            row.deleted_at = row.deleted_at or timestamp
    await db.flush()
    queued = set()
    for row in revoked:
        target = (row.trajectory_id, row.storage_key)
        if row.storage_kind != "blob" or not row.storage_key or target in queued:
            continue
        shared = await db.scalar(select(TrajectoryPayload.payload_id).where(
            TrajectoryPayload.trajectory_id == row.trajectory_id, TrajectoryPayload.storage_kind == "blob",
            TrajectoryPayload.storage_key == row.storage_key, TrajectoryPayload.availability == "available").limit(1))
        if shared is None:
            try:
                await enqueue_gc(db, GC_KEY, row.storage_key, "asset_deleted", at=timestamp)
            except ValueError:
                # Ingest revokes inside its batch transaction: a key outside the
                # trajectory namespace is never deleted, and must not fail the batch.
                log.warning("Revoked copy outside the trajectory namespace kept trajectory_id=%s", row.trajectory_id)
            queued.add(target)
    live = (await db.scalars(select(SessionTrajectory).where(
        SessionTrajectory.id.in_(sorted({row.trajectory_id for row in rows})),
        SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None),
    ).order_by(SessionTrajectory.id))).all()
    return [AssetRevocation(trajectory, artifact_removed_event_id(trajectory.session_id, asset_id),
                            artifact_removed_data(asset_id)) for trajectory in live]


# -- Notifications after commit --

def publish_after_commit(db, trajectory, *, deleted: bool = False) -> None:
    """Publish ``trajectory.available`` for ``trajectory`` once the outer transaction of ``db`` commits.

    A rollback discards it. Publication goes through
    ``trajectory.worker.notify.publish_available`` and never raises.
    """
    session = getattr(db, "sync_session", db)
    snapshot = SimpleNamespace(id=trajectory.id, user_id=trajectory.user_id, session_id=trajectory.session_id,
                               committed_seq=trajectory.committed_seq)
    session.info.setdefault(NOTIFICATIONS_KEY, {})[trajectory.id] = (snapshot, deleted)


@sa_event.listens_for(SyncSession, "after_commit")
def _publish_committed(session) -> None:
    # SAVEPOINT releases dispatch after_commit too; only the outer commit counts.
    if session.in_nested_transaction():
        return
    pending = session.info.pop(NOTIFICATIONS_KEY, None)
    if not pending:
        return
    try:
        notify = importlib.import_module("trajectory.worker.notify")
    except ImportError as exc:
        log.warning("Trajectory notifications unavailable: %s", type(exc).__name__)
        return
    for snapshot, deleted in pending.values():
        try:
            notify.publish_available(snapshot, deleted=deleted)
        except Exception as exc:
            log.warning("Trajectory %s notification failed: %s", snapshot.id, type(exc).__name__)


@sa_event.listens_for(SyncSession, "after_soft_rollback")
def _discard_rolled_back(session, previous_transaction) -> None:
    # A rollback that ends at a SAVEPOINT keeps the outer transaction's notifications.
    boundary = previous_transaction
    while boundary is not None and not boundary.nested and boundary.parent is not None:
        boundary = boundary.parent
    if boundary is not None and boundary.nested:
        return
    session.info.pop(NOTIFICATIONS_KEY, None)


@sa_event.listens_for(SyncSession, "after_transaction_end")
def _discard_uncommitted(session, transaction) -> None:
    # An outer transaction that closes without COMMIT publishes nothing; a
    # committed one already popped its notifications in after_commit.
    if transaction.parent is None:
        session.info.pop(NOTIFICATIONS_KEY, None)
