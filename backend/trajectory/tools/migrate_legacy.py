"""Legacy trajectory converter (SPEC §6.9, §8.14).

Copies the retired trajectory tables of the business database
(``legacy_trajectory_*``, or the original names with ``--source-tables
original``) into the trace database and its blob store. Database URLs come
from the environment so passwords stay out of the process list
(``--business-database-url`` is accepted too):

    TRAJECTORY_DATABASE_URL=... TRAJECTORY_LEGACY_DATABASE_URL=... \\
        python -m trajectory.tools.migrate_legacy --legacy-blob-path /legacy-blobs [--only trj_x ...] [--dry-run]
    python -m trajectory.tools.migrate_legacy --verify         # events, content hashes, payload digests
    python -m trajectory.tools.migrate_legacy --finalize-drop  # after a successful full --verify

Each trajectory converts in one trace transaction: its row with the original
id, owner and status; events with their original seq, event id, context,
times and content hash; content prepared as ingest prepares it (SPEC §8.4,
without the redaction pass the legacy recorder already applied); and legacy
payload rows mapped to rows with the same payload_id and first_seq. Records,
checkpoints and segments are rebuilt by the worker; exports are not migrated.
A marker per trajectory in ``trajectory_worker_state`` makes reruns skip
finished work. The business database is only read, except by
``--finalize-drop``.

Exit status: 0 success, 1 conversion errors, verification mismatches or a
refused drop, 2 usage or configuration errors.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from trajectory.storage import blob_key, decode_blob, encode_blob, get_blob_store
from trajectory.store.database import close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryEventKey, TrajectoryPayload,
    TrajectorySegment, TrajectoryWorkerState)
from trajectory.types import ID_FIELDS
from trajectory.worker import content

LEGACY_ENV = "TRAJECTORY_LEGACY_DATABASE_URL"
TRACE_ENV = "TRAJECTORY_DATABASE_URL"
CONVERTED_KEY = "legacy_convert:{}"
RUN_KEY = "legacy_convert"
VERIFY_KEY = "legacy_verify"
PAGE_EVENTS = 500
#: Table role -> (accepted names after the business rename, first match wins; the original name).
TABLES = {
    "trajectories": (("legacy_trajectory_session_trajectories", "legacy_session_trajectories",
                      "legacy_trajectory_sessions"), "session_trajectories"),
    "events": (("legacy_trajectory_events",), "trajectory_events"),
    "payloads": (("legacy_trajectory_payloads",), "trajectory_payloads"),
    "records": (("legacy_trajectory_records",), "trajectory_records"),
    "summaries": (("legacy_trajectory_session_summaries",), "trajectory_session_summaries"),
    "checkpoints": (("legacy_trajectory_checkpoints",), "trajectory_checkpoints"),
    "exports": (("legacy_trajectory_exports",), "trajectory_exports"),
}
REQUIRED_ROLES = ("trajectories", "events", "payloads")
DROP_ORDER = ("exports", "checkpoints", "summaries", "records", "payloads", "events", "trajectories")


class UsageError(Exception):
    """Configuration problems: exit status 2."""


class ConversionError(Exception):
    """A trajectory that cannot be converted faithfully (for example missing payload bytes)."""


def _utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _same_database(first: str, second: str) -> bool:
    try:
        a, b = make_url(first), make_url(second)
    except Exception:
        return first == second
    if a.get_backend_name() != b.get_backend_name():
        return False
    if a.get_backend_name() == "sqlite":
        return Path(a.database or "").expanduser().resolve() == Path(b.database or "").expanduser().resolve()
    return (a.host or "localhost", a.port or 5432, a.database) == (b.host or "localhost", b.port or 5432, b.database)


async def _put_state(db, key: str, value: dict) -> None:
    now = datetime.now(timezone.utc)
    row = await db.get(TrajectoryWorkerState, key)
    if row is None:
        db.add(TrajectoryWorkerState(key=key, value=value, updated_at=now))
    else:
        row.value, row.updated_at = value, now


@dataclass
class Summary:
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    events: int = 0
    payloads: int = 0
    mismatches: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (f"converted={self.converted} skipped={self.skipped} failed={self.failed} events={self.events} "
                f"payloads={self.payloads} mismatches={len(self.mismatches)}")


class LegacySource:
    """Access to the legacy tables of the business database; reads use read-only connections."""

    def __init__(self, url: str, *, source_tables: str):
        self.source_tables = source_tables
        self.engine = create_async_engine(url, poolclass=NullPool)
        self.tables: dict[str, sa.Table] = {}
        self.assets: sa.Table | None = None

    async def open(self) -> None:
        async with self.engine.connect() as connection:
            names = set(await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names()))
            resolved = {}
            for role, (legacy_names, original) in TABLES.items():
                candidates = (original,) if self.source_tables == "original" else legacy_names
                found = next((name for name in candidates if name in names), None)
                if found is not None:
                    resolved[role] = found
            missing = [role for role in REQUIRED_ROLES if role not in resolved]
            if missing:
                raise UsageError(f"legacy trajectory tables not found for: {', '.join(missing)}")
            metadata = sa.MetaData()
            only = list(resolved.values()) + (["file_assets"] if "file_assets" in names else [])
            await connection.run_sync(lambda sync: metadata.reflect(sync, only=only))
        self.tables = {role: metadata.tables[name] for role, name in resolved.items()}
        self.assets = metadata.tables.get("file_assets")

    async def close(self) -> None:
        await self.engine.dispose()

    async def read(self) -> AsyncConnection:
        """A read-only connection (one consistent snapshot on PostgreSQL)."""
        connection = await self.engine.connect()
        if connection.dialect.name == "postgresql":
            await connection.execution_options(isolation_level="REPEATABLE READ")
            await connection.execute(text("SET TRANSACTION READ ONLY"))
        else:
            await connection.exec_driver_sql("PRAGMA query_only = ON")
        return connection

    async def trajectory_ids(self, connection, only: list[str] | None) -> list[str]:
        table = self.tables["trajectories"]
        statement = select(table.c.id).order_by(table.c.id)
        if only:
            statement = statement.where(table.c.id.in_(only))
        return list((await connection.execute(statement)).scalars())

    async def trajectory(self, connection, trajectory_id: str):
        table = self.tables["trajectories"]
        return (await connection.execute(select(table).where(table.c.id == trajectory_id))).first()

    async def events(self, connection, trajectory_id: str, *, page: int = PAGE_EVENTS):
        table = self.tables["events"]
        last = 0
        while True:
            rows = (await connection.execute(select(table).where(
                table.c.trajectory_id == trajectory_id, table.c.seq > last).order_by(table.c.seq).limit(page))).all()
            if not rows:
                return
            yield rows
            last = rows[-1].seq

    async def event_stats(self, connection, trajectory_id: str) -> tuple[int, int]:
        table = self.tables["events"]
        row = (await connection.execute(select(func.count(), func.coalesce(func.max(table.c.seq), 0)).where(
            table.c.trajectory_id == trajectory_id))).one()
        return int(row[0]), int(row[1])

    async def payloads(self, connection, trajectory_id: str) -> list:
        table = self.tables["payloads"]
        columns = [column for column in table.c if column.name != "content"]
        return list((await connection.execute(select(*columns).where(table.c.trajectory_id == trajectory_id)
                                              .order_by(table.c.first_seq, table.c.payload_id))).all())

    async def payload_content(self, connection, payload_id: str) -> bytes | None:
        table = self.tables["payloads"]
        value = (await connection.execute(select(table.c.content).where(table.c.payload_id == payload_id))).scalar()
        return bytes(value) if value is not None else None

    async def checkpoint_pages(self, connection, trajectory_id: str) -> set[str]:
        table = self.tables.get("checkpoints")
        if table is None:
            return set()
        pages = set()
        for (state,) in (await connection.execute(select(table.c.state).where(
                table.c.trajectory_id == trajectory_id))).all():
            for page in (_json(state) or {}).get("record_pages") or []:
                reference = page.get("$payload") if isinstance(page, dict) else None
                if isinstance(reference, dict) and isinstance(reference.get("payload_id"), str):
                    pages.add(reference["payload_id"])
        return pages

    async def live_assets(self, connection, asset_ids) -> dict[str, str]:
        """``{asset id: oss_key}`` of business assets that still exist."""
        if self.assets is None or not asset_ids:
            return {}
        table = self.assets
        conditions = [table.c.id.in_(sorted(asset_ids))]
        if "is_deleted" in table.c:
            conditions.append(sa.or_(table.c.is_deleted.is_(None), table.c.is_deleted == sa.false()))
        if "deleted_at" in table.c:
            conditions.append(table.c.deleted_at.is_(None))
        if "status" in table.c:
            conditions.append(table.c.status != "deleted")
        return {row.id: row.oss_key for row in (await connection.execute(
            select(table.c.id, table.c.oss_key).where(*conditions))).all()}

    async def fingerprint(self) -> dict:
        connection = await self.read()
        try:
            return {role: int((await connection.execute(select(func.count()).select_from(self.tables[role]))).scalar())
                    for role in REQUIRED_ROLES}
        finally:
            await connection.close()


def _references(value, found: set[str]) -> set[str]:
    """Payload ids a stored value points at: reference objects and ``trajectory-media:`` placeholders."""
    if isinstance(value, dict):
        if isinstance(value.get("payload_id"), str) and "sha256" in value:
            found.add(value["payload_id"])
        for child in value.values():
            _references(child, found)
    elif isinstance(value, list):
        for child in value:
            _references(child, found)
    elif isinstance(value, str) and value.startswith("trajectory-media:"):
        found.add(value[len("trajectory-media:"):])
    return found


class Converter:
    def __init__(self, source: LegacySource, *, blob_store, legacy_blob_path: Path | None, dry_run: bool,
                 inline_bytes: int, out=print):
        self.source = source
        self.blob_store = blob_store
        self.legacy_blob_path = legacy_blob_path
        self.dry_run = dry_run
        self.inline_bytes = inline_bytes
        self.out = out

    async def convert(self, only: list[str] | None) -> Summary:
        summary = Summary()
        connection = await self.source.read()
        try:
            for trajectory_id in await self.source.trajectory_ids(connection, only):
                try:
                    outcome = await self._convert_one(connection, trajectory_id, summary)
                except (ConversionError, sa.exc.SQLAlchemyError, OSError, ValueError) as exc:
                    summary.failed += 1
                    self.out(f"failed trajectory={trajectory_id} error={type(exc).__name__}: {exc}")
                    continue
                self.out(f"{outcome} trajectory={trajectory_id}")
        finally:
            await connection.close()
        if not self.dry_run:
            async with trace_session() as db:
                await _put_state(db, RUN_KEY, {"finished_at": _now_iso(), "converted": summary.converted,
                                               "failed": summary.failed})
        return summary

    async def _convert_one(self, connection, trajectory_id: str, summary: Summary) -> str:
        legacy = await self.source.trajectory(connection, trajectory_id)
        count, max_seq = await self.source.event_stats(connection, trajectory_id)
        async with trace_session() as db:
            marker = await db.get(TrajectoryWorkerState, CONVERTED_KEY.format(trajectory_id))
            if marker is not None and marker.value.get("events") == count and \
                    marker.value.get("committed_seq") == int(legacy.committed_seq):
                summary.skipped += 1
                return "skipped"
            taken = await db.scalar(select(SessionTrajectory.id).where(sa.or_(
                SessionTrajectory.id == trajectory_id, SessionTrajectory.session_id == legacy.session_id)))
            if taken is not None:
                raise ConversionError("the trace database already holds this trajectory id or session")
        if self.dry_run:
            summary.converted += 1
            summary.events += count
            return f"would-convert events={count}"
        async with trace_session() as db:
            copied = await _TrajectoryCopy(self, connection, db, legacy).run()
            await _put_state(db, CONVERTED_KEY.format(trajectory_id), {
                "events": count, "committed_seq": int(legacy.committed_seq), "max_seq": max_seq,
                "payloads": copied["payloads"], "converted_at": _now_iso()})
        summary.converted += 1
        summary.events += copied["events"]
        summary.payloads += copied["payloads"]
        return f"converted events={copied['events']} payloads={copied['payloads']}"

    async def legacy_bytes(self, connection, row) -> bytes | None:
        """Bytes of a legacy payload from its ``content`` column or the legacy blob directory, digest-checked."""
        data = await self.source.payload_content(connection, row.payload_id)
        if data is None and self.legacy_blob_path is not None and row.storage_key:
            root = self.legacy_blob_path.resolve()
            path = (root / row.storage_key).resolve()
            if root in path.parents and path.is_file():
                data = await asyncio.to_thread(path.read_bytes)
        if data is not None and hashlib.sha256(data).hexdigest() != row.sha256:
            raise ConversionError(f"legacy payload {row.payload_id} does not match its sha256")
        return data


class _TrajectoryCopy:
    """One trajectory's rows, written inside one trace transaction."""

    def __init__(self, converter: Converter, connection, db, legacy):
        self.converter = converter
        self.source = converter.source
        self.connection = connection
        self.db = db
        self.legacy = legacy
        self.trajectory_id = legacy.id
        self.planner = content.ContentPlanner(inline_bytes=converter.inline_bytes, blob_key=blob_key)
        self.rows: dict[str, content.ExistingPayload] = {}
        self.inserted: set[str] = set()
        self.uploaded: set[str] = set()
        self.stored_objects: set[str] = set()
        self.stored_bytes = 0
        self.payload_count = 0
        self.pending: list[dict] = []

    async def run(self) -> dict:
        legacy, db = self.legacy, self.db
        started_at = _utc(legacy.started_at)
        await db.execute(insert(SessionTrajectory).values(
            id=legacy.id, user_id=legacy.user_id, session_id=legacy.session_id, workspace_id=legacy.workspace_id,
            started_at=started_at, updated_at=_utc(legacy.updated_at) or started_at, last_activity_at=started_at,
            next_seq=int(legacy.next_seq), committed_seq=int(legacy.committed_seq), projected_seq=0, archived_seq=0,
            checkpoint_seq=0, recording_status=legacy.recording_status, recording_epoch=0, event_count=0,
            stored_bytes=0, deleted_at=_utc(legacy.deleted_at)))
        legacy_payloads = await self.source.payloads(self.connection, self.trajectory_id)
        by_id = {row.payload_id: row for row in legacy_payloads}
        pages = await self.source.checkpoint_pages(self.connection, self.trajectory_id)
        whole: set[str] = set()
        referenced: set[str] = set()
        last_activity = started_at
        events = 0
        async for page in self.source.events(self.connection, self.trajectory_id):
            loaded = []
            for row in page:
                data = await self._event_data(row, by_id, whole)
                referenced |= _references(data, set())
                loaded.append((row, data))
            await self._map_payloads(by_id, referenced - whole - pages - self.inserted)
            lookup = content.TrajectoryContent.from_rows(self.rows.values())
            plans = []
            for index, (row, data) in enumerate(loaded):
                plans.append((row, self.planner.plan(
                    index=index, trajectory_id=self.trajectory_id, event_type=row.type, data=data,
                    media=content.extract_media(data), helpers={}, lookup=lookup, assets={},
                    owner_user_id=legacy.user_id, workspace_id=legacy.workspace_id)))
            await self._upload()
            event_rows, key_rows = [], []
            for row, plan in plans:
                for ref in plan.refs:
                    self._new_row(ref, int(row.seq))
                recorded_at, occurred_at = _utc(row.recorded_at), _utc(row.occurred_at)
                context = _json(row.context) or {}
                event_rows.append({
                    "trajectory_id": self.trajectory_id, "seq": int(row.seq), "recorded_on": recorded_at.date(),
                    "event_id": row.event_id, "type": row.type, "version": int(row.version), "user_id": row.user_id,
                    "session_id": row.session_id, "source_session_id": row.source_session_id,
                    "request_id": row.request_id, "call_id": row.call_id, "agent_id": row.agent_id,
                    "context": {key: context[key] for key in ID_FIELDS if context.get(key) is not None},
                    "data": plan.data, "hints": content.final_hints(plan.previews, plan.data),
                    "content_hash": row.content_hash, "occurred_at": occurred_at, "recorded_at": recorded_at})
                key_rows.append({"event_id": row.event_id, "trajectory_id": self.trajectory_id, "seq": int(row.seq),
                                 "content_hash": row.content_hash, "recorded_at": recorded_at})
                last_activity = max(last_activity, occurred_at)
            await self._flush_payloads()
            await db.execute(insert(TrajectoryEvent), event_rows)
            known = set((await db.scalars(select(TrajectoryEventKey.event_id).where(
                TrajectoryEventKey.event_id.in_([item["event_id"] for item in key_rows])))).all())
            fresh = [item for item in key_rows if item["event_id"] not in known]
            if fresh:
                await db.execute(insert(TrajectoryEventKey), fresh)
            events += len(event_rows)
        # Deleted content stays represented so that it can never be stored again.
        await self._map_payloads(by_id, {row.payload_id for row in legacy_payloads if row.availability != "available"}
                                 - pages - self.inserted)
        await self._flush_payloads()
        await db.execute(sa.update(SessionTrajectory).where(SessionTrajectory.id == self.trajectory_id).values(
            event_count=events, stored_bytes=self.stored_bytes, last_activity_at=last_activity))
        return {"events": events, "payloads": self.payload_count}

    async def _event_data(self, row, by_id: dict, whole: set[str]) -> dict:
        """Event data with a whole-data ``$payload`` loaded back, so content preparation sees the values."""
        data = _json(row.data)
        envelope = data.get("$payload") if isinstance(data, dict) and len(data) == 1 else None
        if isinstance(envelope, dict) and envelope.get("payload_id") in by_id:
            stored = by_id[envelope["payload_id"]]
            if stored.availability == "available":
                raw = await self.converter.legacy_bytes(self.connection, stored)
                if raw is None:
                    raise ConversionError(f"bytes of payload {stored.payload_id} are missing")
                data = json.loads(raw)
                whole.add(stored.payload_id)
        if not isinstance(data, dict):
            raise ConversionError(f"event {row.event_id} has no object data")
        return data

    async def _map_payloads(self, by_id: dict, payload_ids: set[str]) -> None:
        """Legacy rows keep their payload_id and first_seq; copies of live assets become asset references."""
        # Oldest first: the original row of duplicated content keeps the canonical dedupe key.
        rows = sorted((by_id[identifier] for identifier in payload_ids if identifier in by_id),
                      key=lambda row: (int(row.first_seq), row.payload_id))
        if not rows:
            return
        assets = await self.source.live_assets(self.connection,
                                               {row.source_asset_id for row in rows if row.source_asset_id})
        taken = {row.dedupe_key for row in self.rows.values()}
        for row in rows:
            media_type = content.normalize_media_type(row.media_type)
            if row.source_asset_id in assets:
                kind, key, encoding, stored = "asset", assets[row.source_asset_id], "identity", 0
            else:
                kind, key, encoding, stored = "blob", blob_key(self.trajectory_id, row.sha256), "identity", 0
                if row.availability == "available":
                    raw = await self.converter.legacy_bytes(self.connection, row)
                    if raw is None:
                        raise ConversionError(f"bytes of payload {row.payload_id} are missing")
                    data, encoding = encode_blob(raw, media_type)
                    stored = len(data)
                    if key not in self.uploaded:
                        await self.converter.blob_store.put(key, data, content_type=media_type, if_absent=True)
                        self.uploaded.add(key)
                    if key not in self.stored_objects:
                        self.stored_objects.add(key)
                        self.stored_bytes += stored
            dedupe = content.dedupe_key(row.sha256, media_type, row.source_asset_id, kind)
            if dedupe in taken:
                # The legacy table had no unique dedupe key; duplicates keep their own ids.
                dedupe = hashlib.sha256(f"{dedupe}:legacy:{row.payload_id}".encode()).hexdigest()
            taken.add(dedupe)
            self.rows[row.payload_id] = content.ExistingPayload(row.payload_id, dedupe, row.sha256, row.availability,
                                                                kind, encoding, stored, row.source_asset_id)
            self.inserted.add(row.payload_id)
            self.pending.append({
                "payload_id": row.payload_id, "trajectory_id": self.trajectory_id, "dedupe_key": dedupe,
                "sha256": row.sha256, "size_bytes": int(row.size_bytes), "stored_bytes": stored,
                "media_type": media_type, "encoding": encoding, "storage_kind": kind, "storage_key": key,
                "source_asset_id": row.source_asset_id, "availability": row.availability,
                "first_seq": int(row.first_seq), "created_at": _utc(row.created_at),
                "deleted_at": _utc(row.deleted_at)})

    def _new_row(self, ref: content.PendingRef, seq: int) -> None:
        if ref.failed or ref.blocked or ref.existing or ref.payload_id in self.inserted:
            return
        blob = ref.storage_kind == "blob"
        self.inserted.add(ref.payload_id)
        self.rows[ref.payload_id] = content.ExistingPayload(
            ref.payload_id, ref.dedupe_key, ref.sha256, ref.availability, ref.storage_kind,
            ref.encoding if blob else "identity", ref.stored_bytes if blob else 0, ref.source_asset_id)
        if blob and ref.storage_key not in self.stored_objects:
            self.stored_objects.add(ref.storage_key)
            self.stored_bytes += ref.stored_bytes
        self.pending.append({
            "payload_id": ref.payload_id, "trajectory_id": self.trajectory_id, "dedupe_key": ref.dedupe_key,
            "sha256": ref.sha256, "size_bytes": ref.size_bytes, "stored_bytes": ref.stored_bytes if blob else 0,
            "media_type": ref.media_type, "encoding": ref.encoding if blob else "identity",
            "storage_kind": ref.storage_kind, "storage_key": ref.storage_key, "source_asset_id": ref.source_asset_id,
            "availability": ref.availability, "first_seq": seq, "created_at": datetime.now(timezone.utc),
            "deleted_at": None})

    async def _upload(self) -> None:
        for key, upload in list(self.planner.uploads.items()):
            if key not in self.uploaded:
                await self.converter.blob_store.put(key, upload.data, content_type=upload.content_type, if_absent=True)
                self.uploaded.add(key)
            upload.data = b""
        self.planner.uploads.clear()

    async def _flush_payloads(self) -> None:
        if self.pending:
            await self.db.execute(insert(TrajectoryPayload), self.pending)
            self.payload_count += len(self.pending)
            self.pending = []


