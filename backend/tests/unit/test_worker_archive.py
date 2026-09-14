"""ArchiveService on a SQLite trace database (SPEC §8.10): selection, limits, verification, crash consistency, keys."""
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from tests.unit.test_worker_archive_support import (FakeMetrics, add_events, add_trajectory, blob_store, event_values,
    gc_entries, hot_seqs, segment_ranges, trace_db, trajectory_row, worker_settings, worker_state)
from trajectory.lifecycle import tombstone_trajectory
from trajectory.segments import decode_segment
from trajectory.storage import MemoryBlobStore, decode_blob, segment_key
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryEvent, TrajectoryEventKey, TrajectorySegment
from trajectory.types import now
from trajectory.worker.archive import SEGMENT_FIELDS, ArchiveService
from trajectory.worker.retention import RetentionService

__all__ = ["blob_store", "trace_db"]


def idle_since(minutes: int = 10) -> datetime:
    return now() - timedelta(minutes=minutes)


async def seed(trajectory_id: str, count: int, **trajectory) -> None:
    await add_trajectory(trajectory_id, committed=trajectory.pop("committed", count), **trajectory)
    await add_events([event_values(trajectory_id, seq) for seq in range(1, count + 1)])


class CorruptingStore(MemoryBlobStore):
    """Stores segment uploads with a flipped last byte while ``corrupt`` is set."""

    def __init__(self):
        super().__init__()
        self.corrupt = True

    async def put(self, key, data, *, content_type, if_absent=True):
        if self.corrupt and "/segments/" in key:
            data = bytes(data[:-1]) + bytes([data[-1] ^ 1])
        await super().put(key, data, content_type=content_type, if_absent=if_absent)


async def test_run_once_archives_full_backlogs_and_idle_tails_only(trace_db, blob_store):
    idle = idle_since()
    await seed("trj_full", 2500)
    await seed("trj_idle", 30, last_activity_at=idle)
    await seed("trj_busy", 30)
    await seed("trj_lagging", 40, projected=12, last_activity_at=idle)
    await seed("trj_deleted", 30, last_activity_at=idle, deleted_at=idle, recording_status="deleted")
    await seed("trj_expired", 30, last_activity_at=idle, content_expired_at=idle, recording_status="expired")
    metrics = FakeMetrics()
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=metrics)

    assert await service.run_once() == 2000 + 30 + 12
    assert await segment_ranges("trj_full") == [(1, 1000), (1001, 2000)]
    assert await hot_seqs("trj_full") == list(range(2001, 2501))
    assert (await segment_ranges("trj_idle"), await hot_seqs("trj_idle")) == ([(1, 30)], [])
    # Archival never passes projection.
    assert (await segment_ranges("trj_lagging"), await hot_seqs("trj_lagging")) == ([(1, 12)], list(range(13, 41)))
    for trajectory_id in ("trj_busy", "trj_deleted", "trj_expired"):
        assert (await segment_ranges(trajectory_id), len(await hot_seqs(trajectory_id))) == ([], 30)
    watermarks = [(await trajectory_row(name)).archived_seq for name in ("trj_full", "trj_idle", "trj_lagging", "trj_busy")]
    assert watermarks == [2000, 30, 12, 0]
    assert sorted(blob_store.objects) == sorted([
        segment_key("trj_full", 1, 1000), segment_key("trj_full", 1001, 2000), segment_key("trj_idle", 1, 30),
        segment_key("trj_lagging", 1, 12)])
    assert metrics.counters == {"segment_uploads": 4}
    assert metrics.gauges == {"archive_lag_events": 500 + 30, "hot_events_rows": 500 + 30 + 28}
    assert await service.run_once() == 0


async def test_segment_lines_are_the_stored_rows_and_the_commit_moves_them(trace_db, blob_store):
    await add_trajectory("trj_a", committed=3, last_activity_at=idle_since())
    rows = [
        event_values("trj_a", 1, type="request.prepared", request_id="req_1", agent_id="agent_1",
                     context={"user_id": "user_a", "session_id": "session_trj_a", "run_id": "run_1", "generation": 2},
                     data={"input": {"text": "你好 🌏", "ratio": 1.5, "nested": {"b": [1, 2], "a": None}}},
                     hints={"preview": {"input": "你好"}}),
        event_values("trj_a", 2, type="tool.output", call_id="call_1", data={"output": "line\nnext", "chunk_index": 0}),
        event_values("trj_a", 3),
    ]
    await add_events(rows)
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics())

    assert await service.archive_trajectory("trj_a") == 3
    key = segment_key("trj_a", 1, 3)
    stored = blob_store.objects[key]
    raw = decode_blob(stored, "zstd")
    async with trace_session() as db:
        segment = await db.get(TrajectorySegment, ("trj_a", 1))
        trajectory = await db.get(SessionTrajectory, "trj_a")
    assert (segment.to_seq, segment.storage_key, segment.event_count, segment.compression) == (3, key, 3, "zstd")
    assert (segment.raw_bytes, segment.stored_bytes) == (len(raw), len(stored))
    assert segment.sha256 == hashlib.sha256(raw).hexdigest()
    assert (trajectory.archived_seq, trajectory.stored_bytes) == (3, len(stored))
    assert blob_store.content_types[key] == "application/zstd"
    lines = decode_segment(stored, expected_sha256=segment.sha256)
    for line, row in zip(lines, rows, strict=True):
        assert set(line) == set(SEGMENT_FIELDS)
        for field in SEGMENT_FIELDS:
            if field in ("occurred_at", "recorded_at"):
                # Lossless: microseconds survive archival.
                assert datetime.fromisoformat(line[field]) == row[field], field
            else:
                assert line[field] == row[field], field
    assert (await hot_seqs("trj_a"), await worker_state("archive:trj_a")) == ([], None)


