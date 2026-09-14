"""Retention service: content expiry, export expiry, deletion follow-up and the GC queue (SPEC §8.11).

Deletion happens in two steps. A trace transaction removes or tombstones the
rows and enqueues the object-storage keys and prefixes they referenced
(``trajectory.lifecycle``); the GC queue then deletes those objects outside
any transaction. A failing entry backs off exponentially from 30 seconds to
6 hours while the queue keeps serving the entries that are due, so a broken
key cannot starve the rest. Right before deleting, the queue checks that the
object is not referenced again (content-addressed keys can be reused) and
refuses anything outside the trajectory namespace and the prefix of a live
trajectory.

The duties of one pass are independent: a purge that fails (a statement
timeout on a huge trajectory, say) is logged and the others still run.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, select, update

from core.log import create_logger
from trajectory.lifecycle import (GC_KEY, GC_PREFIX, allow_long_statements, delete_exports,
    expire_trajectory_content, gc_retry_delay, lock_trajectory, tombstone_trajectory, utc, worker_setting)
from trajectory.storage import key_prefix
from trajectory.store.database import trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryExport, TrajectoryGcQueue, TrajectoryMetaSession,
    TrajectoryPayload, TrajectorySegment)
from trajectory.types import now

log = create_logger("trajectory.worker.retention")

#: Trajectories or exports handled by one sweep; the rest waits for the next pass.
SWEEP_LIMIT = 100
ERROR_TEXT_LIMIT = 1000
#: Directory of export archives next to the trajectory directories (``trajectory.storage.export_key``).
EXPORTS_DIRECTORY = "_exports"


class GcRefused(ValueError):
    """A queued GC entry that must never run."""


class RetentionService:
    """Expires old content and exports, catches up on deleted sessions and drains the GC queue."""

    def __init__(self, settings, *, blob_store, metrics):
        self.settings = settings
        self.blob_store = blob_store
        self.metrics = metrics
        self.content_retention_days = worker_setting(settings, "content_retention_days",
                                                     "TRAJECTORY_CONTENT_RETENTION_DAYS", 180)
        self.export_retention_days = worker_setting(settings, "export_retention_days",
                                                    "TRAJECTORY_EXPORT_RETENTION_DAYS", 30)
        #: Committed work since the process started, for the worker's daily report (WorkerServices).
        #: released_bytes: stored bytes of expired or tombstoned trajectories and sizes of expired exports.
        self.totals = {"expired": 0, "tombstones": 0, "released_bytes": 0, "objects_deleted": 0, "gc_failures": 0}

    async def run_once(self) -> dict:
        """One pass over every retention duty; returns what each did (0 for a duty that failed)."""
        result = {}
        for name, duty in (("tombstoned", self.tombstone_deleted_sessions), ("expired", self.expire_due_content),
                           ("exports_expired", self.expire_exports), ("gc_processed", self.process_gc_queue)):
            try:
                result[name] = await duty()
            except Exception as exc:
                result[name] = 0
                log.warning("Trajectory retention step %s failed: %s", name, type(exc).__name__)
        return result

    async def tombstone(self, db, trajectory, *, reason: str) -> None:
        """Tombstone ``trajectory`` inside the caller's trace transaction (ingest applies ``session.deleted``).

        On PostgreSQL the rest of that transaction gets the long statement
        timeout, so a large purge cannot fail the batch over and over. The
        deleted notification is published once the transaction commits.
        """
        await tombstone_trajectory(db, trajectory, reason=reason)

    async def expire_content(self, trajectory_id: str, *, inactive_before: datetime | None = None) -> None:
        """Expire one trajectory's content in a transaction of its own.

        With ``inactive_before`` the trajectory is expired only when its last
        activity, read under the row lock, is still older than that instant.
        """
        await self._expire(trajectory_id, inactive_before)

    async def _expire(self, trajectory_id: str, inactive_before: datetime | None) -> bool:
        async with trace_session() as db:
            await allow_long_statements(db)
            trajectory = await lock_trajectory(db, trajectory_id)
            if trajectory is None:
                return False
            if inactive_before is not None and utc(trajectory.last_activity_at) >= inactive_before:
                return False
            released = trajectory.stored_bytes or 0
            expired = await expire_trajectory_content(db, trajectory)
        if expired:
            self.totals["expired"] += 1
            self.totals["released_bytes"] += released
        return expired

    async def expire_due_content(self) -> int:
        """Expire trajectories inactive for TRAJECTORY_CONTENT_RETENTION_DAYS; returns how many."""
        cutoff = now() - timedelta(days=self.content_retention_days)
        async with trace_session() as db:
            await allow_long_statements(db)
            candidates = (await db.scalars(
                select(SessionTrajectory.id)
                .where(SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None),
                       SessionTrajectory.last_activity_at < cutoff)
                .order_by(SessionTrajectory.last_activity_at, SessionTrajectory.id)
                .limit(SWEEP_LIMIT)
            )).all()
        expired = 0
        for trajectory_id in candidates:
            try:
                expired += await self._expire(trajectory_id, cutoff)
            except Exception as exc:
                log.warning("Trajectory %s content expiry failed: %s", trajectory_id, type(exc).__name__)
        return expired

    async def tombstone_deleted_sessions(self) -> int:
        """Tombstone live trajectories whose replicated session is deleted.

        ``session.deleted`` controls normally tombstone at once; this sweep
        covers a deletion that only reached the worker through metadata sync.
        Like the control, it only acts for the session's owner.
        """
        async with trace_session() as db:
            await allow_long_statements(db)
            candidates = (await db.scalars(
                select(SessionTrajectory.id)
                .join(TrajectoryMetaSession, and_(TrajectoryMetaSession.id == SessionTrajectory.session_id,
                                                  TrajectoryMetaSession.user_id == SessionTrajectory.user_id))
                .where(SessionTrajectory.deleted_at.is_(None), TrajectoryMetaSession.is_deleted.is_(True))
                .order_by(SessionTrajectory.id)
                .limit(SWEEP_LIMIT)
            )).all()
        tombstoned = 0
        for trajectory_id in candidates:
            try:
                async with trace_session() as db:
                    await allow_long_statements(db)
                    trajectory = await lock_trajectory(db, trajectory_id)
                    released = (trajectory.stored_bytes or 0) if trajectory is not None else 0
                    done = trajectory is not None and await tombstone_trajectory(db, trajectory,
                                                                                 reason="session_deleted")
            except Exception as exc:
                log.warning("Trajectory %s tombstone failed: %s", trajectory_id, type(exc).__name__)
                continue
            if done:
                tombstoned += 1
                self.totals["tombstones"] += 1
                self.totals["released_bytes"] += released
        return tombstoned

    async def expire_exports(self) -> int:
        """Delete exports older than TRAJECTORY_EXPORT_RETENTION_DAYS and queue their objects; returns how many."""
        cutoff = now() - timedelta(days=self.export_retention_days)
        async with trace_session() as db:
            await allow_long_statements(db)
            rows = (await db.scalars(
                select(TrajectoryExport)
                .where(TrajectoryExport.status != "deleted", TrajectoryExport.created_at < cutoff)
                .order_by(TrajectoryExport.created_at, TrajectoryExport.id)
                .limit(SWEEP_LIMIT)
                .with_for_update()
            )).all()
            released = sum(row.size_bytes or 0 for row in rows)
            deleted = await delete_exports(db, rows, reason="export_expired")
        self.totals["released_bytes"] += released
        return deleted

    async def process_gc_queue(self, limit: int = 100) -> int:
        """Delete the objects of due GC entries, oldest due first; returns the entries completed.

        Object deletion runs outside any database transaction. An entry whose
        object is referenced again completes without deleting it. A completed
        entry is removed; a failed or refused one records its error and moves
        back by ``gc_retry_delay(attempts)``, which lets later entries through.
        """
        started = now()
        async with trace_session() as db:
            entries = (await db.execute(
                select(TrajectoryGcQueue.id, TrajectoryGcQueue.kind, TrajectoryGcQueue.storage_key,
                       TrajectoryGcQueue.attempts)
                .where(TrajectoryGcQueue.next_attempt_at <= started)
                .order_by(TrajectoryGcQueue.next_attempt_at, TrajectoryGcQueue.id)
                .limit(max(1, limit))
            )).all()
        completed, failed, objects = [], [], 0
        for entry_id, kind, storage_key, attempts in entries:
            try:
                if await self._still_used(kind, storage_key):
                    completed.append(entry_id)
                    continue
                if kind == GC_PREFIX:
                    objects += await self.blob_store.delete_prefix(storage_key)
                else:
                    await self.blob_store.delete(storage_key)
                    objects += 1
            except Exception as exc:
                if isinstance(exc, GcRefused):
                    log.error("Trajectory GC entry %s refused: %s", entry_id, exc)
                failed.append((entry_id, attempts + 1, f"{type(exc).__name__}: {exc}"[:ERROR_TEXT_LIMIT]))
                continue
            completed.append(entry_id)
        finished = now()
        async with trace_session() as db:
            if completed:
                await db.execute(delete(TrajectoryGcQueue).where(TrajectoryGcQueue.id.in_(completed)))
            for entry_id, attempts, error in failed:
                await db.execute(update(TrajectoryGcQueue).where(TrajectoryGcQueue.id == entry_id).values(
                    attempts=attempts, next_attempt_at=finished + gc_retry_delay(attempts), last_error=error))
            depth = await db.scalar(select(func.count()).select_from(TrajectoryGcQueue))
        if objects:
            self.metrics.inc("gc_deleted", objects)
        if failed:
            self.metrics.inc("gc_failures", len(failed))
            log.warning("Trajectory GC rescheduled %s failed entries", len(failed))
        self.metrics.set_gauge("gc_queue_depth", int(depth or 0))
        return len(completed)

    @staticmethod
    async def _still_used(kind: str, storage_key: str) -> bool:
        """Whether a queued object is referenced again; GcRefused for an entry that must never run.

        Content-addressed blobs and range-named segments can be written again
        after their key was queued; a committed segment, an available payload
        or a live export that uses the key keeps the object. Refused: keys
        outside the trajectory namespace (the bucket may hold user assets),
        prefixes other than one trajectory directory, and the prefix of a
        trajectory that is neither deleted nor expired.
        """
        namespace = key_prefix()
        if not isinstance(storage_key, str) or not storage_key.startswith(namespace):
            raise GcRefused("the object lies outside the trajectory namespace")
        owner, _, rest = storage_key[len(namespace):].partition("/")
        if not owner:
            raise GcRefused("the entry names no trajectory directory")
        async with trace_session() as db:
            if kind == GC_PREFIX:
                if rest:
                    raise GcRefused("a prefix entry must name one trajectory directory")
                trajectory = await db.get(SessionTrajectory, owner)
                if trajectory is not None and trajectory.deleted_at is None and trajectory.content_expired_at is None:
                    raise GcRefused("the trajectory is live")
                return False
            if kind != GC_KEY:
                raise GcRefused(f"unknown entry kind {kind!r}")
            section, _, name = rest.partition("/")
            if owner == EXPORTS_DIRECTORY:
                query = select(TrajectoryExport.id).where(TrajectoryExport.storage_key == storage_key,
                                                          TrajectoryExport.status != "deleted")
            elif section == "segments":
                query = select(TrajectorySegment.from_seq).where(TrajectorySegment.trajectory_id == owner,
                                                                 TrajectorySegment.storage_key == storage_key)
            elif section == "blobs":
                query = select(TrajectoryPayload.payload_id).where(
                    TrajectoryPayload.trajectory_id == owner, TrajectoryPayload.sha256 == name,
                    TrajectoryPayload.storage_key == storage_key, TrajectoryPayload.availability == "available")
            else:
                return False
            return await db.scalar(query.limit(1)) is not None
