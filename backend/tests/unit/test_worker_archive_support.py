"""Shared pieces of the archive, retention and export tests: SQLite trace database, blob store, metrics, seeds.

``trajectory.segments`` (w2-projection) and ``trajectory.worker.notify``
(w2-ingest) are wave-2 interfaces owned by other packages. While they are not
importable, importing this module installs minimal stand-ins that follow the
agreed signatures; the real modules are used as soon as they exist. Import
this module before ``trajectory.worker.archive`` or ``trajectory.export``.
"""
from __future__ import annotations

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
                  dedupe_days=30, content_retention_days=180, export_retention_days=30)
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
