"""Worker counters and gauges, served as JSON by ``GET /metrics`` (SPEC §8.13).

The names are the contract with ``trajectory.ops.cms`` and the gw2 drills, so a
snapshot always lists every one of them. The registry is process-wide and
thread-safe (blob uploads and file scans may report from worker threads), and
recording a value never raises.
"""
import math
import threading
import time

from core.log import create_logger

log = create_logger("trajectory.worker.metrics")

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
