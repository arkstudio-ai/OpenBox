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

Some objects no row ever references, such as the blobs of a batch that never
committed. The orphan sweep pages through the namespace and queues GC entries
for the old blobs and exports nothing uses; segments are left to their
trajectory's prefix deletion.

The duties of one pass are independent: a purge that fails (a statement
timeout on a huge trajectory, say) is logged and the others still run.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import and_, delete, func, or_, select, update

from core.log import create_logger
from trajectory.lifecycle import (GC_KEY, GC_PREFIX, allow_long_statements, delete_exports, enqueue_gc,
    expire_trajectory_content, gc_retry_delay, lock_trajectory, tombstone_trajectory, utc, worker_setting)
from trajectory.storage import check_key, key_prefix
from trajectory.store.database import trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryExport, TrajectoryGcQueue, TrajectoryMetaSession,
    TrajectoryPayload, TrajectorySegment, TrajectoryWorkerState)
from trajectory.types import now
from trajectory.worker.budgets import NORMAL, USER_BYTES_STATE_KEY, iso, utc_day

log = create_logger("trajectory.worker.retention")

#: Trajectories or exports handled by one sweep; the rest waits for the next pass.
SWEEP_LIMIT = 100
ERROR_TEXT_LIMIT = 1000
#: Directory of export archives next to the trajectory directories (``trajectory.storage.export_key``).
EXPORTS_DIRECTORY = "_exports"
#: The daily report is logged by the first retention pass of each UTC day at or after this time, so the
#: users it lists as degraded are those of that day (their budget resets at midnight).
REPORT_TIME = time(23, 55)
#: Work the daily report counts, in the order it lists it.
REPORT_COUNTERS = ("trajectories_expired", "tombstones_processed", "gc_objects_deleted", "gc_failures",
                   "gc_orphans_queued")
#: Keys the orphan sweep lists per pass (``RetentionService.sweep_orphans``).
ORPHAN_BATCH = 1000
#: Younger objects are never orphans: far beyond any ingest or archive retry horizon.
ORPHAN_MIN_AGE_SECONDS = 7 * 24 * 3600
#: ``trajectory_worker_state`` key of the last key the orphan sweep listed.
ORPHAN_CURSOR_STATE_KEY = "gc.orphan_cursor"
ORPHAN_REASON = "orphan_object"
#: Keys per statement of the orphan sweep.
QUERY_CHUNK = 500


class DailyReport:
    """What retention did since the previous daily report, kept in memory (a restart begins a new period)."""

    def __init__(self, clock=now):
        self.clock = clock
        self.since = clock()
        self.counters = dict.fromkeys(REPORT_COUNTERS, 0)
        #: UTC day of the last report.
        self.reported_day: str | None = None

    def add(self, name: str, value: int = 1) -> None:
        self.counters[name] += value

    def due(self, moment: datetime) -> bool:
        return utc_day(moment) != self.reported_day and utc(moment).time() >= REPORT_TIME

    def emit(self, moment: datetime, *, degraded_trajectories: int, degraded_users: int) -> dict:
        """Log the report line and begin the next period; returns the fields logged."""
        fields = {"since": iso(self.since), "until": iso(moment), **self.counters,
                  "degraded_trajectories": degraded_trajectories, "degraded_users": degraded_users}
        log.info("Trajectory worker daily report %s", " ".join(f"{name}={value}" for name, value in fields.items()))
        self.since, self.reported_day = moment, utc_day(moment)
        self.counters = dict.fromkeys(REPORT_COUNTERS, 0)
        return fields


