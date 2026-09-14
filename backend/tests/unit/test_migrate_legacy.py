"""Legacy trajectory converter (SPEC §8.14) against a business schema built by the legacy trajectory migration.

The business database gets migration ``f6a8c0e2b4d6`` (the original trajectory tables), then the tables are
renamed to ``legacy_trajectory_*`` as the wave-2 business migration does.
"""
import hashlib
import importlib
import json
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from trajectory.storage import MemoryBlobStore, blob_key, decode_blob
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryEventKey, TrajectoryPayload,
    TrajectoryWorkerState)
from trajectory.tools.migrate_legacy import run
from trajectory.worker.content import dedupe_key

AT = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
LEGACY_NAMES = {
    "session_trajectories": "legacy_trajectory_session_trajectories", "trajectory_events": "legacy_trajectory_events",
    "trajectory_payloads": "legacy_trajectory_payloads", "trajectory_records": "legacy_trajectory_records",
    "trajectory_session_summaries": "legacy_trajectory_session_summaries",
    "trajectory_checkpoints": "legacy_trajectory_checkpoints", "trajectory_exports": "legacy_trajectory_exports",
}
IMAGE = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
OLD = b"\xff\xd8\xff" + bytes(range(90))
DELETED = b"deleted-image-bytes"
WHOLE = {"output": "o" * 100, "status": "completed"}
PAGE = {"records": {"baseline:trj_a": {"kind": "baseline"}}}


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ref(payload_id: str, data: bytes, media_type: str) -> dict:
    return {"payload_id": payload_id, "sha256": _sha(data), "size_bytes": len(data), "media_type": media_type,
            "availability": "available"}


def _apply_schema(connection, rename: bool) -> None:
    from alembic import op
    migration = importlib.import_module("db.migrations.versions.f6a8c0e2b4d6_session_trajectories")
    connection.exec_driver_sql("CREATE TABLE session_executions (session_id VARCHAR(64) PRIMARY KEY)")
    connection.exec_driver_sql("CREATE TABLE cron_runs (id VARCHAR(64) PRIMARY KEY)")
    # SQLite reflects "TIMESTAMP WITH TIME ZONE" as a numeric type; PostgreSQL needs it for aware datetimes.
    timestamp = "TIMESTAMP WITH TIME ZONE" if connection.dialect.name == "postgresql" else "TIMESTAMP"
    connection.exec_driver_sql("CREATE TABLE file_assets (id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64), "
                               "oss_key VARCHAR(512) NOT NULL, status VARCHAR(16) NOT NULL, "
                               f"is_deleted BOOLEAN NOT NULL DEFAULT false, deleted_at {timestamp})")
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        if rename:
            for old, new in LEGACY_NAMES.items():
                op.rename_table(old, new)


