"""Checkpoint wire compatibility, byte bounds and interrupted capture recovery."""
import asyncio
import hashlib
import json
import threading
import tracemalloc

import pytest
from sqlalchemy import select, update

from tests.unit.test_worker_projection import RUN, SESSION, TRAJECTORY, USER, _event
from tests.unit.test_worker_projection_support import (Ingest, Metrics, add_meta, add_trajectory, blobs,  # noqa: F401
    settings, trace_db)
from trajectory.projector import empty_state, replay
from trajectory.read_budget import ReadTooLarge
from trajectory.repository import get_checkpoint
from trajectory.storage import decode_blob
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryCheckpoint, TrajectoryRecord
from trajectory.types import canonical, canonical_chunks, digest
from trajectory.worker.checkpoint import CheckpointSpool, blocking
from trajectory.worker.projection import ProjectionService


@pytest.mark.parametrize("value", [None, -0.0, 1e-7, {"中文": ["🌱", "\\\"\n", 2**70, False, None]},
                                  {"records": {"z": {}, "a": {"x": 1.23456789012345}}}])
def test_streaming_digest_is_byte_compatible_with_existing_checkpoints(value):
    assert b"".join(canonical_chunks(value)) == canonical(value)
    assert digest(value) == hashlib.sha256(canonical(value)).hexdigest()


def test_checkpoint_spool_preserves_state_digest_and_page_format(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_CHECKPOINT_PAGE_BYTES", "180")
    records = [{"record_id": name, "start_seq": str(seq), "data": {"text": "中文🌱" * 6}}
               for seq, name in [(3, "a"), (1, 'z"'), (2, "中"), (2, "b")]]
    state = {**empty_state(), "through_seq": "3", "records": {r["record_id"]: r for r in records}}
    spool = CheckpointSpool()
    try:
        for record in records:
            spool.add(record)
        assert spool.digest(state) == hashlib.sha256(canonical(state)).hexdigest()
        pages = list(spool.pages())
        assert len(pages) > 1
        found = {}
        for page in pages:
            blob = spool.blob("trj", page)
            raw = decode_blob(blob["stored"], blob["encoding"])
            assert raw == canonical(json.loads(raw)) and len(raw) <= 180
            found.update(json.loads(raw)["records"])
        assert found == state["records"]
    finally:
        spool.close()


def test_repeated_context_does_not_make_checkpoint_peak_memory_scale_with_session():
    # 150 MiB on the wire, but only one bounded record is encoded at a time.
    # Whole-state json.dumps would allocate the entire JSON string and bytes.
    context = ["上下文" * 10922] * 20
    spool = CheckpointSpool()
    tracemalloc.start()
    try:
        for seq in range(80):
            spool.add({"record_id": f"r:{seq:03}", "start_seq": str(seq), "data": {"messages": context}})
        size = sum(record.size for record in spool.records)
        assert size > 140 * 1024 * 1024
        assert len(spool.digest({**empty_state(), "through_seq": "80"})) == 64
        for page in spool.pages():
            blob = spool.blob("trj", page)
            assert blob["size_bytes"] <= 4 * 1024 * 1024
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 32 * 1024 * 1024
    finally:
        tracemalloc.stop()
        spool.close()


def test_checkpoint_size_limit_stops_repeated_values_without_building_full_json(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_CHECKPOINT_RECORD_BYTES", "4096")
    spool = CheckpointSpool()
    try:
        with pytest.raises(ReadTooLarge):
            spool.add({"record_id": "r", "start_seq": "1", "data": ["x" * 1024] * 1000})
        assert spool.file.tell() <= 4096 and not spool.records
    finally:
        spool.close()


async def test_checkpoint_capture_splits_reference_reads_and_keeps_historical_replay(trace_db, blobs, monkeypatch):
    await add_meta(SESSION, USER)
    await add_trajectory(TRAJECTORY, SESSION, USER)
    events = [_event(seq, "input.accepted", {"text": f"消息{seq}" * 500}, message_id=f"msg_{seq:03}", **RUN)
              for seq in range(1, 25)]
    await Ingest(blobs, inline_bytes=256).append(TRAJECTORY, events)
    service = ProjectionService(settings(checkpoint_interval=1, record_inline_bytes=512),
                                blob_store=blobs, metrics=Metrics())
    assert await service.project(TRAJECTORY) == len(events)
    monkeypatch.setenv("TRAJECTORY_CHECKPOINT_READ_BYTES", "16384")
    monkeypatch.setenv("TRAJECTORY_CHECKPOINT_PAGE_BYTES", "16384")
    assert await service.maybe_checkpoint(TRAJECTORY)
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, TRAJECTORY)
        saved = await db.get(TrajectoryCheckpoint, (TRAJECTORY, len(events)))
        result = await get_checkpoint(db, trajectory, len(events))
    expected = replay(events)
    assert result["state"] == expected and result["digest"] == hashlib.sha256(canonical(expected)).hexdigest()
    assert len(saved.state["record_pages"]) > 1


async def test_a_record_larger_than_the_budget_leaves_projection_available(trace_db, blobs, monkeypatch):
    await add_meta(SESSION, USER)
    await add_trajectory(TRAJECTORY, SESSION, USER)
    events = [_event(1, "input.accepted", {"text": "x" * 1024}, message_id="m")]
    await Ingest(blobs).append(TRAJECTORY, events)
    service = ProjectionService(settings(checkpoint_interval=1), blob_store=blobs, metrics=Metrics())
    assert await service.project(TRAJECTORY) == 1
    monkeypatch.setenv("TRAJECTORY_CHECKPOINT_READ_BYTES", "128")
    assert not await service.maybe_checkpoint(TRAJECTORY)
    assert ("checkpoint", TRAJECTORY) in service._retry
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, TRAJECTORY)
        assert trajectory.projected_seq == 1 and trajectory.checkpoint_seq == 0
        assert await db.scalar(select(TrajectoryCheckpoint)) is None


async def test_concurrent_projection_never_publishes_a_mixed_checkpoint(trace_db, blobs):
    await add_meta(SESSION, USER)
    await add_trajectory(TRAJECTORY, SESSION, USER)
    await Ingest(blobs).append(TRAJECTORY, [_event(1, "input.accepted", {"text": "hi"}, message_id="m")])
    service = ProjectionService(settings(checkpoint_interval=1), blob_store=blobs, metrics=Metrics())
    await service.project(TRAJECTORY)
    async with trace_session() as db:
        await db.execute(update(TrajectoryRecord).values(applied_seq=2))
    assert not await service.maybe_checkpoint(TRAJECTORY)
    async with trace_session() as db:
        assert await db.scalar(select(TrajectoryCheckpoint)) is None


async def test_cancelled_file_work_is_drained_before_its_spool_can_close():
    started, release = threading.Event(), threading.Event()
    def writing():
        started.set()
        release.wait(timeout=5)
        return True
    task = asyncio.create_task(blocking(writing))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
