"""Shared pieces of the archive, retention and export tests: SQLite trace database, blob store, metrics, seeds.

``trajectory.segments`` (w2-projection) and ``trajectory.worker.notify``
(w2-ingest) are wave-2 interfaces owned by other packages. While they are not
importable, importing this module installs minimal stand-ins that follow the
agreed signatures; the real modules are used as soon as they exist. Import
this module before ``trajectory.worker.archive`` or ``trajectory.export``.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
import zstandard
from sqlalchemy import select

from trajectory.storage import MemoryBlobStore, decode_blob, set_blob_store
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryGcQueue, TrajectorySegment,
    TrajectoryWorkerState)
from trajectory.types import CorruptContent, canonical, now

AT = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


def _install(name: str, build) -> None:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        module = types.ModuleType(name)
        build(module)
        sys.modules[name] = module
        parent, _, child = name.rpartition(".")
        setattr(importlib.import_module(parent), child, module)


def _segments(module) -> None:
    def encode_segment(rows):
        raw = b"".join(canonical(row) + b"\n" for row in rows)
        stored = zstandard.ZstdCompressor(level=3).compress(raw)
        return stored, {"sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw), "stored_bytes": len(stored),
                        "event_count": len(rows), "from_seq": int(rows[0]["seq"]), "to_seq": int(rows[-1]["seq"])}

    def decode_segment(data, *, expected_sha256=None):
        raw = decode_blob(data, "zstd")
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise CorruptContent("Segment digest mismatch")
        return [json.loads(line) for line in raw.splitlines() if line]

    async def load_segment(blob_store, segment_row):
        def field(name):
            return segment_row[name] if isinstance(segment_row, dict) else getattr(segment_row, name)
        return decode_segment(await blob_store.get(field("storage_key")), expected_sha256=field("sha256"))

    module.encode_segment, module.decode_segment, module.load_segment = encode_segment, decode_segment, load_segment


def _notify(module) -> None:
    def publish_available(trajectory, *, deleted=False) -> None:
        return None

    module.publish_available = publish_available


_install("trajectory.segments", _segments)
_install("trajectory.worker.notify", _notify)


class FakeMetrics:
    """The Metrics interface (inc, set_gauge, snapshot) without a registry."""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, float] = {}

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    def set_gauge(self, name: str, value) -> None:
        self.gauges[name] = value

    def snapshot(self) -> dict:
        return {"counters": dict(self.counters), "gauges": dict(self.gauges), "uptime_seconds": 0.0}


def worker_settings(**overrides) -> types.SimpleNamespace:
    """The WorkerSettings attributes these services read, with SPEC §13 defaults."""
    values = dict(segment_events=1000, segment_max_bytes=4 * 1024 * 1024, segment_idle_seconds=300, hot_days=7,
                  dedupe_days=3, content_retention_days=180, export_retention_days=30)
    values.update(overrides)
    return types.SimpleNamespace(**values)


@pytest.fixture
async def trace_db(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    yield engine
    await close_trace_engine()


@pytest.fixture
def blob_store():
    store = MemoryBlobStore()
    set_blob_store(store)
    yield store
    set_blob_store(None)


@pytest.fixture
def notifications(monkeypatch):
    """Deleted and available notifications, in publication order."""
    published = []
    notify = importlib.import_module("trajectory.worker.notify")

    def publish_available(trajectory, *, deleted=False):
        published.append({"trajectory_id": trajectory.id, "user_id": trajectory.user_id,
                          "session_id": trajectory.session_id, "committed_seq": trajectory.committed_seq,
                          "deleted": deleted})

    monkeypatch.setattr(notify, "publish_available", publish_available)
    return published


async def add_trajectory(trajectory_id: str, *, committed: int = 0, projected: int | None = None, archived: int = 0,
                         session_id: str | None = None, user_id: str = "user_a", **extra) -> None:
    values = dict(id=trajectory_id, user_id=user_id, session_id=session_id or f"session_{trajectory_id}",
                  workspace_id="ws_a", started_at=AT, updated_at=AT, last_activity_at=now(), next_seq=committed + 1,
                  committed_seq=committed, projected_seq=committed if projected is None else projected,
                  archived_seq=archived, event_count=committed)
    values.update(extra)
    async with trace_session() as db:
        db.add(SessionTrajectory(**values))


def event_values(trajectory_id: str, seq: int, **overrides) -> dict:
    """A hot ``trajectory_events`` row as ingest stores it (microsecond timestamps included)."""
    recorded_at = overrides.pop("recorded_at", AT + timedelta(seconds=seq, microseconds=seq * 1111))
    session_id = overrides.pop("session_id", f"session_{trajectory_id}")
    # Like ingest, the context holds every non-null identity field; the id columns only index them.
    context = {"user_id": "user_a", "session_id": session_id, "source_session_id": session_id}
    context.update({field: overrides[field] for field in ("request_id", "call_id", "agent_id")
                    if overrides.get(field) is not None})
    values = {
        "trajectory_id": trajectory_id, "seq": seq, "recorded_on": recorded_at.astimezone(timezone.utc).date(),
        "event_id": f"evt_{trajectory_id}_{seq}", "type": "input.accepted", "version": 1, "user_id": "user_a",
        "session_id": session_id, "source_session_id": session_id, "request_id": None, "call_id": None,
        "agent_id": None, "context": context, "data": {"text": f"message {seq}"}, "hints": None,
        "content_hash": hashlib.sha256(f"{trajectory_id}:{seq}".encode()).hexdigest(),
        "occurred_at": recorded_at - timedelta(milliseconds=5), "recorded_at": recorded_at,
    }
    values.update(overrides)
    return values


async def add_events(rows: list[dict]) -> None:
    if rows:
        async with trace_session() as db:
            await db.execute(TrajectoryEvent.__table__.insert(), rows)


async def hot_seqs(trajectory_id: str) -> list[int]:
    async with trace_session() as db:
        return list((await db.scalars(select(TrajectoryEvent.seq).where(TrajectoryEvent.trajectory_id == trajectory_id)
                                      .order_by(TrajectoryEvent.seq))).all())


async def segment_ranges(trajectory_id: str) -> list[tuple[int, int]]:
    async with trace_session() as db:
        return [tuple(row) for row in (await db.execute(
            select(TrajectorySegment.from_seq, TrajectorySegment.to_seq)
            .where(TrajectorySegment.trajectory_id == trajectory_id).order_by(TrajectorySegment.from_seq))).all()]


async def gc_entries() -> list[tuple[str, str, str]]:
    async with trace_session() as db:
        return [tuple(row) for row in (await db.execute(
            select(TrajectoryGcQueue.kind, TrajectoryGcQueue.storage_key, TrajectoryGcQueue.reason)
            .order_by(TrajectoryGcQueue.id))).all()]


async def trajectory_row(trajectory_id: str) -> SessionTrajectory | None:
    async with trace_session() as db:
        return await db.get(SessionTrajectory, trajectory_id)


async def worker_state(key: str) -> dict | None:
    async with trace_session() as db:
        row = await db.get(TrajectoryWorkerState, key)
        return row.value if row is not None else None


def normalized(row) -> dict:
    """A stored event (hot row or segment line) as its segment fields with comparable values."""
    from trajectory.worker.archive import SEGMENT_FIELDS

    values = {}
    for field in SEGMENT_FIELDS:
        value = row[field]
        if field in ("occurred_at", "recorded_at"):
            value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        elif field in ("seq", "version"):
            value = int(value)
        values[field] = value
    return values


async def ingest_commit(trajectory_id: str, rows: list[dict], *, projected: int, last_activity_at: datetime) -> None:
    """Append ``rows`` as an ingest commit does: the events and the trajectory watermarks in one transaction."""
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id, with_for_update=True)
        await db.execute(TrajectoryEvent.__table__.insert(), rows)
        last = rows[-1]["seq"]
        trajectory.next_seq, trajectory.committed_seq, trajectory.event_count = last + 1, last, last
        trajectory.projected_seq, trajectory.last_activity_at, trajectory.updated_at = projected, last_activity_at, now()


async def assert_contiguous(trajectory_id: str) -> None:
    """Watermarks in order, and hot rows (read first) plus segments (read after them) cover 1..committed_seq.

    Reading the hot rows first means an archive commit in between can only
    show a range twice, never hide it.
    """
    trajectory = await trajectory_row(trajectory_id)
    assert trajectory.archived_seq <= trajectory.projected_seq <= trajectory.committed_seq
    hot = await hot_seqs(trajectory_id)
    ranges = await segment_ranges(trajectory_id)
    if hot:
        assert hot == list(range(hot[0], hot[-1] + 1)), f"hot events have a hole: {hot}"
    following = 1
    for first, last in ranges:
        assert first == following and last >= first, f"segments are not contiguous: {ranges}"
        following = last + 1
    missing = set(range(1, trajectory.committed_seq + 1)) - set(hot) - set(range(1, following))
    assert not missing, f"events neither hot nor archived: {sorted(missing)[:10]}"


async def archived_rows(blob_store, trajectory_id: str) -> list[dict]:
    """Every archived event of a trajectory, downloaded and verified from its segment objects."""
    from trajectory.segments import load_segment

    async with trace_session() as db:
        segments = (await db.scalars(select(TrajectorySegment).where(TrajectorySegment.trajectory_id == trajectory_id)
                                     .order_by(TrajectorySegment.from_seq))).all()
    rows = []
    for segment in segments:
        rows.extend(await load_segment(blob_store, segment))
    return rows


async def interleave_ingest_and_archive(blob_store, trajectory_id: str, *, batches: int, per_batch: int, lag: int,
                                        recorded_at=None) -> list[dict]:
    """Race ingest-style commits against two archive services and the GC queue; returns the ingested rows.

    The trajectory is idle throughout, so every pass archives whatever tail is
    projected and the candidate ranges keep changing while commits land;
    projection trails the commits by ``lag`` events. The invariants are checked
    after every archive call. At the end everything is archived and the
    segment objects must hold exactly the ingested rows.
    """
    from trajectory.worker.archive import ArchiveService
    from trajectory.worker.retention import RetentionService

    settings = worker_settings(segment_events=10)
    archives = [ArchiveService(settings, blob_store=blob_store, metrics=FakeMetrics()) for _ in range(2)]
    retention = RetentionService(settings, blob_store=blob_store, metrics=FakeMetrics())
    idle = now() - timedelta(hours=1)
    await add_trajectory(trajectory_id, last_activity_at=idle)
    inserted, done = [], asyncio.Event()

    async def archived_past(watermark: int) -> int:
        while (current := (await trajectory_row(trajectory_id)).archived_seq) <= watermark:
            await asyncio.sleep(0.01)
        return current

    async def ingest():
        watermark = 0
        try:
            for batch in range(batches):
                first = batch * per_batch + 1
                rows = [event_values(trajectory_id, seq, **({} if recorded_at is None else {"recorded_at": recorded_at(seq)}))
                        for seq in range(first, first + per_batch)]
                await ingest_commit(trajectory_id, rows, projected=max(0, rows[-1]["seq"] - lag), last_activity_at=idle)
                inserted.extend(rows)
                if batch % 10 == 9:
                    # Commits must keep landing while segments are cut, not only before or after.
                    watermark = await asyncio.wait_for(archived_past(watermark), 30)
                await asyncio.sleep(0)
        finally:
            done.set()

    async def archive(service):
        while not done.is_set():
            await service.archive_trajectory(trajectory_id)
            await assert_contiguous(trajectory_id)
            await asyncio.sleep(0)

    async def collect():
        while not done.is_set():
            await retention.process_gc_queue()
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(ingest(), *(archive(service) for service in archives), collect()), 120)
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id)
        trajectory.projected_seq = trajectory.committed_seq
    while await archives[0].archive_trajectory(trajectory_id):
        pass
    while await retention.process_gc_queue():
        pass
    await assert_contiguous(trajectory_id)
    assert await hot_seqs(trajectory_id) == []
    assert [normalized(row) for row in await archived_rows(blob_store, trajectory_id)] == [
        normalized(row) for row in inserted]
    return inserted
