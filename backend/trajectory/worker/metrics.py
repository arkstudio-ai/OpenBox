"""Worker counters and gauges, served as JSON by ``GET /metrics`` (SPEC §8.13).

The names are the contract with ``trajectory.ops.cms`` and the gw2 drills, so a
snapshot always lists every one of them. The registry is process-wide and
thread-safe (blob uploads and file scans may report from worker threads), and
recording a value never raises. The worker services set the gauges of their
own loops; ``TraceDbSizeSampler`` samples ``trace_db_bytes`` every 5 minutes.
"""
import asyncio
import math
import os
import threading
import time

from sqlalchemy import text

from core.log import create_logger

log = create_logger("trajectory.worker.metrics")

#: SPEC §8.13: ``trace_db_bytes`` is sampled every 5 minutes.
TRACE_DB_SAMPLE_SECONDS = 300.0

COUNTERS = (
    "ingest_lines", "ingest_events", "duplicates", "idempotency_conflicts", "deleted_drops", "ownership_drops",
    "gaps_recorded", "producer_loss_events", "quarantined_files", "blob_puts", "blob_put_bytes",
    "blob_put_failures", "segment_uploads", "segment_failures", "gc_deleted", "gc_failures", "exports_built",
)
GAUGES = (
    "spool_bytes", "spool_files", "spool_oldest_age_seconds", "ingest_lag_seconds", "projection_lag_events",
    "archive_lag_events", "gc_queue_depth", "trace_db_bytes", "hot_events_rows", "trajectories_degraded",
    "trajectories_blocked", "stale_hot_partitions",
)


def _number(value) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


class Metrics:
    """Counters only grow; gauges hold the latest sample. Unknown names are kept and warned about once."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._counters: dict[str, int | float] = dict.fromkeys(COUNTERS, 0)
        self._gauges: dict[str, int | float] = dict.fromkeys(GAUGES, 0)
        self._warned: set[tuple[str, str]] = set()

    def inc(self, name: str, value=1) -> None:
        """Add ``value`` (a finite number, at least 0) to a counter."""
        number = _number(value)
        with self._lock:
            if not isinstance(name, str) or number is None or number < 0:
                self._warn_locked("invalid counter increment", repr(name))
                return
            if name not in self._counters:
                self._warn_locked("unknown counter", name)
                self._counters[name] = 0
            self._counters[name] += number

    def set_gauge(self, name: str, value) -> None:
        number = _number(value)
        with self._lock:
            if not isinstance(name, str) or number is None:
                self._warn_locked("invalid gauge value", repr(name))
                return
            if name not in self._gauges:
                self._warn_locked("unknown gauge", name)
            self._gauges[name] = number

    def snapshot(self) -> dict:
        with self._lock:
            return {"counters": dict(self._counters), "gauges": dict(self._gauges),
                    "uptime_seconds": round(max(0.0, self._clock() - self._started), 3)}

    def _warn_locked(self, problem: str, name: str) -> None:
        if (problem, name) not in self._warned:
            self._warned.add((problem, name))
            log.warning("Trajectory metrics: %s %s", problem, name)


_metrics: Metrics | None = None
_metrics_lock = threading.Lock()


def get_metrics() -> Metrics:
    """The process registry, created on first use."""
    global _metrics
    metrics = _metrics
    if metrics is None:
        with _metrics_lock:
            if _metrics is None:
                _metrics = Metrics()
            metrics = _metrics
    return metrics


def reset_metrics_for_tests() -> None:
    global _metrics
    with _metrics_lock:
        _metrics = None


def _sqlite_bytes(database: str) -> int:
    """The SQLite database file together with its WAL and shared-memory files."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.stat(database + suffix).st_size
        except FileNotFoundError:
            continue
    return total


async def trace_db_bytes(engine) -> int | None:
    """Bytes the trace database occupies: ``pg_database_size`` on PostgreSQL, its files on SQLite.

    None when there is nothing on disk to measure (an in-memory SQLite database).
    """
    if engine.dialect.name == "postgresql":
        async with engine.connect() as connection:
            return int(await connection.scalar(text("SELECT pg_database_size(current_database())")))
    database = engine.url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return await asyncio.to_thread(_sqlite_bytes, database)


class TraceDbSizeSampler:
    """Keeps the ``trace_db_bytes`` gauge current: one sample at start, then one per interval."""

    def __init__(self, *, interval: float = TRACE_DB_SAMPLE_SECONDS, metrics: Metrics | None = None):
        self._interval = interval
        self._metrics = metrics
        self._task: asyncio.Task | None = None

    async def sample_once(self) -> int | None:
        from trajectory.store.database import get_trace_engine
        size = await trace_db_bytes(get_trace_engine())
        if size is not None:
            (self._metrics or get_metrics()).set_gauge("trace_db_bytes", size)
        return size

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="trajectory-trace-db-size")
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Trace database size sample failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(self._interval)