# -- Verification and finalization ---------------------------------------------------------

async def verify(source: LegacySource, *, blob_store, only: list[str] | None, out=print) -> Summary:
    """Compare converted trajectories with the legacy tables; a clean full run records the verify marker."""
    summary = Summary()
    connection = await source.read()
    try:
        identifiers = await source.trajectory_ids(connection, only)
        for trajectory_id in identifiers:
            problems = await _verify_one(source, connection, blob_store, trajectory_id)
            if problems:
                summary.failed += 1
                for problem in problems:
                    summary.mismatches.append(f"{trajectory_id}: {problem}")
                    out(f"mismatch trajectory={trajectory_id} {problem}")
            else:
                summary.converted += 1
    finally:
        await connection.close()
    if only:
        return summary
    async with trace_session() as db:
        if summary.mismatches:
            # A failed full verification withdraws any earlier permission to drop the legacy tables.
            await db.execute(delete(TrajectoryWorkerState).where(TrajectoryWorkerState.key == VERIFY_KEY))
        else:
            await _put_state(db, VERIFY_KEY, {"verified_at": _now_iso(), "trajectories": len(identifiers),
                                              "fingerprint": await source.fingerprint(),
                                              "tables": sorted(table.name for table in source.tables.values())})
    return summary


async def _trace_events(db, blob_store, trajectory, through_seq: int) -> dict[int, tuple[str, str]]:
    found = {}
    if trajectory.archived_seq:
        segments = (await db.scalars(select(TrajectorySegment).where(
            TrajectorySegment.trajectory_id == trajectory.id).order_by(TrajectorySegment.from_seq))).all()
        if segments:
            from trajectory.segments import load_segment
            for segment in segments:
                for row in await load_segment(blob_store, segment):
                    found[int(row["seq"])] = (row["event_id"], row["content_hash"])
    for seq, event_id, content_hash in (await db.execute(select(
            TrajectoryEvent.seq, TrajectoryEvent.event_id, TrajectoryEvent.content_hash).where(
            TrajectoryEvent.trajectory_id == trajectory.id, TrajectoryEvent.seq <= through_seq))).all():
        found[int(seq)] = (event_id, content_hash)
    return {seq: value for seq, value in found.items() if seq <= through_seq}


