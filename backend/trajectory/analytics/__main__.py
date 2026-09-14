"""``python -m trajectory.analytics export --date YYYY-MM-DD [--dry-run]`` (WAVE3 contract 4).

The trace database is the worker's TRAJECTORY_DATABASE_URL (the embedded
default outside external mode). Objects go through the trajectory blob store
settings (TRAJECTORY_BLOB_PROVIDER and friends) under
TRAJECTORY_ANALYTICS_PREFIX, default ``analytics/trajectories/``. One JSON
summary line goes to stdout, also after a failed export; messages go to
stderr.

Exit status: 0 success, 1 the export failed, 2 usage or configuration errors.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from trajectory.analytics.export import AnalyticsConfigError, ExportFailed, analytics_prefix, export_day
from trajectory.storage import get_blob_store
from trajectory.store.database import close_trace_engine, init_trace_engine

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _day(value: str) -> date:
    try:
        if not _DATE.fullmatch(value):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date as YYYY-MM-DD, got {value!r}") from None


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="python -m trajectory.analytics",
                                      description="Parquet exports of trajectory statistics.")
    commands = command.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export one UTC day of sessions, requests and tool calls")
    export.add_argument("--date", required=True, type=_day, metavar="YYYY-MM-DD", help="the UTC day to export")
    export.add_argument("--dry-run", action="store_true", help="read and build the files without uploading them")
    return command


def _same_database(first: URL, second: URL) -> bool:
    if first.get_backend_name() != second.get_backend_name():
        return False
    if first.get_backend_name() == "sqlite":
        return Path(first.database or "").expanduser().resolve() == Path(second.database or "").expanduser().resolve()
    return ((first.host or "localhost", first.port or 5432, first.database)
            == (second.host or "localhost", second.port or 5432, second.database))


def trace_database_url() -> str:
    """The worker's trace database URL; refused when missing, the business database or an absent SQLite file."""
    from trajectory.worker.settings import WorkerSettings

    url = WorkerSettings.from_env().database_url
    if not url:
        raise AnalyticsConfigError("TRAJECTORY_DATABASE_URL is required")
    try:
        parsed = make_url(url)
    except ArgumentError:
        raise AnalyticsConfigError("TRAJECTORY_DATABASE_URL is not a database URL") from None
    try:
        business = make_url(os.getenv("DATABASE_URL") or "")
    except ArgumentError:
        business = None
    if business is not None and _same_database(parsed, business):
        raise AnalyticsConfigError("TRAJECTORY_DATABASE_URL must not name the business database")
    if parsed.get_backend_name() == "sqlite" and not Path(parsed.database or "").expanduser().is_file():
        # Connecting would create an empty database file instead.
        raise AnalyticsConfigError(f"trace database file not found: {parsed.database}")
    return url


async def run(argv: list[str] | None = None, *, stdout=None, stderr=None, engine=None, blob_store=None, metrics=None,
              today: date | None = None) -> int:
    """The command without exiting. ``engine``, ``blob_store`` and ``metrics`` replace the configured ones."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits on --help (0) and on usage errors (2).
        return exc.code if isinstance(exc.code, int) else 2
    own_engine = engine is None
    try:
        current = today or datetime.now(timezone.utc).date()
        if args.date > current:
            raise AnalyticsConfigError(f"--date {args.date.isoformat()} is after the current UTC day {current.isoformat()}")
        prefix = analytics_prefix()
        if blob_store is None:
            try:
                blob_store = get_blob_store()
            except RuntimeError as exc:
                # An unsupported TRAJECTORY_BLOB_PROVIDER or OSS without a bucket.
                raise AnalyticsConfigError(str(exc)) from exc
        if own_engine:
            url = trace_database_url()
            try:
                engine = init_trace_engine(url, pool_size=1, max_overflow=0)
            except Exception as exc:
                # Type only: the message can carry the URL and its password.
                raise AnalyticsConfigError(f"cannot open the trace database: {type(exc).__name__}") from exc
    except AnalyticsConfigError as exc:
        print(f"analytics: {exc}", file=stderr)
        return 2
    try:
        summary = await export_day(args.date, engine=engine, blob_store=blob_store, prefix=prefix,
                                   dry_run=args.dry_run, metrics=metrics)
    except ExportFailed as failed:
        print(failed.summary.line(), file=stdout, flush=True)
        print(f"analytics: export of {failed.summary.date} failed: {failed.summary.error}", file=stderr)
        return 1
    finally:
        if own_engine:
            await close_trace_engine()
    print(summary.line(), file=stdout, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