async def test_active_trajectories_get_full_segments_and_bytes_cap_every_segment(trace_db, blob_store):
    await seed("trj_events", 10)
    service = ArchiveService(worker_settings(segment_events=3), blob_store=blob_store, metrics=FakeMetrics())
    assert await service.archive_trajectory("trj_events") == 9
    assert await segment_ranges("trj_events") == [(1, 3), (4, 6), (7, 9)]
    assert await hot_seqs("trj_events") == [10]

    await add_trajectory("trj_bytes", committed=30, last_activity_at=idle_since())
    await add_events([event_values("trj_bytes", seq, data={"text": "y" * 12000 if seq == 12 else "x" * 1500})
                      for seq in range(1, 31)])
    service = ArchiveService(worker_settings(segment_events=50, segment_max_bytes=8000), blob_store=blob_store,
                             metrics=FakeMetrics())
    assert await service.archive_trajectory("trj_bytes") == 30
    ranges = await segment_ranges("trj_bytes")
    assert [seq for first, last in ranges for seq in range(first, last + 1)] == list(range(1, 31))
    async with trace_session() as db:
        segments = (await db.scalars(select(TrajectorySegment).where(TrajectorySegment.trajectory_id == "trj_bytes"))).all()
    assert (12, 12) in ranges and len(ranges) > 5
    for segment in segments:
        # A single oversized event still forms a segment of its own.
        assert segment.raw_bytes <= 8000 or (segment.from_seq, segment.to_seq) == (12, 12)


async def test_a_segment_is_committed_only_after_it_reads_back_intact(trace_db):
    store, metrics = CorruptingStore(), FakeMetrics()
    await seed("trj_a", 4, last_activity_at=idle_since())
    service = ArchiveService(worker_settings(), blob_store=store, metrics=metrics)

    assert await service.archive_trajectory("trj_a") == 0
    assert metrics.counters == {"segment_failures": 1}
    assert (await segment_ranges("trj_a"), await hot_seqs("trj_a")) == ([], [1, 2, 3, 4])
    assert (await trajectory_row("trj_a")).archived_seq == 0

    store.corrupt = False
    assert await service.archive_trajectory("trj_a") == 4
    assert len(decode_segment(store.objects[segment_key("trj_a", 1, 4)])) == 4
    assert metrics.counters == {"segment_failures": 1, "segment_uploads": 1}
    # The retry wrote the same range key again, so nothing became garbage.
    assert await gc_entries() == []


async def test_an_upload_failure_keeps_the_hot_rows(trace_db, blob_store):
    metrics = FakeMetrics()
    await seed("trj_a", 5, last_activity_at=idle_since())
    blob_store.fail("put", times=1)
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=metrics)
    assert await service.archive_trajectory("trj_a") == 0
    assert (await hot_seqs("trj_a"), blob_store.objects) == ([1, 2, 3, 4, 5], {})
    assert await service.archive_trajectory("trj_a") == 5
    assert metrics.counters == {"segment_failures": 1, "segment_uploads": 1}


async def test_a_crash_between_upload_and_commit_is_retried_on_the_same_key(trace_db, blob_store, monkeypatch):
    await seed("trj_a", 5, last_activity_at=idle_since())
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics())
    commit = ArchiveService._commit

    async def killed(self, *args, **kwargs):
        raise RuntimeError("worker killed before the commit")

    monkeypatch.setattr(ArchiveService, "_commit", killed)
    assert await service.archive_trajectory("trj_a") == 0
    key = segment_key("trj_a", 1, 5)
    assert key in blob_store.objects
    assert (await segment_ranges("trj_a"), await hot_seqs("trj_a")) == ([], [1, 2, 3, 4, 5])
    assert (await trajectory_row("trj_a")).archived_seq == 0
    assert await worker_state("archive:trj_a") == {"storage_key": key, "from_seq": 1, "to_seq": 5}

    monkeypatch.setattr(ArchiveService, "_commit", commit)
    restarted = ArchiveService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics())
    assert await restarted.archive_trajectory("trj_a") == 5
    assert (await segment_ranges("trj_a"), await hot_seqs("trj_a")) == ([(1, 5)], [])
    assert (await worker_state("archive:trj_a"), await gc_entries()) == (None, [])
    assert list(blob_store.objects) == [key]