def _seed(connection, blob_dir, rename: bool, extra: bool) -> None:
    metadata = sa.MetaData()
    metadata.reflect(connection)
    sqlite = connection.dialect.name == "sqlite"

    def table(name):
        return metadata.tables[LEGACY_NAMES[name] if rename else name]

    def as_json(value):
        return json.dumps(value) if sqlite else value

    connection.execute(sa.insert(metadata.tables["file_assets"]), [
        {"id": "asset_live", "user_id": "u1", "oss_key": "assets/u1/asset_live/a.png", "status": "ready",
         "is_deleted": False, "deleted_at": None},
        {"id": "asset_gone", "user_id": "u1", "oss_key": "assets/u1/asset_gone/b.jpg", "status": "ready",
         "is_deleted": True, "deleted_at": AT}])
    trajectories = [
        {"id": "trj_a", "user_id": "u1", "session_id": "ses_a", "workspace_id": "ws_1", "started_at": AT,
         "updated_at": AT, "next_seq": 6, "committed_seq": 5, "projected_seq": 5, "schema_version": 1,
         "recording_status": "paused", "deleted_at": None},
        {"id": "trj_b", "user_id": "u1", "session_id": "ses_b", "workspace_id": "ws_1", "started_at": AT,
         "updated_at": AT, "next_seq": 3, "committed_seq": 2, "projected_seq": 2, "schema_version": 1,
         "recording_status": "deleted", "deleted_at": AT}]
    whole_bytes = _canonical(WHOLE)
    page_bytes = _canonical(PAGE)

    def payload(payload_id, trajectory_id, data, media_type, *, content=True, source=None, availability="available",
                first_seq=2):
        key = f"trajectories/{trajectory_id}/payloads/{payload_id}/{_sha(data)}"
        return {"payload_id": payload_id, "trajectory_id": trajectory_id, "sha256": _sha(data), "storage_key": key,
                "storage_status": "pending" if content else "stored", "content": data if content else None,
                "size_bytes": len(data), "media_type": media_type, "encoding": "binary", "availability": availability,
                "first_seq": first_seq, "source_asset_id": source, "created_at": AT,
                "deleted_at": AT if availability != "available" else None}

    payloads = [
        payload("pld_img", "trj_a", IMAGE, "image/png", content=False, source="asset_live"),
        payload("pld_old", "trj_a", OLD, "image/jpeg", source="asset_gone"),
        payload("pld_dup", "trj_a", OLD, "image/jpeg", source="asset_gone", first_seq=4),
        payload("pld_whole", "trj_a", whole_bytes, "application/json", content=False, first_seq=3),
        payload("pld_page", "trj_a", page_bytes, "application/json", first_seq=5),
        payload("pld_deleted", "trj_a", DELETED, "image/png", content=False, source="asset_gone",
                availability="deleted"),
        payload("pld_b", "trj_b", DELETED, "image/png", content=False, availability="deleted", first_seq=1)]
    (blob_dir / payloads[3]["storage_key"]).parent.mkdir(parents=True, exist_ok=True)
    (blob_dir / payloads[3]["storage_key"]).write_bytes(whole_bytes)
    media = lambda payload_id, data, media_type, source: {  # noqa: E731
        "$media": _ref(payload_id, data, media_type), "source_kind": "asset", "original_encoding": "base64",
        "declared_media_type": media_type, "source_asset_id": source}

    def item(seq, event_id, event_type, data, **ids):
        context = {"source_session_id": "ses_a", **ids}
        return {"event_id": event_id, "trajectory_id": "trj_a", "seq": seq, "type": event_type, "version": 1,
                "user_id": "u1", "session_id": "ses_a", "source_session_id": "ses_a",
                "request_id": ids.get("request_id"), "call_id": ids.get("call_id"), "agent_id": None,
                "context": as_json(context), "data": as_json(data), "content_hash": _sha(f"hash-{seq}".encode()),
                "occurred_at": AT.replace(second=seq), "recorded_at": AT.replace(second=seq, microsecond=5000)}

    events = [
        item(1, "evt_start_trj_a", "trajectory.started",
             {"existing_session": None, "coverage_start": "2026-09-14T08:00:00.000Z", "schema_version": 1}),
        item(2, "request:r1:prepared", "request.prepared", {"model": "m", "input": {"system": "S" * 2000, "messages": [
            {"role": "user", "content": [media("pld_img", IMAGE, "image/png", "asset_live"),
                                         media("pld_old", OLD, "image/jpeg", "asset_gone")]}]}}, request_id="r1"),
        item(3, "evt_tool", "tool.finished", {"$payload": _ref("pld_whole", whole_bytes, "application/json")},
             call_id="c1"),
        item(4, "asset:dup", "artifact.recorded", {"artifact_id": "asset_gone", "availability": "available",
                                                   "payload": _ref("pld_dup", OLD, "image/jpeg")}),
        item(5, "evt_gap", "recording.gap", {"phase": "paused", "reason": "recording_disabled",
                                             "last_recorded_seq": "4"})]
    if extra:
        trajectories.append({"id": "trj_c", "user_id": "u2", "session_id": "ses_c", "workspace_id": "ws_2",
                             "started_at": AT, "updated_at": AT, "next_seq": 2, "committed_seq": 1, "projected_seq": 1,
                             "schema_version": 1, "recording_status": "recording", "deleted_at": None})
        payloads.append(payload("pld_missing", "trj_c", b"lost bytes", "text/plain", content=False, first_seq=1))
        events.append({**item(1, "evt_c", "tool.finished", {"$payload": _ref("pld_missing", b"lost bytes",
                                                                               "text/plain")}),
                       "trajectory_id": "trj_c", "user_id": "u2", "session_id": "ses_c", "source_session_id": "ses_c"})
    connection.execute(sa.insert(table("session_trajectories")), trajectories)
    connection.execute(sa.insert(table("trajectory_payloads")), payloads)
    connection.execute(sa.insert(table("trajectory_events")), events)
    connection.execute(sa.insert(table("trajectory_checkpoints")), [{
        "trajectory_id": "trj_a", "through_seq": 5, "projector_version": 1, "digest": "0" * 64, "created_at": AT,
        "state": as_json({"records": {}, "record_pages": [{"$payload": _ref("pld_page", page_bytes, "application/json")}]})}])


