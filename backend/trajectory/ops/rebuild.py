"""Trace database rebuild drill (SPEC §12: deploy/gw2/scripts/rebuild-trace-db.sh).

Proves that archived trajectory events can be restored from object storage. For
each selected live trajectory it copies the trajectory row into a scratch trace
database (created and migrated by the script), downloads every segment listed
in trajectory_segments, decodes it the way the worker stores it (zstd frames of
JSON lines, one stored event row per line, SPEC §8.10), checks the sha256 of
the decoded lines, the trajectory, event count and sequence range and the
row's raw and stored byte counts, inserts the events as hot rows, copies the
still-hot tail from
the live database, and compares a digest of the rebuilt stream read back from
the scratch database with a digest of the stream read from the sources. Event
ids and content hashes are also checked against trajectory_event_keys where
those rows still exist. The scratch copy has archived/projected/checkpoint
watermarks reset to 0, like a converted trajectory (SPEC §8.14).

The live database is only read, in one read-only snapshot on PostgreSQL. The
scratch database name must start with openbox_trace_rebuild_ so a wrong URL
cannot write into the real trace database. URLs are passed by environment
variable name to keep passwords out of the process list:

    REBUILD_SCRATCH_URL=postgresql+asyncpg://.../openbox_trace_rebuild_20260915T0330Z \\
        python -m trajectory.ops.rebuild [--only trj_x ...] [--limit N]

Exit status: 0 when every trajectory rebuilt identically, 1 on any mismatch,
2 on usage or configuration errors.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import zstandard
from sqlalchemy import MetaData, delete, func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import sqltypes

from core.aliyun import AliyunCredentialsError
from trajectory.ops.oss import OpsStorageError, blob_provider, describe_error, http_client, local_blob_root, oss_client
from trajectory.storage import decode_blob
from trajectory.types import canonical

SCRATCH_PREFIX = "openbox_trace_rebuild_"
SOURCE_TABLES = ("session_trajectories", "trajectory_segments", "trajectory_events", "trajectory_event_keys")
SCRATCH_TABLES = ("session_trajectories", "trajectory_events")
# Stored event fields (the SPEC §8.10 segment line) covered by the digest.
DIGEST_FIELDS = (
    "seq", "event_id", "type", "version", "user_id", "session_id", "source_session_id", "request_id", "call_id",
    "agent_id", "context", "data", "hints", "content_hash", "occurred_at", "recorded_at",
)
RESET_WATERMARKS = ("archived_seq", "projected_seq", "checkpoint_seq")
PAGE_ROWS = 1000
REPORT_LIMIT = 20


class RebuildError(Exception):
    pass


def _database_name(url: str) -> str:
    parsed = make_url(url)
    name = parsed.database or ""
    return Path(name).stem if parsed.get_backend_name() == "sqlite" else name


def check_urls(source_url: str, scratch_url: str, business_url: str | None = None) -> None:
    """Refuse a scratch database that could be the live trace or business database."""
    try:
        scratch_name = _database_name(scratch_url)
        others = {_database_name(url) for url in (source_url, business_url) if url}
    except Exception:
        # Never echo the URL: it carries the password.
        raise RebuildError("a database URL cannot be parsed") from None
    if not scratch_name.startswith(SCRATCH_PREFIX):
        raise RebuildError(f"the scratch database name must start with {SCRATCH_PREFIX}")
    if scratch_name in others or scratch_url in (source_url, business_url):
        raise RebuildError("the scratch database must differ from the trace and business databases")


class SegmentStore:
    """Segment objects from the worker's blob provider (TRAJECTORY_BLOB_PROVIDER local or oss)."""

    def __init__(self, environ=None, transport=None):
        self._provider = blob_provider(environ)
        self._http = None
        if self._provider == "local":
            self._root = local_blob_root(environ).resolve()
        elif self._provider == "oss":
            self._oss, self._internal = oss_client(environ)
            self._http = http_client(transport)
        else:
            raise RebuildError(f"unsupported TRAJECTORY_BLOB_PROVIDER {self._provider!r}")

    async def get(self, key: str) -> bytes:
        if self._http is None:
            path = (self._root / key).resolve()
            if not path.is_relative_to(self._root):
                raise RebuildError(f"storage key {key!r} leaves the blob root")
            return await asyncio.to_thread(path.read_bytes)
        response = await self._http.get(self._oss.presign_get(key, 600, internal=self._internal))
        if response.status_code == 404:
            raise FileNotFoundError(key)
        if response.status_code != 200:
            raise OpsStorageError(f"GET {key} failed: {describe_error(response)}")
        return response.content

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _datetime(value) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _column_value(column, value):
    if value is None:
        return None
    kind = column.type
    if isinstance(kind, sqltypes.DateTime):
        return _datetime(value)
    if isinstance(kind, sqltypes.Date):
        return value if isinstance(value, date) and not isinstance(value, datetime) else _datetime(value).date()
    if isinstance(kind, sqltypes.JSON):
        return _json(value)
    if isinstance(kind, sqltypes.Integer):
        return int(value)
    if isinstance(value, (dict, list)):
        # JSON stored as text (SQLite trace databases).
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def _row(table, source: dict, *, complete: bool, **overrides) -> dict:
    row = {}
    for column in table.columns:
        name = column.name
        if name in overrides:
            row[name] = overrides[name]
        elif name in source:
            row[name] = _column_value(column, source[name])
        elif name == "recorded_on" and source.get("recorded_at") is not None:
            row[name] = _datetime(source["recorded_at"]).date()
        elif complete:
            row[name] = None
    return row