def _chunks(values: list, size: int = QUERY_CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


class GcRefused(ValueError):
    """A queued GC entry that must never run."""


class RetentionService:
    """Expires old content and exports, catches up on deleted sessions and drains the GC queue."""

    def __init__(self, settings, *, blob_store, metrics, clock=now):
        self.settings = settings
        self.blob_store = blob_store
        self.metrics = metrics
        self.content_retention_days = worker_setting(settings, "content_retention_days",
                                                     "TRAJECTORY_CONTENT_RETENTION_DAYS", 180)
        self.export_retention_days = worker_setting(settings, "export_retention_days",
                                                    "TRAJECTORY_EXPORT_RETENTION_DAYS", 30)
        #: Work since the previous daily report; ``clock`` gives its aware UTC instants.
        self.report = DailyReport(clock)

    async def run_once(self) -> dict:
        """One pass over every retention duty; returns what each did (0 for a duty that failed).

        The pass ends with the daily report when it is due (``report_if_due``).
        """
        result = {}
        for name, duty in (("tombstoned", self.tombstone_deleted_sessions), ("expired", self.expire_due_content),
                           ("exports_expired", self.expire_exports), ("gc_processed", self.process_gc_queue)):
            try:
                result[name] = await duty()
            except Exception as exc:
                result[name] = 0
                log.warning("Trajectory retention step %s failed: %s", name, type(exc).__name__)
        await self.report_if_due()
        return result

    async def report_if_due(self) -> dict | None:
        """Log the daily report when it is due (``DailyReport.due``); returns the fields logged.

        Degraded trajectories and users are read at that moment: live trajectories whose budget level is
        not normal, and the users over their daily bytes for the current UTC day. When they cannot be read
        the report waits for the next pass.
        """
        moment = self.report.clock()
        if not self.report.due(moment):
            return None
        try:
            async with trace_session() as db:
                trajectories = await db.scalar(select(func.count()).select_from(SessionTrajectory).where(
                    SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None),
                    SessionTrajectory.budget_level != NORMAL))
                state = await db.get(TrajectoryWorkerState, USER_BYTES_STATE_KEY)
        except Exception as exc:
            log.warning("Trajectory daily report postponed: %s", type(exc).__name__)
            return None
        value = state.value if state is not None and isinstance(state.value, dict) else {}
        exceeded = value.get("exceeded") if value.get("day") == utc_day(moment) else None
        return self.report.emit(moment, degraded_trajectories=int(trajectories or 0),
                                degraded_users=len(exceeded) if isinstance(exceeded, dict) else 0)

    async def tombstone(self, db, trajectory, *, reason: str) -> None:
        """Tombstone ``trajectory`` inside the caller's trace transaction (ingest applies ``session.deleted``).

        On PostgreSQL the rest of that transaction gets the long statement
        timeout, so a large purge cannot fail the batch over and over. The
        deleted notification is published once the transaction commits. The
        caller reports committed tombstones through ``tombstones_committed``.
        """
        await tombstone_trajectory(db, trajectory, reason=reason)

    def tombstones_committed(self, count: int) -> None:
        """Count tombstones a caller's transaction committed (``tombstone``) for the daily report."""
        self.report.add("tombstones_processed", count)

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
            expired = await expire_trajectory_content(db, trajectory)
        if expired:
            self.report.add("trajectories_expired")
        return expired

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
                    done = trajectory is not None and await tombstone_trajectory(db, trajectory,
                                                                                 reason="session_deleted")
            except Exception as exc:
                log.warning("Trajectory %s tombstone failed: %s", trajectory_id, type(exc).__name__)
                continue
            if done:
                tombstoned += 1
                self.report.add("tombstones_processed")
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
        self.report.add("gc_objects_deleted", objects)
        self.report.add("gc_failures", len(failed))
        if objects:
            self.metrics.inc("gc_deleted", objects)
        if failed:
            self.metrics.inc("gc_failures", len(failed))
            log.warning("Trajectory GC rescheduled %s failed entries", len(failed))
        self.metrics.set_gauge("gc_queue_depth", int(depth or 0))
        return len(completed)

    async def sweep_orphans(self, limit: int = ORPHAN_BATCH) -> int:
        """Queue GC key entries for old objects nothing references; returns how many were queued.

        Each call lists the next ``limit`` keys of the trajectory namespace after the cursor kept under
        ORPHAN_CURSOR_STATE_KEY and starts over after a short page. Objects younger than
        ORPHAN_MIN_AGE_SECONDS, or of unknown age, are left alone; the blobs and exports among the others are
        checked by the rules of ``_still_used``, a few statements per page (``_unused_keys``). An unused key
        without a queued key entry gets one (reason
        ``orphan_object``), which the GC pass checks again before it deletes the object. Prefixes are never
        queued.
        """
        limit, namespace, moment = max(1, limit), key_prefix(), now()
        async with trace_session() as db:
            state = await db.get(TrajectoryWorkerState, ORPHAN_CURSOR_STATE_KEY)
        cursor = state.value.get("after") if state is not None and isinstance(state.value, dict) else None
        if not isinstance(cursor, str) or not cursor.startswith(namespace):
            cursor = None
        page = await self.blob_store.list_objects(namespace, start_after=cursor, limit=limit)
        cutoff = moment.timestamp() - ORPHAN_MIN_AGE_SECONDS
        old = [key for key, modified in page if modified is not None and modified <= cutoff]
        async with trace_session() as db:
            unused = await self._unused_keys(db, namespace, old)
            queued = set()
            for chunk in _chunks(unused):
                queued.update((await db.scalars(select(TrajectoryGcQueue.storage_key).where(
                    TrajectoryGcQueue.kind == GC_KEY, TrajectoryGcQueue.storage_key.in_(chunk)))).all())
            orphans = [key for key in unused if key not in queued]
            for key in orphans:
                await enqueue_gc(db, GC_KEY, key, ORPHAN_REASON, at=moment)
            value = {"after": page[-1][0] if len(page) >= limit else None}
            state = await db.get(TrajectoryWorkerState, ORPHAN_CURSOR_STATE_KEY)
            if state is None:
                db.add(TrajectoryWorkerState(key=ORPHAN_CURSOR_STATE_KEY, value=value, updated_at=moment))
            else:
                state.value, state.updated_at = value, moment
        self.report.add("gc_orphans_queued", len(orphans))
        if orphans:
            log.info("Trajectory orphan sweep queued %s of %s listed objects for GC", len(orphans), len(page))
        return len(orphans)

    @staticmethod
    async def _unused_keys(db, namespace: str, keys: list[str]) -> list[str]:
        """The keys ``_still_used`` finds unused, in their order, decided for a chunk of keys per statement.

        Only blobs (no available payload row; checkpoint pages are payload rows too) and exports (no live export
        row) are candidates. A GC delete of a blob waits for in-flight ingest, projection and checkpoint batches
        (``ObjectGuard``) and export keys are never written twice, but an archive retry rewrites the same segment
        key without that guard, so segments are left to their trajectory's prefix deletion. Objects directly in
        the namespace, other reserved ("_") directories, other sections and keys ``enqueue_gc`` refuses are left
        alone too.
        """
        candidates = []
        for key in keys:
            owner, _, rest = key[len(namespace):].partition("/")
            section, _, name = rest.partition("/")
            if owner == EXPORTS_DIRECTORY:
                if not rest:
                    continue
            elif owner.startswith("_") or section != "blobs" or not name:
                continue
            try:
                check_key(key)
            except ValueError:
                continue
            candidates.append((key, owner, name))
        used = set()
        for chunk in _chunks(candidates):
            blobs, exports = {}, []
            for key, owner, name in chunk:
                if owner == EXPORTS_DIRECTORY:
                    exports.append(key)
                else:
                    blobs.setdefault(owner, {})[name] = key
            if blobs:
                rows = await db.execute(
                    select(TrajectoryPayload.trajectory_id, TrajectoryPayload.sha256, TrajectoryPayload.storage_key)
                    .where(TrajectoryPayload.availability == "available",
                           or_(*(and_(TrajectoryPayload.trajectory_id == owner, TrajectoryPayload.sha256.in_(names))
                                 for owner, names in blobs.items()))))
                used.update(storage_key for owner, sha256, storage_key in rows
                            if blobs[owner].get(sha256) == storage_key)
            if exports:
                used.update((await db.scalars(select(TrajectoryExport.storage_key).where(
                    TrajectoryExport.storage_key.in_(exports), TrajectoryExport.status != "deleted"))).all())
        return [key for key, *_ in candidates if key not in used]

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
