"""The rebuild drill (trajectory.ops.rebuild) restores segments written by ArchiveService into a scratch trace database."""
import asyncio
import io
import json

import zstandard
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from tests.unit.test_worker_archive_support import FakeMetrics, add_events, add_trajectory, event_values, worker_settings
from trajectory.lifecycle import utc
from trajectory.ops import rebuild
from trajectory.storage import LocalBlobStore, decode_blob, segment_key
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryEvent, TrajectoryEventKey
from trajectory.types import now
from trajectory.worker.archive import ArchiveService


def stored_events(trajectory_id: str, count: int) -> list[dict]:
    return [
        event_values(trajectory_id, seq, type="request.delta" if seq % 3 == 0 else "tool.output",
                     request_id="req_1" if seq % 3 == 0 else None, call_id=None if seq % 3 == 0 else "call_1",
                     data={"text": f"chunk {seq} 你好 🌏", "ratio": seq / 3, "nested": {"list": [seq, None, True], "empty": {}}},
                     hints={"preview": {"text": f"chunk {seq}"}} if seq % 2 else None)
        for seq in range(1, count + 1)
    ]


async def create_schema(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    await engine.dispose()


async def archived_source(tmp_path) -> tuple[dict, list[dict]]:
    """A trace database whose trj_a is partly archived by ArchiveService; returns the drill environment and the rows."""
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'openbox_trace.db'}"
    scratch_url = f"sqlite+aiosqlite:///{tmp_path / 'openbox_trace_rebuild_drill.db'}"
    blobs = tmp_path / "blobs"
    rows = stored_events("trj_a", 7) + stored_events("trj_hot", 2)
    await close_trace_engine()
    init_trace_engine(source_url)
    try:
        await create_schema(source_url)
        await add_trajectory("trj_a", committed=7)
        await add_trajectory("trj_hot", committed=2)
        await add_trajectory("trj_deleted", deleted_at=now(), recording_status="deleted")
        await add_events(rows)
        async with trace_session() as db:
            for row in rows:
                db.add(TrajectoryEventKey(event_id=row["event_id"], trajectory_id=row["trajectory_id"], seq=row["seq"],
                                          content_hash=row["content_hash"], recorded_at=row["recorded_at"]))
        archive = ArchiveService(worker_settings(segment_events=3), blob_store=LocalBlobStore(blobs), metrics=FakeMetrics())
        # Two full segments; the active session keeps event 7 hot.
        assert await archive.archive_trajectory("trj_a") == 6
    finally:
        await close_trace_engine()
    await create_schema(scratch_url)
    environ = {"TRAJECTORY_DATABASE_URL": source_url, "REBUILD_SCRATCH_URL": scratch_url,
               "TRAJECTORY_BLOB_PROVIDER": "local", "TRAJECTORY_BLOB_LOCAL_PATH": str(blobs)}
    return environ, rows


async def drill(environ: dict) -> tuple[int, dict]:
    stdout = io.StringIO()
    code = await asyncio.to_thread(rebuild.main, [], environ=environ, stdout=stdout)
    return code, json.loads(stdout.getvalue())


async def test_rebuild_restores_segments_written_by_the_archive_service(tmp_path):
    environ, rows = await archived_source(tmp_path)
    code, report = await drill(environ)
    assert code == 0, report
    assert (report["ok"], report["trajectories"], report["segments"], report["events"], report["not_found"]) == (
        True, 2, 2, 9, [])
    reports = {item["trajectory_id"]: item for item in report["reports"]}
    for item in reports.values():
        assert item["source_digest"] == item["scratch_digest"]
        assert (item["segment_errors"], item["missing_seqs"], item["key_mismatches"], item["stale_hot_rows"]) == ([], 0, [], 0)
    assert (reports["trj_a"]["archived_seq"], reports["trj_a"]["segments"], reports["trj_hot"]["segments"]) == (6, 2, 0)

    # The scratch copy reads back through the trace models exactly as ingest stored the events.
    init_trace_engine(environ["REBUILD_SCRATCH_URL"])
    try:
        async with trace_session() as db:
            restored = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.trajectory_id,
                                                                          TrajectoryEvent.seq))).all()
            copies = {row.id: row for row in (await db.scalars(select(SessionTrajectory))).all()}
    finally:
        await close_trace_engine()
    expected = sorted(rows, key=lambda row: (row["trajectory_id"], row["seq"]))
    assert len(restored) == len(expected)
    for event, row in zip(restored, expected):
        for field, value in row.items():
            actual = getattr(event, field)
            assert (utc(actual) if field in ("occurred_at", "recorded_at") else actual) == value, (row["event_id"], field)
    assert sorted(copies) == ["trj_a", "trj_hot"]
    assert [(copy.committed_seq, copy.archived_seq, copy.projected_seq, copy.checkpoint_seq)
            for copy in (copies["trj_a"], copies["trj_hot"])] == [(7, 0, 0, 0), (2, 0, 0, 0)]


async def test_rebuild_rejects_segment_objects_that_no_longer_match_their_rows(tmp_path):
    environ, _ = await archived_source(tmp_path)
    blobs = environ["TRAJECTORY_BLOB_LOCAL_PATH"]
    first = tmp_path / "blobs" / segment_key("trj_a", 1, 3)
    second = tmp_path / "blobs" / segment_key("trj_a", 4, 6)
    assert str(first).startswith(blobs)
    # Same lines, other compression: the sha256 still matches but the stored size does not.
    raw = decode_blob(first.read_bytes(), "zstd")
    first.write_bytes(zstandard.ZstdCompressor(level=19, write_checksum=True).compress(raw))
    stored = second.read_bytes()
    second.write_bytes(stored[: len(stored) // 2])

    code, report = await drill(environ)
    detail = {item["trajectory_id"]: item for item in report["reports"]}["trj_a"]
    assert code == 1 and not report["ok"]
    assert detail["segment_errors"][0].startswith("1-3: the object has") and "stored bytes" in detail["segment_errors"][0]
    assert detail["segment_errors"][1].startswith("4-6: ") and "zstd" in detail["segment_errors"][1]
    assert (detail["missing_seqs"], detail["first_missing_seqs"]) == (6, [1, 2, 3, 4, 5, 6])