async def test_an_upload_orphaned_by_a_crash_is_collected_when_the_range_changes(trace_db, blob_store, monkeypatch):
    await seed("trj_a", 5, last_activity_at=idle_since())
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics())
    commit = ArchiveService._commit

    async def killed(self, *args, **kwargs):
        raise RuntimeError("worker killed before the commit")

    monkeypatch.setattr(ArchiveService, "_commit", killed)
    assert await service.archive_trajectory("trj_a") == 0
    orphan = segment_key("trj_a", 1, 5)

    # Two more events are ingested and projected before the retry.
    await add_events([event_values("trj_a", 6), event_values("trj_a", 7)])
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, "trj_a")
        trajectory.committed_seq = trajectory.projected_seq = 7
    monkeypatch.setattr(ArchiveService, "_commit", commit)
    assert await service.archive_trajectory("trj_a") == 7
    assert await segment_ranges("trj_a") == [(1, 7)]
    assert await gc_entries() == [("key", orphan, "segment_superseded")]

    retention = RetentionService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics())
    assert await retention.process_gc_queue() == 1
    assert list(blob_store.objects) == [segment_key("trj_a", 1, 7)]


async def test_content_deleted_before_the_commit_abandons_the_segment(trace_db, blob_store):
    await seed("trj_a", 3, last_activity_at=idle_since())
    metrics = FakeMetrics()

    async def delete_session(key):
        if "/segments/" in key:
            async with trace_session() as db:
                await tombstone_trajectory(db, await db.get(SessionTrajectory, "trj_a"), reason="session_deleted")

    blob_store.get_hook = delete_session
    assert await ArchiveService(worker_settings(), blob_store=blob_store, metrics=metrics).archive_trajectory("trj_a") == 0
    key = segment_key("trj_a", 1, 3)
    assert ("key", key, "segment_abandoned") in await gc_entries()
    assert (await segment_ranges("trj_a"), await hot_seqs("trj_a")) == ([], [])
    assert (await worker_state("archive:trj_a"), metrics.counters) == (None, {})


async def test_the_commit_refuses_a_range_whose_hot_rows_changed(trace_db, blob_store):
    await seed("trj_a", 3, last_activity_at=idle_since())
    metrics = FakeMetrics()

    async def lose_a_row(key):
        async with trace_session() as db:
            await db.execute(delete(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == "trj_a", TrajectoryEvent.seq == 2))

    blob_store.get_hook = lose_a_row
    assert await ArchiveService(worker_settings(), blob_store=blob_store, metrics=metrics).archive_trajectory("trj_a") == 0
    assert metrics.counters == {"segment_failures": 1}
    assert (await segment_ranges("trj_a"), await hot_seqs("trj_a")) == ([], [1, 3])
    assert (await trajectory_row("trj_a")).archived_seq == 0


async def test_a_missing_hot_event_stops_archival_at_the_hole(trace_db, blob_store):
    await add_trajectory("trj_a", committed=5, last_activity_at=idle_since())
    await add_events([event_values("trj_a", seq) for seq in (1, 2, 4, 5)])
    metrics = FakeMetrics()
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=metrics)
    assert await service.archive_trajectory("trj_a") == 2
    assert await segment_ranges("trj_a") == [(1, 2)]
    assert await service.archive_trajectory("trj_a") == 0
    assert metrics.counters == {"segment_uploads": 1, "segment_failures": 1}
    assert await hot_seqs("trj_a") == [4, 5]


async def test_expired_idempotency_keys_are_pruned_once_archived(trace_db, blob_store):
    old, recent = now() - timedelta(days=40), now() - timedelta(days=2)
    await add_trajectory("trj_a", committed=20, archived=10)
    await add_trajectory("trj_deleted", committed=60, deleted_at=old, recording_status="deleted")
    keys = [("old_archived", "trj_a", 5, old), ("old_hot", "trj_a", 15, old), ("recent_archived", "trj_a", 6, recent),
            ("old_deleted", "trj_deleted", 50, old), ("old_orphan", "trj_gone", 1, old),
            ("recent_orphan", "trj_gone", 2, recent)]
    async with trace_session() as db:
        for event_id, trajectory_id, seq, recorded_at in keys:
            db.add(TrajectoryEventKey(event_id=event_id, trajectory_id=trajectory_id, seq=seq,
                                      content_hash="0" * 64, recorded_at=recorded_at))
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics())
    assert await service.prune_event_keys() == 3
    async with trace_session() as db:
        remaining = (await db.scalars(select(TrajectoryEventKey.event_id).order_by(TrajectoryEventKey.event_id))).all()
    assert remaining == ["old_hot", "recent_archived", "recent_orphan"]


async def test_partition_maintenance_is_a_no_op_on_sqlite(trace_db, blob_store):
    metrics = FakeMetrics()
    service = ArchiveService(worker_settings(), blob_store=blob_store, metrics=metrics)
    await service.maintain_partitions()
    assert await service.run_once() == 0
    assert metrics.gauges == {"archive_lag_events": 0, "hot_events_rows": 0}
