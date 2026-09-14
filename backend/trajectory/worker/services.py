"""Worker service loops (SPEC §8.0, §8.1).

``WorkerServices`` holds the single-writer lock and runs every writer loop:
ingest, projection with checkpoints, archive and partition maintenance,
retention with the GC queue, exports, budgets and the heartbeat. A process
without the lock serves reads only and retries the lock every 10 seconds; a
writer that loses it (its lock connection died) stops writing at once.

On PostgreSQL every loop is its own task. SQLite allows one writer at a time,
so the embedded worker runs the same steps one after another in one task.
``run_once`` and ``drain`` run the steps synchronously for tests, the dev
harness and shutdown.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import socket
import uuid
from dataclasses import replace
from pathlib import Path

from core.log import create_logger
from trajectory.store.database import get_trace_engine
from trajectory.worker.budgets import BudgetService, write_heartbeat
from trajectory.worker.ingest import IngestService
from trajectory.worker.lock import writer_lock_for

log = create_logger("trajectory.worker.services")

LOCK_RETRY_SECONDS = 10.0
LOCK_VERIFY_SECONDS = 10.0
ARCHIVE_SECONDS = 30.0
PARTITION_SECONDS = 3600.0
RETENTION_SECONDS = 60.0
GC_SECONDS = 5.0
EXPORT_SECONDS = 2.0
BUDGET_SECONDS = 10.0
HEARTBEAT_SECONDS = 5.0
ERROR_BACKOFF_SECONDS = 5.0
GC_BATCH = 100
#: Steps that run whenever the sequential (SQLite) loop wakes; the others follow their own interval.
CONTINUOUS_STEPS = ("ingest", "projection")


def _build(module: str, name: str, settings, **kwargs):
    return getattr(importlib.import_module(module), name)(settings, **kwargs)


class WorkerServices:
    """Writer lock, writer loops and synchronous passes of one worker process."""

    def __init__(self, settings, *, blob_store=None, spool_dir=None, metrics=None, ingest=None, projection=None,
                 archive=None, retention=None, exports=None, budgets=None, lock=None):
        if spool_dir is not None:
            settings = replace(settings, spool_dir=Path(spool_dir))
        self.settings = settings
        if blob_store is None:
            from trajectory.storage import get_blob_store
            blob_store = get_blob_store()
        if metrics is None:
            from trajectory.worker.metrics import get_metrics
            metrics = get_metrics()
        self.blob_store, self.metrics = blob_store, metrics
        self.owner_id = f"{socket.gethostname()[:32]}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        common = {"blob_store": blob_store, "metrics": metrics}
        self.retention = retention or _build("trajectory.worker.retention", "RetentionService", settings, **common)
        self.ingest = ingest or IngestService(settings, retention=self.retention, **common)
        self.projection = projection or _build("trajectory.worker.projection", "ProjectionService", settings, **common)
        self.archive = archive or _build("trajectory.worker.archive", "ArchiveService", settings, **common)
        self.exports = exports or _build("trajectory.export", "ExportService", settings, owner_id=self.owner_id,
                                         **common)
        self.budgets = budgets or BudgetService(settings, metrics=metrics)
        self._lock = lock
        self._supervisor: asyncio.Task | None = None
        self._writer_tasks: list[asyncio.Task] = []
        self._stop: asyncio.Event | None = None
        self._projection_wake: asyncio.Event | None = None
        self._checkpoints: set[str] = set()
        self._last_checkpoints = 0

    @property
    def is_writer(self) -> bool:
        return self._lock is not None and bool(self._lock.held)

    # Lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        """Try the writer lock and start the loops; lock retries continue in the background."""
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._stop = asyncio.Event()
        self._projection_wake = asyncio.Event()
        if await self._ensure_writer():
            self._start_writer_loops()
        else:
            log.info("Trace writer lock is held elsewhere; serving reads and retrying")
        self._supervisor = asyncio.create_task(self._supervise(), name="trajectory-worker-supervisor")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await self._stop_writer_loops()
        if self.is_writer:
            try:
                await self.ingest.flush_state()
            except Exception as exc:
                log.warning("Ingest state not saved at shutdown error_type=%s", type(exc).__name__)
        if self._lock is not None:
            await self._lock.release()

    async def _supervise(self) -> None:
        while not self._stop.is_set():
            if not self.is_writer:
                if await self._sleep(LOCK_RETRY_SECONDS):
                    return
                if await self._ensure_writer():
                    log.info("Acquired the trace writer lock; starting writer loops")
                    self._start_writer_loops()
                continue
            if await self._sleep(LOCK_VERIFY_SECONDS):
                return
            if not await self._lock.verify():
                log.warning("Lost the trace writer lock; stopping writer loops")
                await self._stop_writer_loops()

    async def _ensure_writer(self) -> bool:
        if self._lock is None:
            self._lock = writer_lock_for(get_trace_engine())
        if self._lock.held:
            return True
        return await self._lock.acquire()

    def _steps(self) -> list[tuple[str, float, object]]:
        settings = self.settings
        return [("ingest", settings.ingest_poll_ms / 1000, self._ingest_step),
                ("projection", settings.projection_batch_ms / 1000, self._projection_step),
                ("archive", ARCHIVE_SECONDS, self._archive_step),
                ("partitions", PARTITION_SECONDS, self._partition_step),
                ("retention", RETENTION_SECONDS, self._retention_step),
                ("gc", GC_SECONDS, self._gc_step),
                ("exports", EXPORT_SECONDS, self._export_step),
                ("budgets", BUDGET_SECONDS, self._budget_step),
                ("heartbeat", HEARTBEAT_SECONDS, self._heartbeat_step)]

    def _start_writer_loops(self) -> None:
        if self._writer_tasks:
            return
        steps = self._steps()
        if get_trace_engine().dialect.name == "sqlite":
            self._writer_tasks = [asyncio.create_task(self._sequential(steps), name="trajectory-worker")]
        else:
            self._writer_tasks = [asyncio.create_task(self._loop(name, interval, step), name=f"trajectory-{name}")
                                  for name, interval, step in steps]

    async def _stop_writer_loops(self) -> None:
        tasks, self._writer_tasks = self._writer_tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self, name: str, interval: float, step) -> None:
        wake = self._projection_wake if name == "projection" else None
        while not self._stop.is_set():
            try:
                busy = await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Trajectory %s step failed error_type=%s", name, type(exc).__name__)
                if await self._sleep(ERROR_BACKOFF_SECONDS):
                    return
                continue
            if busy and name in CONTINUOUS_STEPS:
                # More work may be waiting: go again, but let the other loops run first
                # (a step that finished without suspending would otherwise hold the event loop).
                await asyncio.sleep(0)
                continue
            if await self._sleep(interval, wake):
                return

    async def _sequential(self, steps) -> None:
        loop = asyncio.get_running_loop()
        due = {name: 0.0 for name, _, _ in steps}
        while not self._stop.is_set():
            busy = False
            for name, interval, step in steps:
                if name not in CONTINUOUS_STEPS and loop.time() < due[name]:
                    continue
                try:
                    ran = await step()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Trajectory %s step failed error_type=%s", name, type(exc).__name__)
                    due[name] = loop.time() + ERROR_BACKOFF_SECONDS
                    continue
                due[name] = loop.time() + interval
                busy = busy or (bool(ran) and name in CONTINUOUS_STEPS)
            if not busy and await self._sleep(self.settings.ingest_poll_ms / 1000):
                return

    async def _sleep(self, seconds: float, wake: asyncio.Event | None = None) -> bool:
        """Wait up to ``seconds`` (less when ``wake`` fires); True once the services are stopping."""
        waiters = [asyncio.ensure_future(self._stop.wait())]
        if wake is not None:
            waiters.append(asyncio.ensure_future(wake.wait()))
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if wake is not None:
            wake.clear()
        return self._stop.is_set()

    # Steps --------------------------------------------------------------------------------

    async def _ingest_step(self) -> dict:
        result = await self.ingest.run_once()
        if result["trajectories"]:
            self._checkpoints.update(result["trajectories"])
            if self._projection_wake is not None:
                self._projection_wake.set()
        return result if result["lines"] else {}

    async def _projection_step(self) -> int:
        projected = await self.projection.run_once()
        checkpoints = 0
        for trajectory_id in sorted(self._checkpoints):
            checkpoints += int(bool(await self.projection.maybe_checkpoint(trajectory_id)))
        if not projected:
            # Caught up: every candidate had its chance at a checkpoint.
            self._checkpoints.clear()
        self._last_checkpoints = checkpoints
        return projected

    async def _archive_step(self) -> int:
        return await self.archive.run_once()

    async def _partition_step(self) -> None:
        await self.archive.maintain_partitions()

    async def _retention_step(self):
        return await self.retention.run_once()

    async def _gc_step(self) -> int:
        return await self.retention.process_gc_queue(limit=GC_BATCH)

    async def _export_step(self) -> int:
        return await self.exports.run_once()

    async def _budget_step(self) -> dict:
        return await self.budgets.run_once()

    async def _heartbeat_step(self) -> None:
        await asyncio.to_thread(write_heartbeat, self.settings.spool_dir,
                                ingest_lag_seconds=self.ingest.last_lag_seconds)

    # Synchronous passes ---------------------------------------------------------------

    async def run_once(self) -> dict:
        """One pass of every step: ingest, projection and checkpoints, archive, retention, exports, budgets."""
        return await self._pass(include_archive=True)

    async def _pass(self, *, include_archive: bool) -> dict:
        if not await self._ensure_writer():
            return {"writer": False}
        ingest = await self.ingest.run_once()
        self._checkpoints.update(ingest["trajectories"])
        self._last_checkpoints = 0
        projected = await self._projection_step()
        counters = {"writer": True, "ingest": ingest, "projected": projected, "checkpoints": self._last_checkpoints,
                    "archived": 0}
        if include_archive:
            await self.archive.maintain_partitions()
            counters["archived"] = await self.archive.run_once()
        counters["retention"] = await self.retention.run_once()
        counters["gc"] = await self.retention.process_gc_queue(limit=GC_BATCH)
        counters["exports"] = await self.exports.run_once()
        counters["budgets"] = await self.budgets.run_once()
        await self._heartbeat_step()
        return counters

    async def drain(self, timeout: float = 30.0, include_archive: bool = False) -> dict:
        """Flush this process's emitter, then repeat passes until one makes no progress (or ``timeout``)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await self._flush_emitter(min(5.0, timeout))
        totals = {"passes": 0, "lines": 0, "events": 0, "projected": 0, "checkpoints": 0, "archived": 0,
                  "gc": 0, "exports": 0, "trajectories": set(), "deleted_trajectories": set(),
                  "writer": False, "timed_out": False}
        ingest_totals: dict[str, int] = {}
        while True:
            counters = await self._pass(include_archive=include_archive)
            if not counters.get("writer"):
                break
            totals["writer"] = True
            totals["passes"] += 1
            ingest = counters["ingest"]
            for name, value in ingest.items():
                if isinstance(value, set):
                    totals[name] |= value
                else:
                    ingest_totals[name] = ingest_totals.get(name, 0) + value
            for name in ("projected", "checkpoints", "archived", "gc", "exports"):
                totals[name] += int(counters.get(name) or 0)
            progress = (ingest["lines"] or counters["projected"] or counters["checkpoints"] or counters["archived"]
                        or counters["gc"] or counters["exports"])
            if not progress:
                break
            if loop.time() >= deadline:
                totals["timed_out"] = True
                break
        totals["lines"], totals["events"] = ingest_totals.get("lines", 0), ingest_totals.get("events", 0)
        totals["ingest"] = ingest_totals
        return totals

    @staticmethod
    async def _flush_emitter(timeout: float) -> None:
        # Only an emitter this process already runs (embedded mode, tests): the
        # external worker must never start one of its own.
        from trajectory import emitter as emitter_module
        current = emitter_module._emitter
        if current is not None:
            await asyncio.to_thread(current.flush, timeout)