def fingerprint(event: dict) -> bytes:
    """Canonical form of one stored event, the same for a segment line and a database row."""
    normalized = {}
    for key in DIGEST_FIELDS:
        value = event.get(key)
        if value is not None:
            if key in ("occurred_at", "recorded_at"):
                # Microseconds: the digest must notice a rebuild that loses stored precision.
                value = _datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
            elif key in ("context", "data", "hints"):
                value = _json(value)
            elif key in ("seq", "version"):
                value = int(value)
        normalized[key] = value
    return canonical(normalized) + b"\n"


def _decode(stored: bytes, compression: str | None) -> bytes:
    kind = (compression or "identity").lower()
    if kind in ("identity", "none"):
        return stored
    if kind != "zstd":
        raise RebuildError(f"unsupported compression {compression!r}")
    # The worker's decoder: every frame, and a truncated frame is an error rather than short content.
    return decode_blob(stored, "zstd")


async def segment_events(store: SegmentStore, segment: dict, trajectory_id: str) -> list[dict]:
    """Decoded, checked events of one segment row; raises on any inconsistency."""
    stored = await store.get(segment["storage_key"])
    raw = _decode(stored, segment.get("compression"))
    if segment.get("sha256") and hashlib.sha256(raw).hexdigest() != segment["sha256"]:
        raise RebuildError("sha256 of the decoded segment does not match trajectory_segments")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not all(isinstance(event, dict) for event in events):
        raise RebuildError("a segment line is not an event object")
    if any(event.get("trajectory_id", trajectory_id) != trajectory_id for event in events):
        raise RebuildError("the segment holds events of another trajectory")
    first, last = int(segment["from_seq"]), int(segment["to_seq"])
    if [int(event.get("seq", -1)) for event in events] != list(range(first, last + 1)):
        raise RebuildError(f"event sequence does not cover {first}-{last} contiguously")
    if segment.get("event_count") is not None and len(events) != int(segment["event_count"]):
        raise RebuildError(f"{len(events)} events, trajectory_segments says {segment['event_count']}")
    for column, size in (("raw_bytes", len(raw)), ("stored_bytes", len(stored))):
        if segment.get(column) is not None and int(segment[column]) != size:
            raise RebuildError(f"the object has {size} {column.replace('_', ' ')}, trajectory_segments says {segment[column]}")
    return events


async def _snapshot(connection: AsyncConnection) -> None:
    if connection.dialect.name == "postgresql":
        # Segments and hot rows must come from one state while the worker archives.
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))


async def _pages(connection: AsyncConnection, table, trajectory_id: str, after_seq: int):
    while True:
        query = (
            select(table)
            .where(table.c.trajectory_id == trajectory_id, table.c.seq > after_seq)
            .order_by(table.c.seq)
            .limit(PAGE_ROWS)
        )
        page = (await connection.execute(query)).mappings().all()
        if not page:
            return
        yield [dict(row) for row in page]
        after_seq = int(page[-1]["seq"])


