"""Retention service: content expiry, export expiry, deletion follow-up and the GC queue (SPEC §8.11).

Deletion happens in two steps. A trace transaction removes or tombstones the
rows and enqueues the object-storage keys and prefixes they referenced
(``trajectory.lifecycle``); the GC queue then deletes those objects outside
any transaction. A failing entry backs off exponentially from 30 seconds to
6 hours while the queue keeps serving the entries that are due, so a broken
key cannot starve the rest.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update

from core.log import create_logger
from trajectory.lifecycle import (GC_KEY, GC_PREFIX, delete_exports, expire_trajectory_content, gc_retry_delay,
    lock_trajectory, tombstone_trajectory, utc, worker_setting)
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryExport, TrajectoryGcQueue, TrajectoryMetaSession
from trajectory.types import now

log = create_logger("trajectory.worker.retention")

#: Trajectories or exports handled by one sweep; the rest waits for the next pass.
SWEEP_LIMIT = 100
ERROR_TEXT_LIMIT = 1000


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

    async def run_once(self) -> dict:
        """One pass over every retention duty; returns what each did."""
        return {
            "tombstoned": await self.tombstone_deleted_sessions(),
            "expired": await self.expire_due_content(),
            "exports_expired": await self.expire_exports(),
            "gc_processed": await self.process_gc_queue(),
        }

    async def tombstone(self, db, trajectory, *, reason: str) -> None:
        """Tombstone ``trajectory`` inside the caller's trace transaction (ingest applies ``session.deleted``).

        The deleted notification is published once that transaction commits.
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
            trajectory = await lock_trajectory(db, trajectory_id)
            if trajectory is None:
                return False
            if inactive_before is not None and utc(trajectory.last_activity_at) >= inactive_before:
                return False
            return await expire_trajectory_content(db, trajectory)

    async def expire_due_content(self) -> int:
        """Expire trajectories inactive for TRAJECTORY_CONTENT_RETENTION_DAYS; returns how many."""
        cutoff = now() - timedelta(days=self.content_retention_days)
        async with trace_session() as db:
            candidates = (await db.scalars(
                select(SessionTrajectory.id)
                .where(SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None),
                       SessionTrajectory.last_activity_at < cutoff)
                .order_by(SessionTrajectory.last_activity_at, SessionTrajectory.id)
                .limit(SWEEP_LIMIT)
            )).all()
        expired = 0
        for trajectory_id in candidates:
            expired += await self._expire(trajectory_id, cutoff)
        return expired

    async def tombstone_deleted_sessions(self) -> int:
        """Tombstone live trajectories whose replicated session is deleted.

        ``session.deleted`` controls normally tombstone at once; this sweep
        covers a deletion that only reached the worker through metadata sync.
        """
        async with trace_session() as db:
            candidates = (await db.scalars(
                select(SessionTrajectory.id)
                .join(TrajectoryMetaSession, TrajectoryMetaSession.id == SessionTrajectory.session_id)
                .where(SessionTrajectory.deleted_at.is_(None), TrajectoryMetaSession.is_deleted.is_(True))
                .order_by(SessionTrajectory.id)
                .limit(SWEEP_LIMIT)
            )).all()
        tombstoned = 0
        for trajectory_id in candidates:
            async with trace_session() as db:
                trajectory = await lock_trajectory(db, trajectory_id)
                if trajectory is not None:
                    tombstoned += await tombstone_trajectory(db, trajectory, reason="session_deleted")
        return tombstoned

    async def expire_exports(self) -> int:
        """Delete exports older than TRAJECTORY_EXPORT_RETENTION_DAYS and queue their objects; returns how many."""
        cutoff = now() - timedelta(days=self.export_retention_days)
        async with trace_session() as db:
            rows = (await db.scalars(
                select(TrajectoryExport)
                .where(TrajectoryExport.status != "deleted", TrajectoryExport.created_at < cutoff)
                .order_by(TrajectoryExport.created_at, TrajectoryExport.id)
                .limit(SWEEP_LIMIT)
                .with_for_update()
            )).all()
            return await delete_exports(db, rows, reason="export_expired")

    async def process_gc_queue(self, limit: int = 100) -> int:
        """Delete the objects of due GC entries, oldest due first; returns the entries completed.

        Object deletion runs outside any database transaction. A completed
        entry is removed; a failed one records its error and moves back by
        ``gc_retry_delay(attempts)``, which lets later entries through.
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
                if kind == GC_PREFIX:
                    objects += await self.blob_store.delete_prefix(storage_key)
                elif kind == GC_KEY:
                    await self.blob_store.delete(storage_key)
                    objects += 1
                else:
                    raise ValueError(f"Unknown GC entry kind: {kind!r}")
            except Exception as exc:
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
