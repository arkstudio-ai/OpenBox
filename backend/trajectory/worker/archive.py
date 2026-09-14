"""Archive service: cold event segments, partitions and idempotency-key pruning (SPEC §8.10).

Projected events leave the hot ``trajectory_events`` table in contiguous
segments of at most TRAJECTORY_SEGMENT_EVENTS events and
TRAJECTORY_SEGMENT_MAX_BYTES of uncompressed JSONL (``trajectory.segments``).
A segment is uploaded under its range key, read back and verified, and only
then does one trace transaction insert the segment row, advance
``archived_seq`` and delete the hot rows of the range. Until that commit the
hot rows stay authoritative, so a crash or failure in between loses nothing:
the retry writes the same key again (``if_absent=False``), and an upload whose
range changed meanwhile is handed to the GC queue through the in-flight marker
kept in ``trajectory_worker_state``.

On PostgreSQL the service also keeps a week of daily partitions ahead and
drops old partitions once archival has emptied them.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import orjson
from sqlalchemy import delete, func, or_, select, text

from core.log import create_logger
from trajectory.lifecycle import GC_KEY, archive_marker_key, enqueue_gc, utc, worker_setting
from trajectory.segments import decode_segment, encode_segment
from trajectory.storage import segment_key
from trajectory.store import partitions
from trajectory.store.database import get_trace_engine, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryEventKey, TrajectorySegment,
    TrajectoryWorkerState)
from trajectory.types import CorruptContent, now

log = create_logger("trajectory.worker.archive")

#: Stored event fields of one segment line, in SPEC §8.10 order.
SEGMENT_FIELDS = (
    "event_id", "trajectory_id", "seq", "type", "version", "user_id", "session_id", "source_session_id",
    "request_id", "call_id", "agent_id", "context", "data", "hints", "content_hash", "occurred_at", "recorded_at",
)
SEGMENT_CONTENT_TYPE = "application/zstd"
#: Cadence of the worker loop that calls run_once (SPEC §8.10); run_once itself never waits.
RUN_INTERVAL_SECONDS = 30
#: Bounds of one pass; whatever is left waits for the next pass.
TRAJECTORIES_PER_PASS = 100
SEGMENTS_PER_TRAJECTORY = 20
ROW_PAGE = 250
PARTITION_DAYS_AHEAD = 7
PARTITION_INTERVAL_SECONDS = 3600
PARTITION_RETRY_SECONDS = 60
#: Partition DDL may move default-partition rows; far above the 5 s request timeout.
MAINTENANCE_STATEMENT_TIMEOUT = "60s"
KEY_PRUNE_INTERVAL_SECONDS = 3600
KEY_PRUNE_BATCH = 1000
KEY_PRUNE_MAX_BATCHES = 50


class ArchiveAbandoned(Exception):
    """A verified segment can no longer be committed: the content was deleted or the range archived meanwhile."""


def timestamp_text(value: datetime) -> str:
    """Lossless UTC text of a stored timestamp (microseconds, ``Z``)."""
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def segment_row(event) -> dict:
    """One stored event (a row mapping of ``trajectory_events``) as its segment line object."""
    row = {field: event[field] for field in SEGMENT_FIELDS}
    row["seq"], row["version"] = int(row["seq"]), int(row["version"])
    row["occurred_at"], row["recorded_at"] = timestamp_text(row["occurred_at"]), timestamp_text(row["recorded_at"])
    return row


class ArchiveService:
    """Moves projected hot events into verified object-storage segments; runs in the single writer."""

    def __init__(self, settings, *, blob_store, metrics):
        self.settings = settings
        self.blob_store = blob_store
        self.metrics = metrics
        self.segment_events = worker_setting(settings, "segment_events", "TRAJECTORY_SEGMENT_EVENTS", 1000)
        self.segment_max_bytes = worker_setting(settings, "segment_max_bytes", "TRAJECTORY_SEGMENT_MAX_BYTES",
                                                4 * 1024 * 1024)
        self.segment_idle_seconds = worker_setting(settings, "segment_idle_seconds",
                                                   "TRAJECTORY_SEGMENT_IDLE_SECONDS", 300)
        self.hot_days = worker_setting(settings, "hot_days", "TRAJECTORY_HOT_DAYS", 7)
        self.dedupe_days = worker_setting(settings, "dedupe_days", "TRAJECTORY_DEDUPE_DAYS", 30)
        self._partitions_due = 0.0
        self._keys_due = 0.0
        self._reported_holes: set[tuple[str, int]] = set()

    async def run_once(self) -> int:
        """One pass: housekeeping when due, then every qualifying trajectory. Returns the events archived."""
        clock = time.monotonic()
        if clock >= self._partitions_due:
            try:
                await self.maintain_partitions()
            except Exception as exc:
                self._partitions_due = clock + PARTITION_RETRY_SECONDS
                log.warning("Trajectory partition maintenance failed: %s", type(exc).__name__)
        if clock >= self._keys_due:
            self._keys_due = clock + KEY_PRUNE_INTERVAL_SECONDS
            try:
                await self.prune_event_keys()
            except Exception as exc:
                log.warning("Trajectory event key pruning failed: %s", type(exc).__name__)
        archived = 0
        for trajectory_id in await self._candidates():
            try:
                archived += await self.archive_trajectory(trajectory_id)
            except Exception as exc:
                self.metrics.inc("segment_failures")
                log.warning("Trajectory %s archival failed: %s", trajectory_id, type(exc).__name__)
        await self._report_lag()
        return archived

    async def archive_trajectory(self, trajectory_id: str) -> int:
        """Archive the qualifying backlog of one trajectory, bounded per call; returns the events archived.

        A full backlog (TRAJECTORY_SEGMENT_EVENTS projected events) is cut
        into full segments; once the trajectory has been idle for
        TRAJECTORY_SEGMENT_IDLE_SECONDS its projected tail is archived too.
        Only events at or below ``projected_seq`` are ever archived.
        """
        archived = 0
        for _ in range(SEGMENTS_PER_TRAJECTORY):
            rows = await self._next_segment_rows(trajectory_id)
            if not rows:
                break
            stored = await self._store_segment(trajectory_id, rows)
            if not stored:
                break
            archived += stored
        return archived

    async def _candidates(self) -> list[str]:
        idle_before = now() - timedelta(seconds=self.segment_idle_seconds)
        backlog = SessionTrajectory.projected_seq - SessionTrajectory.archived_seq
        async with trace_session() as db:
            return list((await db.scalars(
                select(SessionTrajectory.id)
                .where(SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None),
                       SessionTrajectory.archived_seq < SessionTrajectory.projected_seq,
                       or_(backlog >= self.segment_events, SessionTrajectory.last_activity_at < idle_before))
                .order_by(backlog.desc(), SessionTrajectory.id)
                .limit(TRAJECTORIES_PER_PASS)
            )).all())

    async def _next_segment_rows(self, trajectory_id: str) -> list[dict]:
        """The contiguous hot rows of the next segment within the event and byte limits, or []."""
        events = TrajectoryEvent.__table__
        async with trace_session() as db:
            trajectory = await db.get(SessionTrajectory, trajectory_id)
            if trajectory is None or trajectory.deleted_at is not None or trajectory.content_expired_at is not None:
                return []
            backlog = trajectory.projected_seq - trajectory.archived_seq
            idle = utc(trajectory.last_activity_at) < now() - timedelta(seconds=self.segment_idle_seconds)
            if backlog <= 0 or (backlog < self.segment_events and not idle):
                return []
            expected = trajectory.archived_seq + 1
            last = trajectory.archived_seq + min(backlog, self.segment_events)
            rows, size = [], 0
            while expected <= last:
                page = (await db.execute(
                    select(events)
                    .where(events.c.trajectory_id == trajectory_id, events.c.seq >= expected, events.c.seq <= last)
                    .order_by(events.c.seq)
                    .limit(ROW_PAGE)
                )).mappings().all()
                if not page:
                    break
                for event in page:
                    if event["seq"] != expected:
                        break
                    row = segment_row(event)
                    line = len(orjson.dumps(row)) + 1
                    if rows and size + line > self.segment_max_bytes:
                        return rows
                    rows.append(row)
                    size += line
                    expected += 1
                else:
                    continue
                break
        if not rows and (trajectory_id, expected) not in self._reported_holes:
            # Reads of this range fail with a sequence gap as well; archival must not skip over it.
            self._reported_holes.add((trajectory_id, expected))
            self.metrics.inc("segment_failures")
            log.error("Trajectory %s hot event %s is missing; its archival cannot advance", trajectory_id, expected)
        return rows

    async def _store_segment(self, trajectory_id: str, rows: list[dict]) -> int:
        """Encode, upload, verify and commit one segment; returns its event count, 0 when it was not stored."""
        first = last = key = None
        try:
            rows, stored, meta = await asyncio.to_thread(self._encode, rows)
            first, last = rows[0]["seq"], rows[-1]["seq"]
            key = segment_key(trajectory_id, first, last)
            await self._mark_in_flight(trajectory_id, key, first, last)
            await self.blob_store.put(key, stored, content_type=SEGMENT_CONTENT_TYPE, if_absent=False)
            await self._verify(key, stored, meta, len(rows), first, last)
            await self._commit(trajectory_id, key, stored, meta, len(rows), first, last)
        except ArchiveAbandoned as exc:
            log.info("Trajectory %s segment %s-%s abandoned: %s", trajectory_id, first, last, exc)
            await self._release_upload(trajectory_id, key)
            return 0
        except Exception as exc:
            self.metrics.inc("segment_failures")
            log.warning("Trajectory %s segment %s-%s not archived: %s", trajectory_id, first, last,
                        type(exc).__name__)
            return 0
        self.metrics.inc("segment_uploads")
        return len(rows)

    def _encode(self, rows: list[dict]) -> tuple[list[dict], bytes, dict]:
        # The byte estimate while collecting rows is approximate; the encoder's
        # raw size is authoritative, so shrink until it fits (one row always does).
        while True:
            stored, meta = encode_segment(rows)
            raw = int(meta["raw_bytes"])
            if raw <= self.segment_max_bytes or len(rows) == 1:
                return rows, stored, meta
            rows = rows[:max(1, min(len(rows) - 1, len(rows) * self.segment_max_bytes // raw))]

    async def _mark_in_flight(self, trajectory_id: str, key: str, first: int, last: int) -> None:
        """Remember the upload before it starts; an earlier upload of another range becomes garbage."""
        marker_key = archive_marker_key(trajectory_id)
        async with trace_session() as db:
            marker = await db.get(TrajectoryWorkerState, marker_key)
            previous = marker.value.get("storage_key") if marker is not None and isinstance(marker.value, dict) else None
            if previous and previous != key and not await self._referenced(db, trajectory_id, previous):
                await enqueue_gc(db, GC_KEY, previous, "segment_superseded")
            value = {"storage_key": key, "from_seq": first, "to_seq": last}
            if marker is None:
                db.add(TrajectoryWorkerState(key=marker_key, value=value, updated_at=now()))
            else:
                marker.value, marker.updated_at = value, now()

    async def _verify(self, key: str, stored: bytes, meta: dict, count: int, first: int, last: int) -> None:
        data = await self.blob_store.get(key)
        if bytes(data) != stored:
            raise CorruptContent("Uploaded segment differs from the encoded segment")
        decoded = await asyncio.to_thread(decode_segment, data, expected_sha256=meta["sha256"])
        if len(decoded) != count or int(decoded[0]["seq"]) != first or int(decoded[-1]["seq"]) != last:
            raise CorruptContent("Uploaded segment does not decode to its range")

    async def _commit(self, trajectory_id: str, key: str, stored: bytes, meta: dict, count: int,
                      first: int, last: int) -> None:
        timestamp = now()
        async with trace_session() as db:
            trajectory = await db.get(SessionTrajectory, trajectory_id, with_for_update=True)
            if trajectory is None or trajectory.deleted_at is not None or trajectory.content_expired_at is not None:
                raise ArchiveAbandoned("the trajectory content was deleted")
            if trajectory.archived_seq != first - 1 or trajectory.projected_seq < last:
                raise ArchiveAbandoned("the archive watermark moved")
            removed = await db.execute(
                delete(TrajectoryEvent)
                .where(TrajectoryEvent.trajectory_id == trajectory_id, TrajectoryEvent.seq >= first,
                       TrajectoryEvent.seq <= last)
                .execution_options(synchronize_session=False)
            )
            if removed.rowcount != count:
                raise CorruptContent(f"{removed.rowcount} hot events in {first}-{last}, the segment holds {count}")
            db.add(TrajectorySegment(
                trajectory_id=trajectory_id, from_seq=first, to_seq=last, storage_key=key, event_count=count,
                raw_bytes=int(meta["raw_bytes"]), stored_bytes=len(stored), sha256=meta["sha256"],
                compression="zstd", created_at=timestamp,
            ))
            trajectory.archived_seq = last
            trajectory.stored_bytes += len(stored)
            trajectory.updated_at = timestamp
            await db.execute(delete(TrajectoryWorkerState).where(
                TrajectoryWorkerState.key == archive_marker_key(trajectory_id)))

    async def _release_upload(self, trajectory_id: str, key: str) -> None:
        async with trace_session() as db:
            if not await self._referenced(db, trajectory_id, key):
                await enqueue_gc(db, GC_KEY, key, "segment_abandoned")
            marker = await db.get(TrajectoryWorkerState, archive_marker_key(trajectory_id))
            if marker is not None and isinstance(marker.value, dict) and marker.value.get("storage_key") == key:
                await db.delete(marker)

    @staticmethod
    async def _referenced(db, trajectory_id: str, key: str) -> bool:
        return await db.scalar(select(TrajectorySegment.from_seq).where(
            TrajectorySegment.trajectory_id == trajectory_id, TrajectorySegment.storage_key == key).limit(1)) is not None

    async def _report_lag(self) -> None:
        async with trace_session() as db:
            projected, committed = (await db.execute(
                select(func.coalesce(func.sum(SessionTrajectory.projected_seq - SessionTrajectory.archived_seq), 0),
                       func.coalesce(func.sum(SessionTrajectory.committed_seq - SessionTrajectory.archived_seq), 0))
                .where(SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None))
            )).one()
        self.metrics.set_gauge("archive_lag_events", int(projected))
        # Watermark arithmetic, not a table count: live hot rows are exactly (archived_seq, committed_seq].
        self.metrics.set_gauge("hot_events_rows", int(committed))

    async def maintain_partitions(self) -> None:
        """PostgreSQL: create partitions for today..today+7 and drop empty ones older than TRAJECTORY_HOT_DAYS.

        Every helper call runs in its own short READ COMMITTED transaction with
        a 60 s statement timeout. Busy locks (PartitionLockUnavailable) and a
        refused isolation level are logged and retried after a minute instead
        of an hour. Old partitions that still hold rows are logged and counted
        in the ``stale_hot_partitions`` gauge. SQLite has no partitions.
        """
        clock = time.monotonic()
        if get_trace_engine().dialect.name != "postgresql":
            self._partitions_due = clock + PARTITION_INTERVAL_SECONDS
            return
        today = now().date()
        retry = False
        # A week ahead keeps new rows out of the default partition, which every
        # partition creation scans (and empties into the new partition).
        try:
            await self._maintenance(lambda connection: partitions.ensure_partitions(
                connection, today, PARTITION_DAYS_AHEAD))
        except partitions.PartitionLockUnavailable:
            retry = True
            log.warning("Trajectory event partitions were not created: their locks stayed busy")
        except partitions.PartitionIsolationError as exc:
            retry = True
            log.error("Trajectory event partition maintenance refused: %s", exc)
        oldest_hot_day = today - timedelta(days=self.hot_days)
        stale = []
        for name in await self._maintenance(partitions.list_partitions):
            day = partitions.partition_date(name)
            if day is None or day >= oldest_hot_day:
                continue
            try:
                dropped = await self._maintenance(
                    lambda connection, name=name: partitions.drop_partition_if_empty(connection, name))
            except (partitions.PartitionLockUnavailable, partitions.PartitionIsolationError) as exc:
                retry = True
                dropped = False
                log.warning("Trajectory event partition %s was not checked: %s", name, type(exc).__name__)
            if not dropped:
                stale.append(name)
        self.metrics.set_gauge("stale_hot_partitions", len(stale))
        if stale:
            log.warning("Trajectory event partitions older than %s days still exist: %s", self.hot_days,
                        ",".join(stale))
        self._partitions_due = clock + (PARTITION_RETRY_SECONDS if retry else PARTITION_INTERVAL_SECONDS)

    @staticmethod
    async def _maintenance(work):
        """Run one partition helper in its own READ COMMITTED transaction with the maintenance timeout."""
        async with get_trace_engine().connect() as connection:
            await connection.execution_options(isolation_level="READ COMMITTED")
            async with connection.begin():
                await connection.execute(text(f"SET LOCAL statement_timeout = '{MAINTENANCE_STATEMENT_TIMEOUT}'"))
                return await work(connection)

    async def prune_event_keys(self) -> int:
        """Delete idempotency keys older than TRAJECTORY_DEDUPE_DAYS once their events are archived.

        Keys of tombstoned or content-expired trajectories (and of trajectory
        rows that no longer exist) only need the age condition. Returns the
        number of keys deleted.
        """
        cutoff = now() - timedelta(days=self.dedupe_days)
        keys, trajectories = TrajectoryEventKey, SessionTrajectory
        doomed = (
            select(keys.event_id)
            .outerjoin(trajectories, trajectories.id == keys.trajectory_id)
            .where(keys.recorded_at < cutoff,
                   or_(trajectories.id.is_(None), keys.seq <= trajectories.archived_seq,
                       trajectories.deleted_at.is_not(None), trajectories.content_expired_at.is_not(None)))
            .limit(KEY_PRUNE_BATCH)
        )
        total = 0
        for _ in range(KEY_PRUNE_MAX_BATCHES):
            async with trace_session() as db:
                removed = (await db.execute(
                    delete(keys).where(keys.event_id.in_(doomed)).execution_options(synchronize_session=False)
                )).rowcount
            total += removed
            if removed < KEY_PRUNE_BATCH:
                break
        return total
