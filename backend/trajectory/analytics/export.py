"""The daily analytics export (WAVE3 contract 4): trace database, DuckDB Parquet, blob store.

``export_day`` stages one UTC day (``source.read_day``) as JSON lines in a
temporary directory, converts them to Parquet (``parquet.build``) and puts
``{prefix}dt=YYYY-MM-DD/{sessions,requests,tools}.parquet`` with overwrite.
All three objects are written on every run, empty tables included, so a
rerun replaces the date whole. A dry run builds the same files and uploads
nothing. Runs that upload count ``analytics_exports`` or
``analytics_export_failures`` (contract 5) in the metrics registry of the
process that runs them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from core.log import create_logger
from trajectory.analytics import parquet
from trajectory.analytics.schema import TABLES
from trajectory.analytics.source import read_day
from trajectory.storage import check_key, get_blob_store, key_prefix
from trajectory.store.database import get_trace_engine

log = create_logger("trajectory.analytics")

PREFIX_ENV = "TRAJECTORY_ANALYTICS_PREFIX"
DEFAULT_PREFIX = "analytics/trajectories/"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"
TEMP_PREFIX = "openbox-analytics-"
#: Build directories older than this were left behind by a killed run.
STALE_BUILD_SECONDS = 24 * 3600


class AnalyticsConfigError(ValueError):
    """A setting the export cannot run with (exit status 2)."""


@dataclass
class ExportSummary:
    """The JSON summary line of one run (WAVE3 contract 4)."""

    date: str
    dry_run: bool
    objects: int = 0
    rows: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TABLES, 0))
    bytes: int = 0
    duration_ms: int = 0
    #: Exception type of a failed run.
    error: str | None = None

    def as_dict(self) -> dict:
        value = {"date": self.date, "dry_run": self.dry_run, "objects": self.objects, "rows": dict(self.rows),
                 "bytes": self.bytes, "duration_ms": self.duration_ms}
        if self.error is not None:
            value["error"] = self.error
        return value

    def line(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"))


class ExportFailed(Exception):
    """A run stopped early; ``summary`` holds what it did and the error type."""

    def __init__(self, summary: ExportSummary):
        super().__init__(f"Trajectory analytics export of {summary.date} failed: {summary.error}")
        self.summary = summary


def analytics_prefix(value: str | None = None) -> str:
    """TRAJECTORY_ANALYTICS_PREFIX (or ``value``) as a key prefix ending in "/"; DEFAULT_PREFIX when unset.

    It must be a valid blob key prefix outside the trajectory namespace, where
    retention and GC take every directory for a trajectory.
    """
    raw = os.getenv(PREFIX_ENV) if value is None else value
    name = (raw or "").strip().strip("/")
    prefix = f"{name}/" if name else DEFAULT_PREFIX
    try:
        check_key(prefix, prefix=True)
        namespace = key_prefix()
    except ValueError as exc:
        raise AnalyticsConfigError(str(exc)) from exc
    if prefix.startswith(namespace):
        raise AnalyticsConfigError(f"{PREFIX_ENV}={prefix!r} lies inside the trajectory namespace {namespace!r}")
    return prefix


def object_key(prefix: str, day: date, table: str) -> str:
    return f"{prefix}dt={day.isoformat()}/{table}.parquet"


def remove_stale_build_directories(root: str | os.PathLike[str] | None = None, *,
                                   older_than: float = STALE_BUILD_SECONDS) -> int:
    """Delete build directories a killed run left in ``root`` (default: the temp directory); returns the count."""
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    cutoff = time.time() - older_than
    removed = 0
    try:
        candidates = list(base.glob(f"{TEMP_PREFIX}*"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.is_dir() and not candidate.is_symlink() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate)
                removed += 1
        except OSError:
            continue
    return removed


def _elapsed_ms(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000))


async def export_day(day: date, *, engine=None, blob_store=None, prefix: str | None = None, dry_run: bool = False,
                     metrics=None, temp_root: str | os.PathLike[str] | None = None) -> ExportSummary:
    """Export one UTC day and return its summary; ExportFailed, carrying the summary so far, when a step fails.

    ``engine`` defaults to the trace engine, ``blob_store`` to the trajectory
    blob store, ``prefix`` to analytics_prefix() and ``metrics`` to the
    registry of trajectory.worker.metrics.
    """
    engine = engine if engine is not None else get_trace_engine()
    prefix = prefix if prefix is not None else analytics_prefix()
    if blob_store is None and not dry_run:
        blob_store = get_blob_store()
    if metrics is None:
        from trajectory.worker.metrics import get_metrics
        metrics = get_metrics()
    summary = ExportSummary(date=day.isoformat(), dry_run=dry_run)
    started = time.monotonic()
    remove_stale_build_directories(temp_root)
    try:
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=temp_root, ignore_cleanup_errors=True) as name:
            directory = Path(name).resolve()
            staged: dict[str, parquet.StagedTable] = {}
            try:
                for table in TABLES:
                    staged[table] = parquet.StagedTable(directory, table)
                async with contextlib.aclosing(read_day(engine, day)) as pages:
                    async for table, rows in pages:
                        staged[table].write(rows)
            finally:
                for table in staged.values():
                    table.close()
            built = await asyncio.to_thread(parquet.build, directory, staged)
            for table, (path, rows) in built.items():
                summary.rows[table] = rows
                summary.bytes += path.stat().st_size
            for table in () if dry_run else TABLES:
                data = await asyncio.to_thread(built[table][0].read_bytes)
                await blob_store.put(object_key(prefix, day, table), data, content_type=PARQUET_CONTENT_TYPE,
                                     if_absent=False)
                summary.objects += 1
    except Exception as exc:
        summary.duration_ms = _elapsed_ms(started)
        summary.error = type(exc).__name__
        if not dry_run:
            metrics.inc("analytics_export_failures")
        # Type only: messages of database and storage errors can carry statements or signed URLs.
        log.error("Trajectory analytics export failed date=%s dry_run=%s objects=%d error_type=%s",
                  summary.date, dry_run, summary.objects, summary.error)
        raise ExportFailed(summary) from exc
    summary.duration_ms = _elapsed_ms(started)
    if not dry_run:
        metrics.inc("analytics_exports")
    log.info("Trajectory analytics export date=%s dry_run=%s objects=%d sessions=%d requests=%d tools=%d bytes=%d "
             "duration_ms=%d", summary.date, dry_run, summary.objects, summary.rows["sessions"],
             summary.rows["requests"], summary.rows["tools"], summary.bytes, summary.duration_ms)
    return summary