async def rebuild_trajectory(
    source: AsyncEngine, source_tables, scratch: AsyncEngine, scratch_tables, trajectory: dict, store: SegmentStore
) -> dict:
    trajectory_id = trajectory["id"]
    committed = int(trajectory.get("committed_seq") or 0)
    archived = int(trajectory.get("archived_seq") or 0)
    segments_table = source_tables["trajectory_segments"]
    events_table = source_tables["trajectory_events"]
    keys_table = source_tables["trajectory_event_keys"]
    scratch_trajectories = scratch_tables["session_trajectories"]
    scratch_events = scratch_tables["trajectory_events"]
    report = {
        "trajectory_id": trajectory_id, "committed_seq": committed, "archived_seq": archived, "segments": 0,
        "segment_errors": [], "events": 0, "missing_seqs": 0, "first_missing_seqs": [], "duplicate_seqs": [],
        "unexpected_seqs": [], "stale_hot_rows": 0, "key_mismatches": [], "source_digest": None,
        "scratch_digest": None, "ok": False,
    }
    seen: dict[int, tuple[str, str]] = {}
    source_digest = hashlib.sha256()
    pending: list[dict] = []

    async with source.connect() as reader, scratch.begin() as writer:
        await _snapshot(reader)

        async def take(event: dict) -> None:
            seq = int(event["seq"])
            if seq in seen:
                report["duplicate_seqs"].append(seq)
                return
            seen[seq] = (str(event.get("event_id")), str(event.get("content_hash")))
            source_digest.update(fingerprint(event))
            pending.append(_row(scratch_events, event, complete=True, trajectory_id=trajectory_id))
            if len(pending) >= PAGE_ROWS:
                await writer.execute(insert(scratch_events), pending)
                pending.clear()

        await writer.execute(delete(scratch_events).where(scratch_events.c.trajectory_id == trajectory_id))
        await writer.execute(delete(scratch_trajectories).where(scratch_trajectories.c.id == trajectory_id))
        watermarks = {name: 0 for name in RESET_WATERMARKS if name in scratch_trajectories.c}
        await writer.execute(
            insert(scratch_trajectories).values(_row(scratch_trajectories, trajectory, complete=False, **watermarks))
        )
        segments = (
            await reader.execute(
                select(segments_table)
                .where(segments_table.c.trajectory_id == trajectory_id)
                .order_by(segments_table.c.from_seq)
            )
        ).mappings().all()
        for segment in segments:
            report["segments"] += 1
            label = f"{segment['from_seq']}-{segment['to_seq']}"
            try:
                events = await segment_events(store, dict(segment), trajectory_id)
            except FileNotFoundError:
                report["segment_errors"].append(f"{label}: object is missing")
                continue
            except (RebuildError, OpsStorageError, zstandard.ZstdError, ValueError) as exc:
                report["segment_errors"].append(f"{label}: {exc}")
                continue
            for event in events:
                await take(event)
        async for page in _pages(reader, events_table, trajectory_id, archived):
            for row in page:
                await take(row)
        if pending:
            await writer.execute(insert(scratch_events), pending)
        report["stale_hot_rows"] = (
            await reader.execute(
                select(func.count())
                .select_from(events_table)
                .where(events_table.c.trajectory_id == trajectory_id, events_table.c.seq <= archived)
            )
        ).scalar_one()
        keys = (
            await reader.execute(
                select(keys_table.c.event_id, keys_table.c.seq, keys_table.c.content_hash)
                .where(keys_table.c.trajectory_id == trajectory_id)
            )
        ).all()

    for event_id, seq, content_hash in keys:
        rebuilt = seen.get(int(seq))
        if rebuilt is not None and rebuilt != (str(event_id), str(content_hash)):
            report["key_mismatches"].append(str(event_id))
    scratch_digest = hashlib.sha256()
    async with scratch.connect() as connection:
        async for page in _pages(connection, scratch_events, trajectory_id, 0):
            for row in page:
                scratch_digest.update(fingerprint(row))
    missing = [seq for seq in range(1, committed + 1) if seq not in seen]
    report.update(
        events=len(seen),
        missing_seqs=len(missing),
        first_missing_seqs=missing[:REPORT_LIMIT],
        duplicate_seqs=report["duplicate_seqs"][:REPORT_LIMIT],
        unexpected_seqs=sorted(seq for seq in seen if seq < 1 or seq > committed)[:REPORT_LIMIT],
        key_mismatches=report["key_mismatches"][:REPORT_LIMIT],
        source_digest=source_digest.hexdigest(),
        scratch_digest=scratch_digest.hexdigest(),
    )
    report["ok"] = not (
        report["segment_errors"] or missing or report["duplicate_seqs"] or report["unexpected_seqs"]
        or report["key_mismatches"] or report["source_digest"] != report["scratch_digest"]
    )
    return report


