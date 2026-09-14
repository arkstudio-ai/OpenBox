"""WorkerServices (SPEC §8.0, §8.1): passes over every service, drain, writer lock and loops.

Projection, archive, retention, exports and metrics belong to other packages; fakes stand in for them
through the WorkerServices constructor (and, for the default wiring, through their module names).
"""
import asyncio
import sys
import types

import orjson
import pytest
from sqlalchemy import func, select

from trajectory import spool
from trajectory.storage import MemoryBlobStore
from trajectory.store.models import TrajectoryEvent
from trajectory.store.database import trace_session
from trajectory.worker import services as services_module
from trajectory.worker.services import WorkerServices
from tests.unit.test_worker_ingest import (FakeMetrics, FakeRetention, SpoolWriter, event, events_of,  # noqa: F401
    settings, trace_db)


class FakeProjection:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    async def run_once(self):
        self.calls.append("run_once")
        return self.results.pop(0) if self.results else 0

    async def project(self, trajectory_id, max_events=None):
        return 0

    async def maybe_checkpoint(self, trajectory_id):
        self.calls.append(("maybe_checkpoint", trajectory_id))
        return len([call for call in self.calls if call[0] == "maybe_checkpoint"]) == 1


class FakeArchive:
    def __init__(self):
        self.calls = []

    async def run_once(self):
        self.calls.append("run_once")
        return 0

    async def archive_trajectory(self, trajectory_id):
        return 0

    async def maintain_partitions(self):
        self.calls.append("maintain_partitions")


class FakeRetentionService(FakeRetention):
    async def run_once(self):
        self.calls.append(("run_once",))
        return {"expired": 0}

    async def expire_content(self, trajectory_id):
        return None

    async def process_gc_queue(self, limit=100):
        self.calls.append(("process_gc_queue", limit))
        return 0


class FakeExports:
    def __init__(self):
        self.calls = 0

    async def run_once(self):
        self.calls += 1
        return 0


def _services(settings, **overrides):
    parts = {"blob_store": MemoryBlobStore(), "metrics": FakeMetrics(), "projection": FakeProjection(),
             "archive": FakeArchive(), "retention": FakeRetentionService(), "exports": FakeExports()}
    parts.update(overrides)
    return WorkerServices(settings, **parts)


async def _event_count() -> int:
    async with trace_session() as db:
        return await db.scalar(select(func.count()).select_from(TrajectoryEvent))


async def test_run_once_drives_every_service(trace_db, settings):
    services = _services(settings)
    SpoolWriter(settings.spool_dir).events(event(), event())
    try:
        counters = await services.run_once()
    finally:
        await services.stop()
    trajectory, stored = await events_of("ses_1")
    assert len(stored) == 3 and counters["writer"] is True
    assert counters["ingest"]["trajectories"] == {trajectory.id}
    assert services.projection.calls == ["run_once", ("maybe_checkpoint", trajectory.id)]
    assert counters["checkpoints"] == 1 and counters["projected"] == 0
    assert services.archive.calls == ["maintain_partitions", "run_once"]
    assert services.retention.calls == [("run_once",), ("process_gc_queue", 100)]
    assert services.exports.calls == 1
    assert orjson.loads(spool.budgets_path(settings.spool_dir).read_bytes())["version"] == 1
    heartbeat = orjson.loads((settings.spool_dir / "control" / "worker.json").read_bytes())
    assert heartbeat["ingest_lag_seconds"] == 0.0


async def test_drain_repeats_passes_until_nothing_moves(trace_db, settings, monkeypatch):
    services = _services(settings, projection=FakeProjection([2, 1, 0]))
    SpoolWriter(settings.spool_dir).events(event(), event(session="ses_2"))
    flushed = []
    monkeypatch.setattr("trajectory.emitter._emitter", types.SimpleNamespace(flush=lambda timeout: flushed.append(timeout)))
    try:
        totals = await services.drain(timeout=10)
    finally:
        await services.stop()
    assert flushed == [5.0]
    assert totals["passes"] == 3 and totals["projected"] == 3 and totals["lines"] == 2 and totals["events"] == 2
    assert len(totals["trajectories"]) == 2 and totals["timed_out"] is False
    assert "run_once" not in services.archive.calls
    assert await _event_count() == 4


async def test_only_one_writer_holds_the_lock(trace_db, settings):
    first, second = _services(settings), _services(settings)
    await first.start()
    await second.start()
    try:
        assert first.is_writer and not second.is_writer
        assert await second.run_once() == {"writer": False}
        assert (await second.drain(timeout=1))["writer"] is False
    finally:
        await first.stop()
    try:
        assert not first.is_writer
        assert (await second.run_once())["writer"] is True and second.is_writer
    finally:
        await second.stop()


async def test_loops_ingest_in_the_background_and_stop_cleanly(trace_db, settings):
    from dataclasses import replace
    services = _services(replace(settings, ingest_poll_ms=10, projection_batch_ms=10))
    SpoolWriter(settings.spool_dir).events(event(), event())
    await services.start()
    try:
        for _ in range(200):
            if await _event_count() == 3:
                break
            await asyncio.sleep(0.02)
        assert await _event_count() == 3
        assert (settings.spool_dir / "control" / "worker.json").exists()
    finally:
        await services.stop()
    assert not services.is_writer and services._writer_tasks == []
    assert services.projection.calls.count("run_once") >= 1


async def test_a_writer_that_loses_its_lock_stops_writing(trace_db, settings, monkeypatch):
    class FlakyLock:
        held = False

        async def acquire(self):
            self.held = True
            return True

        async def verify(self):
            self.held = False
            return False

        async def release(self):
            self.held = False

    monkeypatch.setattr(services_module, "LOCK_VERIFY_SECONDS", 0.01)
    monkeypatch.setattr(services_module, "LOCK_RETRY_SECONDS", 3600)
    services = _services(settings, lock=FlakyLock())
    await services.start()
    try:
        for _ in range(100):
            if not services._writer_tasks:
                break
            await asyncio.sleep(0.01)
        assert services._writer_tasks == [] and not services.is_writer
    finally:
        await services.stop()


async def test_spool_dir_override_and_default_wiring(trace_db, settings, tmp_path, monkeypatch):
    built = {}

    def module(name, cls):
        def factory(settings_arg, **kwargs):
            built[cls] = kwargs
            return types.SimpleNamespace(settings=settings_arg, **kwargs)
        fake = types.ModuleType(name)
        setattr(fake, cls, factory)
        monkeypatch.setitem(sys.modules, name, fake)

    module("trajectory.worker.projection", "ProjectionService")
    module("trajectory.worker.archive", "ArchiveService")
    module("trajectory.worker.retention", "RetentionService")
    module("trajectory.export", "ExportService")
    metrics_module = types.ModuleType("trajectory.worker.metrics")
    metrics = FakeMetrics()
    metrics_module.get_metrics = lambda: metrics
    monkeypatch.setitem(sys.modules, "trajectory.worker.metrics", metrics_module)
    store = MemoryBlobStore()
    services = WorkerServices(settings, blob_store=store, spool_dir=tmp_path / "other-spool")
    assert services.settings.spool_dir == tmp_path / "other-spool"
    assert services.ingest.spool_dir == tmp_path / "other-spool"
    assert services.metrics is metrics and services.ingest.retention is services.retention
    for name in ("ProjectionService", "ArchiveService", "RetentionService"):
        assert built[name] == {"blob_store": store, "metrics": metrics}
    assert built["ExportService"]["owner_id"] == services.owner_id and built["ExportService"]["blob_store"] is store