async def build_business(url: str, blob_dir, *, rename: bool = True, extra: bool = False) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_apply_schema, rename)
            await connection.run_sync(_seed, blob_dir, rename, extra)
    finally:
        await engine.dispose()


async def build_trace(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(TraceBase.metadata.create_all)
    finally:
        await engine.dispose()


class Cli:
    def __init__(self, store, blob_dir):
        self.store, self.blob_dir, self.lines = store, blob_dir, []

    async def __call__(self, *argv: str) -> int:
        await close_trace_engine()
        self.lines = []
        return await run(["--legacy-blob-path", str(self.blob_dir), *argv], blob_store=self.store,
                         out=self.lines.append)


async def query(trace_url: str, function):
    await close_trace_engine()
    init_trace_engine(trace_url)
    try:
        async with trace_session() as db:
            return await function(db)
    finally:
        await close_trace_engine()


async def _all(db, model, *where):
    return list((await db.scalars(select(model).where(*where))).all())


@pytest.fixture
async def databases(tmp_path, monkeypatch):
    business = f"sqlite+aiosqlite:///{tmp_path / 'business.db'}"
    trace = f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}"
    blob_dir = tmp_path / "legacy-blobs"
    blob_dir.mkdir()
    await build_trace(trace)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", trace)
    monkeypatch.setenv("TRAJECTORY_LEGACY_DATABASE_URL", business)
    yield business, trace, blob_dir
    await close_trace_engine()


async def test_converts_verifies_and_finalizes(databases):
    business, trace, blob_dir = databases
    await build_business(business, blob_dir)
    store = MemoryBlobStore()
    cli = Cli(store, blob_dir)

    assert await cli("--dry-run") == 0
    assert "would-convert events=5 trajectory=trj_a" in cli.lines and cli.lines[-1].startswith("dry-run converted=2")
    assert await query(trace, lambda db: _all(db, SessionTrajectory)) == [] and store.objects == {}

    assert await cli() == 0, cli.lines
    assert any(line.startswith("converted events=5") and line.endswith("trajectory=trj_a") for line in cli.lines)

    async def inspect(db):
        return (await db.get(SessionTrajectory, "trj_a"), await db.get(SessionTrajectory, "trj_b"),
                list((await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.trajectory_id,
                                                                        TrajectoryEvent.seq))).all()),
                {row.payload_id: row for row in await _all(db, TrajectoryPayload)},
                await _all(db, TrajectoryEventKey), await db.get(TrajectoryWorkerState, "legacy_convert:trj_a"))

    trajectory, tombstone, events, payloads, keys, marker = await query(trace, inspect)
    assert (trajectory.user_id, trajectory.session_id, trajectory.workspace_id, trajectory.recording_status) == (
        "u1", "ses_a", "ws_1", "paused")
    assert (trajectory.next_seq, trajectory.committed_seq, trajectory.event_count) == (6, 5, 5)
    assert (trajectory.projected_seq, trajectory.archived_seq, trajectory.checkpoint_seq) == (0, 0, 0)
    assert [(row.seq, row.event_id, row.content_hash) for row in events] == [
        (seq, event_id, _sha(f"hash-{seq}".encode())) for seq, event_id in
        enumerate(["evt_start_trj_a", "request:r1:prepared", "evt_tool", "asset:dup", "evt_gap"], start=1)]
    prepared, tool = events[1], events[2]
    assert prepared.context == {"source_session_id": "ses_a", "request_id": "r1"} and prepared.request_id == "r1"
    assert prepared.data["input"]["system"]["$ref"]["kind"] == "system"
    # Two media envelopes make the message large enough to be content addressed; the envelopes keep their ids.
    message_ref = prepared.data["input"]["messages"][0]["$ref"]
    message_row = payloads[message_ref["payload_id"]]
    message = json.loads(decode_blob(store.objects[message_row.storage_key], message_row.encoding))
    assert message_ref["kind"] == "message" and message_row.first_seq == 2
    assert [part["$media"]["payload_id"] for part in message["content"]] == ["pld_img", "pld_old"]
    assert tool.data == WHOLE and tool.call_id == "c1"
    assert trajectory.stored_bytes > 0
    assert set(payloads) >= {"pld_img", "pld_old", "pld_dup", "pld_deleted"}
    assert "pld_whole" not in payloads and "pld_page" not in payloads
    image, old, duplicate, deleted = (payloads[name] for name in ("pld_img", "pld_old", "pld_dup", "pld_deleted"))
    assert (image.storage_kind, image.storage_key, image.stored_bytes, image.first_seq) == (
        "asset", "assets/u1/asset_live/a.png", 0, 2)
    assert (old.storage_kind, old.storage_key, old.encoding, old.first_seq) == (
        "blob", blob_key("trj_a", _sha(OLD)), "identity", 2)
    assert old.dedupe_key == dedupe_key(_sha(OLD), "image/jpeg", "asset_gone", "blob")
    assert duplicate.storage_key == old.storage_key and duplicate.dedupe_key != old.dedupe_key
    assert (deleted.availability, deleted.first_seq) == ("deleted", 2)
    [system_row] = [row for row in payloads.values() if row.payload_id == prepared.data["input"]["system"]["$ref"]["payload_id"]]
    assert system_row.first_seq == 2 and system_row.media_type == "application/json"
    assert decode_blob(store.objects[old.storage_key], old.encoding) == OLD
    assert len(keys) == 5 and marker.value["events"] == 5
    assert tombstone.deleted_at is not None and tombstone.recording_status == "deleted"
    assert [row.availability for row in payloads.values() if row.trajectory_id == "trj_b"] == ["deleted"]

    assert await cli() == 0
    assert "skipped trajectory=trj_a" in cli.lines and "skipped trajectory=trj_b" in cli.lines
    assert await cli("--verify") == 0, cli.lines
    assert (await query(trace, lambda db: db.get(TrajectoryWorkerState, "legacy_verify"))).value["fingerprint"] == {
        "trajectories": 2, "events": 5, "payloads": 7}

    async def tamper(db, value):
        row = await db.get(TrajectoryEvent, ("trj_a", 3))
        original = row.content_hash
        row.content_hash = value or ("f" * 64)
        return original

    original = await query(trace, lambda db: tamper(db, None))
    assert await cli("--verify") == 1
    assert "mismatch trajectory=trj_a seq 3 event_id or content_hash differs" in cli.lines
    assert await query(trace, lambda db: db.get(TrajectoryWorkerState, "legacy_verify")) is None
    assert await cli("--finalize-drop") == 1 and cli.lines == ["refused: no successful full --verify is recorded"]
    await query(trace, lambda db: tamper(db, original))
    assert await cli("--verify") == 0
    assert await cli("--finalize-drop") == 0
    assert cli.lines[0] == "dropped legacy_trajectory_exports" and len(cli.lines) == 7
    engine = create_async_engine(business)
    async with engine.connect() as connection:
        names = set(await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names()))
    await engine.dispose()
    assert not {name for name in names if name.startswith("legacy_")} and "file_assets" in names