async def _reflect(engine: AsyncEngine, names: tuple[str, ...], label: str):
    metadata = MetaData()
    try:
        async with engine.connect() as connection:
            await connection.run_sync(lambda sync: metadata.reflect(sync, only=list(names)))
    except InvalidRequestError as exc:
        raise RebuildError(f"the {label} database is missing trace tables: {exc}") from None
    return metadata.tables


async def _trajectories(engine: AsyncEngine, table, only: list[str], limit: int | None) -> list[dict]:
    query = select(table).order_by(table.c.id)
    for column in ("deleted_at", "content_expired_at"):
        if column in table.c:
            query = query.where(table.c[column].is_(None))
    if only:
        query = query.where(table.c.id.in_(only))
    if limit:
        query = query.limit(limit)
    async with engine.connect() as connection:
        return [dict(row) for row in (await connection.execute(query)).mappings().all()]


async def run(
    source_url: str, scratch_url: str, *, only: list[str] | None = None, limit: int | None = None,
    environ=None, transport=None,
) -> dict:
    only = list(only or [])
    store = SegmentStore(environ, transport)
    source = create_async_engine(source_url, poolclass=NullPool)
    scratch = create_async_engine(scratch_url, poolclass=NullPool)
    try:
        source_tables = await _reflect(source, SOURCE_TABLES, "source")
        scratch_tables = await _reflect(scratch, SCRATCH_TABLES, "scratch")
        trajectories = await _trajectories(source, source_tables["session_trajectories"], only, limit)
        reports = [
            await rebuild_trajectory(source, source_tables, scratch, scratch_tables, trajectory, store)
            for trajectory in trajectories
        ]
    finally:
        await store.close()
        await source.dispose()
        await scratch.dispose()
    not_found = sorted(set(only) - {trajectory["id"] for trajectory in trajectories})
    return {
        "ok": not not_found and all(report["ok"] for report in reports),
        "trajectories": len(reports),
        "segments": sum(report["segments"] for report in reports),
        "events": sum(report["events"] for report in reports),
        "not_found": not_found,
        "reports": reports,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m trajectory.ops.rebuild", description=__doc__.splitlines()[0])
    parser.add_argument("--source-url-env", default="TRAJECTORY_DATABASE_URL",
                        help="variable holding the live trace database URL")
    parser.add_argument("--scratch-url-env", default="REBUILD_SCRATCH_URL",
                        help="variable holding the scratch database URL")
    parser.add_argument("--only", nargs="+", default=[], metavar="TRAJECTORY_ID")
    parser.add_argument("--limit", type=int, help="rebuild at most this many live trajectories, ordered by id")
    return parser


def main(argv: list[str] | None = None, *, environ=None, stdout=None, transport=None) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    source_url, scratch_url = env.get(args.source_url_env), env.get(args.scratch_url_env)
    if not source_url or not scratch_url:
        print(f"rebuild: set {args.source_url_env} and {args.scratch_url_env}", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("rebuild: --limit must be at least 1", file=sys.stderr)
        return 2
    try:
        check_urls(source_url, scratch_url, env.get("DATABASE_URL"))
        report = asyncio.run(
            run(source_url, scratch_url, only=args.only, limit=args.limit, environ=env, transport=transport)
        )
    except (RebuildError, OpsStorageError, AliyunCredentialsError) as exc:
        print(f"rebuild: {exc}", file=sys.stderr)
        return 2
    except (SQLAlchemyError, OSError) as exc:
        # Type only: statement parameters in the message may carry event content.
        print(f"rebuild: database or storage failure: {type(exc).__name__}", file=sys.stderr)
        return 2
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["trajectories"]:
        print("rebuild: no live trajectory matched, nothing was verified", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