async def _verify_one(source, connection, blob_store, trajectory_id: str) -> list[str]:
    problems = []
    legacy = await source.trajectory(connection, trajectory_id)
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id)
        if trajectory is None:
            return ["trajectory missing in the trace database"]
        if (trajectory.user_id, trajectory.session_id) != (legacy.user_id, legacy.session_id):
            problems.append("owner or session differs")
        count, max_seq = await source.event_stats(connection, trajectory_id)
        try:
            trace = await _trace_events(db, blob_store, trajectory, max_seq)
        except Exception as exc:
            return problems + [f"trace events unreadable: {type(exc).__name__}"]
        if len(trace) != count:
            problems.append(f"event count legacy={count} trace={len(trace)}")
        if sorted(trace) != list(range(1, len(trace) + 1)):
            problems.append("trace seq is not contiguous from 1")
        async for page in source.events(connection, trajectory_id):
            for row in page:
                if trace.get(int(row.seq)) != (row.event_id, row.content_hash):
                    problems.append(f"seq {row.seq} event_id or content_hash differs")
        mapped_rows = {row.payload_id: row for row in (await db.scalars(select(TrajectoryPayload).where(
            TrajectoryPayload.trajectory_id == trajectory_id))).all()}
        for row in await source.payloads(connection, trajectory_id):
            mapped = mapped_rows.get(row.payload_id)
            if mapped is None:
                continue  # whole-data payloads are prepared again and checkpoint pages are rebuilt
            if mapped.sha256 != row.sha256 or mapped.first_seq != int(row.first_seq):
                problems.append(f"payload {row.payload_id} sha256 or first_seq differs")
            elif mapped.storage_kind == "blob" and mapped.availability == "available":
                try:
                    raw = decode_blob(await blob_store.get(mapped.storage_key), mapped.encoding)
                except Exception as exc:
                    problems.append(f"payload {row.payload_id} unreadable: {type(exc).__name__}")
                    continue
                if hashlib.sha256(raw).hexdigest() != row.sha256:
                    problems.append(f"payload {row.payload_id} bytes differ")
    return problems