async def test_missing_payload_bytes_fail_only_that_trajectory(databases):
    business, trace, blob_dir = databases
    await build_business(business, blob_dir, extra=True)
    cli = Cli(MemoryBlobStore(), blob_dir)
    assert await cli() == 1
    assert any(line.startswith("failed trajectory=trj_c") for line in cli.lines)
    assert cli.lines[-1].startswith("convert converted=2 skipped=0 failed=1")
    rows = await query(trace, lambda db: _all(db, SessionTrajectory))
    assert sorted(row.id for row in rows) == ["trj_a", "trj_b"]
    assert await cli("--verify") == 1
    assert "mismatch trajectory=trj_c trajectory missing in the trace database" in cli.lines


async def test_original_table_names(databases):
    business, trace, blob_dir = databases
    await build_business(business, blob_dir, rename=False)
    cli = Cli(MemoryBlobStore(), blob_dir)
    assert await cli() == 2 and cli.lines == ["error: legacy trajectory tables not found for: trajectories, events, payloads"]
    assert await cli("--source-tables", "original", "--only", "trj_a") == 0
    assert [row.id for row in await query(trace, lambda db: _all(db, SessionTrajectory))] == ["trj_a"]
    assert await cli("--source-tables", "original", "--finalize-drop") == 2


async def test_usage_errors(databases, monkeypatch, tmp_path):
    business, trace, blob_dir = databases
    cli = Cli(MemoryBlobStore(), blob_dir)
    assert await cli("--verify", "--finalize-drop") == 2
    monkeypatch.delenv("TRAJECTORY_LEGACY_DATABASE_URL")
    assert await cli() == 2 and "TRAJECTORY_LEGACY_DATABASE_URL" in cli.lines[0]
    assert await cli("--business-database-url", trace) == 2
    assert cli.lines == ["error: the trace database must not be the business database"]
    monkeypatch.delenv("TRAJECTORY_DATABASE_URL")
    assert await cli("--business-database-url", business) == 2