async def finalize_drop(source: LegacySource, *, out=print) -> int:
    if source.source_tables == "original":
        raise UsageError("--finalize-drop only drops renamed legacy_* tables")
    async with trace_session() as db:
        marker = await db.get(TrajectoryWorkerState, VERIFY_KEY)
        last_run = await db.get(TrajectoryWorkerState, RUN_KEY)
        marker_value = dict(marker.value) if marker is not None else None
        run_value = dict(last_run.value) if last_run is not None else {}
    if marker_value is None:
        out("refused: no successful full --verify is recorded")
        return 1
    if str(run_value.get("finished_at", "")) > str(marker_value.get("verified_at", "")):
        out("refused: a conversion ran after the last successful --verify")
        return 1
    if await source.fingerprint() != marker_value.get("fingerprint"):
        out("refused: the legacy tables changed since the last successful --verify")
        return 1
    async with source.engine.begin() as connection:
        for role in DROP_ORDER:
            table = source.tables.get(role)
            if table is not None and table.name.startswith("legacy_"):
                await connection.execute(text(f'DROP TABLE IF EXISTS "{table.name}"'))
                out(f"dropped {table.name}")
    return 0


# -- Command line ------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="python -m trajectory.tools.migrate_legacy",
                                      description="Convert legacy trajectories into the trace database.")
    command.add_argument("--business-database-url", help=f"business database URL (prefer {LEGACY_ENV})")
    command.add_argument("--legacy-blob-path", type=Path, help="directory of the legacy local blob storage")
    command.add_argument("--dry-run", action="store_true", help="report what would be converted without writing")
    command.add_argument("--only", nargs="+", metavar="TRAJECTORY_ID", help="limit to these trajectory ids")
    command.add_argument("--verify", action="store_true", help="compare converted trajectories with the legacy tables")
    command.add_argument("--finalize-drop", action="store_true", help="drop the legacy tables after a full --verify")
    command.add_argument("--source-tables", choices=("legacy", "original"), default="legacy",
                         help="read legacy_trajectory_* tables (default) or the original table names")
    return command


async def run(argv: list[str] | None = None, *, blob_store=None, out=print) -> int:
    args = parser().parse_args(argv)
    business_url = args.business_database_url or os.getenv(LEGACY_ENV)
    trace_url = os.getenv(TRACE_ENV)
    try:
        if not business_url:
            raise UsageError(f"--business-database-url or {LEGACY_ENV} is required")
        if not trace_url:
            raise UsageError(f"{TRACE_ENV} is required")
        if _same_database(business_url, trace_url):
            raise UsageError("the trace database must not be the business database")
        if args.verify and args.finalize_drop:
            raise UsageError("run --verify and --finalize-drop separately")
        if args.finalize_drop and args.source_tables == "original":
            raise UsageError("--finalize-drop only drops renamed legacy_* tables")
        source = LegacySource(business_url, source_tables=args.source_tables)
        init_trace_engine(trace_url)
        try:
            await source.open()
            store = blob_store if blob_store is not None else get_blob_store()
            if args.finalize_drop:
                return await finalize_drop(source, out=out)
            if args.verify:
                summary = await verify(source, blob_store=store, only=args.only, out=out)
                out(f"verify {summary.line()}")
                return 1 if summary.mismatches else 0
            from trajectory.worker.settings import get_worker_settings
            converter = Converter(source, blob_store=store, legacy_blob_path=args.legacy_blob_path,
                                  dry_run=args.dry_run, inline_bytes=get_worker_settings().inline_bytes, out=out)
            summary = await converter.convert(args.only)
            out(("dry-run " if args.dry_run else "convert ") + summary.line())
            return 1 if summary.failed else 0
        finally:
            await source.close()
            await close_trace_engine()
    except UsageError as exc:
        out(f"error: {exc}")
        return 2


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
