"""Harness shared by the projection and trace read tests (no tests of its own).

A SQLite trace database per test, an in-memory blob store, a stand-in for the
worker metrics registry, metadata replica rows, and ``Ingest``: a minimal
stand-in for worker content preparation (SPEC 8.4 steps 2 and 5-7; the real
one belongs to the ingest package). It stores fixture events the way ingest
does, at thresholds small enough to put references everywhere: hints.preview
from the original values, request inputs split into system/tools/message
``$ref`` blobs, large values as ``$ref`` kind value and oversized data as
``$payload``. ``archive`` moves a hot prefix into a segment like the archive
service.
"""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from trajectory.payload import dedupe_key, is_ref, reset_blob_cache, set_asset_reader
from trajectory.projector import PREVIEW_FIELDS, RESULT_FIELDS, _preview
from trajectory.repository import reset_segment_cache
from trajectory.segments import encode_segment
from trajectory.storage import MemoryBlobStore, blob_key, encode_blob, segment_key, set_blob_store
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryEventKey, TrajectoryMetaAsset,
    TrajectoryMetaSession, TrajectoryMetaUser, TrajectoryMetaWorkspace, TrajectoryPayload, TrajectorySegment)
from trajectory.types import ID_FIELDS, canonical, digest
from trajectory.worker.archive import segment_row

FIXTURES = sorted((Path(__file__).parents[2] / "trajectory" / "fixtures").glob("*.json"))
AT = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
JSON = "application/json"
#: Thresholds that turn most fixture values into references (fixture reasons,
#: errors, titles and usage stay below the value limit, as real ones stay below 64 KiB).
REFERENCES = {"system_bytes": 16, "tools_bytes": 16, "message_bytes": 16, "inline_bytes": 40, "payload_bytes": 150}


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text())


def settings(**overrides) -> SimpleNamespace:
    values = {"projection_batch_events": 200, "record_inline_bytes": 16384, "checkpoint_interval": 1000}
    values.update(overrides)
    return SimpleNamespace(**values)


class Metrics:
    """Stand-in for trajectory.worker.metrics.Metrics (inc, set_gauge, snapshot)."""

    def __init__(self):
        self.counters: dict[str, float] = {}
        self.gauges: dict[str, float] = {}

    def inc(self, name, value=1):
        self.counters[name] = self.counters.get(name, 0) + value

    def set_gauge(self, name, value):
        self.gauges[name] = value

    def snapshot(self):
        return {"counters": dict(self.counters), "gauges": dict(self.gauges), "uptime_seconds": 0.0}


@pytest.fixture
async def trace_db(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    yield engine
    await close_trace_engine()


@pytest.fixture
def blobs():
    store = MemoryBlobStore()
    set_blob_store(store)
    reset_blob_cache()
    reset_segment_cache()
    yield store
    set_blob_store(None)
    set_asset_reader(None)
    reset_blob_cache()
    reset_segment_cache()


def instant(value) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def add_meta(session_id, user_id="user_a", workspace_id="ws_a", *, title=None, status="idle", parent_id=None,
                   updated_at=AT, is_deleted=False, model=None, agent=None, username=None, email=None,
                   workspace_name=None, user=True, workspace=True):
    async with trace_session() as db:
        await db.merge(TrajectoryMetaSession(id=session_id, user_id=user_id, workspace_id=workspace_id, title=title,
            status=status, model=model, agent=agent, parent_id=parent_id, is_deleted=is_deleted,
            deleted_at=AT if is_deleted else None, created_at=AT, updated_at=updated_at, synced_at=AT))
        if user:
            await db.merge(TrajectoryMetaUser(id=user_id, username=username or f"name-{user_id}",
                email=email or f"{user_id}@example.invalid", role="user", updated_at=AT, synced_at=AT))
        if workspace and workspace_id:
            await db.merge(TrajectoryMetaWorkspace(id=workspace_id, name=workspace_name or f"workspace-{workspace_id}",
                updated_at=AT, synced_at=AT))


async def add_asset(asset_id, *, user_id="user_a", oss_key=None, status="ready", is_deleted=False, deleted_at=None):
    async with trace_session() as db:
        await db.merge(TrajectoryMetaAsset(id=asset_id, user_id=user_id, workspace_id="ws_a", session_id=None,
            oss_key=oss_key or f"assets/{asset_id}", mime="image/png", size=3, status=status, is_deleted=is_deleted,
            deleted_at=deleted_at, updated_at=AT, synced_at=AT))


async def add_trajectory(trajectory_id, session_id, user_id="user_a", workspace_id="ws_a", *, started_at=AT,
                         recording_status="recording"):
    async with trace_session() as db:
        db.add(SessionTrajectory(id=trajectory_id, user_id=user_id, session_id=session_id, workspace_id=workspace_id,
            started_at=started_at, updated_at=started_at, last_activity_at=started_at, recording_status=recording_status))


async def add_payload(store, trajectory_id, content: bytes, *, first_seq, media_type="application/octet-stream",
                      storage_kind="blob", source_asset_id=None, availability="available", storage_key=None) -> TrajectoryPayload:
    sha = hashlib.sha256(content).hexdigest()
    stored, encoding = encode_blob(content, media_type)
    key = storage_key or (blob_key(trajectory_id, sha) if storage_kind == "blob" else f"assets/{source_asset_id}")
    if storage_kind == "blob":
        await store.put(key, stored, content_type=media_type)
    row = TrajectoryPayload(payload_id=f"pld_{uuid4().hex}", trajectory_id=trajectory_id,
        dedupe_key=dedupe_key(sha, media_type, source_asset_id, storage_kind), sha256=sha, size_bytes=len(content),
        stored_bytes=len(stored) if storage_kind == "blob" else 0, media_type=media_type,
        encoding=encoding if storage_kind == "blob" else "identity", storage_kind=storage_kind, storage_key=key,
        source_asset_id=source_asset_id, availability=availability, first_seq=first_seq, created_at=AT,
        deleted_at=AT if availability != "available" else None)
    async with trace_session() as db:
        db.add(row)
    return row


class Ingest:
    """Stores prepared events at chosen reference thresholds (None: never)."""

    def __init__(self, store, *, system_bytes=None, tools_bytes=None, message_bytes=None, inline_bytes=None,
                 payload_bytes=None):
        self.store = store
        self.system_bytes, self.tools_bytes, self.message_bytes = system_bytes, tools_bytes, message_bytes
        self.inline_bytes, self.payload_bytes = inline_bytes, payload_bytes

    async def append(self, trajectory_id: str, events: list[dict]) -> None:
        async with trace_session() as db:
            trajectory = await db.get(SessionTrajectory, trajectory_id)
            for event in events:
                seq = int(event["seq"])
                assert seq == trajectory.committed_seq + 1, "events must be appended in order"
                data, hints = await self._prepare(db, trajectory_id, event, seq)
                occurred = instant(event["occurred_at"])
                identity = {key: event[key] for key in ID_FIELDS if event.get(key) is not None}
                content_hash = digest({key: value for key, value in event.items() if key not in {"event_id", "occurred_at"}})
                db.add(TrajectoryEvent(trajectory_id=trajectory_id, seq=seq, recorded_on=occurred.date(),
                    event_id=event["event_id"], type=event["type"], version=event.get("version", 1),
                    user_id=event["user_id"], session_id=event["session_id"],
                    source_session_id=event.get("source_session_id") or event["session_id"],
                    request_id=event.get("request_id"), call_id=event.get("call_id"), agent_id=event.get("agent_id"),
                    context=identity, data=data, hints=hints, content_hash=content_hash, occurred_at=occurred,
                    recorded_at=instant(event.get("recorded_at") or event["occurred_at"])))
                db.add(TrajectoryEventKey(event_id=event["event_id"], trajectory_id=trajectory_id, seq=seq,
                                          content_hash=content_hash, recorded_at=occurred))
                trajectory.committed_seq, trajectory.next_seq = seq, seq + 1
                trajectory.event_count += 1
                last = trajectory.last_activity_at if trajectory.last_activity_at.tzinfo else trajectory.last_activity_at.replace(tzinfo=timezone.utc)
                trajectory.last_activity_at = max(last, occurred)
                await db.flush()

    @staticmethod
    def _over(value, limit) -> bool:
        return limit is not None and len(canonical(value)) >= limit

    async def _prepare(self, db, trajectory_id, event, seq):
        data = deepcopy(event.get("data", {}))
        previews = {field: _preview(data[field]) for field in (*PREVIEW_FIELDS, *RESULT_FIELDS) if data.get(field) is not None}
        hints = {"preview": previews} if previews else None
        actual = data.get("input") if event["type"] == "request.prepared" else None
        if isinstance(actual, dict):
            for field, kind, limit in (("system", "system", self.system_bytes), ("instructions", "system", self.system_bytes),
                                       ("tools", "tools", self.tools_bytes)):
                if actual.get(field) is not None and self._over(actual[field], limit):
                    actual[field] = await self.reference(db, trajectory_id, actual[field], kind, seq)
            for field in ("messages", "input"):
                if isinstance(actual.get(field), list):
                    actual[field] = [await self.reference(db, trajectory_id, item, "message", seq)
                                     if self._over(item, self.message_bytes) else item for item in actual[field]]
        data = await self._values(db, trajectory_id, data, seq, 0)
        if self.payload_bytes is not None and len(canonical(data)) > self.payload_bytes:
            data = {"$payload": await self.payload(db, trajectory_id, data, seq)}
        return data, hints

    async def _values(self, db, trajectory_id, value, seq, depth):
        if is_ref(value) or not isinstance(value, (dict, list)):
            return value
        items = value.items() if isinstance(value, dict) else enumerate(value)
        result = {} if isinstance(value, dict) else [None] * len(value)
        for key, child in items:
            if not is_ref(child) and isinstance(child, (dict, list, str)) and self.inline_bytes is not None \
                    and len(canonical(child)) > self.inline_bytes:
                child = await self.reference(db, trajectory_id, child, "value", seq)
            elif depth < 6:
                child = await self._values(db, trajectory_id, child, seq, depth + 1)
            result[key] = child
        return result

    async def _row(self, db, trajectory_id, content: bytes, seq) -> TrajectoryPayload:
        sha = hashlib.sha256(content).hexdigest()
        key = dedupe_key(sha, JSON, None, "blob")
        row = await db.scalar(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
                                                              TrajectoryPayload.dedupe_key == key))
        if row is None:
            stored, encoding = encode_blob(content, JSON)
            await self.store.put(blob_key(trajectory_id, sha), stored, content_type=JSON)
            row = TrajectoryPayload(payload_id=f"pld_{uuid4().hex}", trajectory_id=trajectory_id, dedupe_key=key,
                sha256=sha, size_bytes=len(content), stored_bytes=len(stored), media_type=JSON, encoding=encoding,
                storage_kind="blob", storage_key=blob_key(trajectory_id, sha), source_asset_id=None,
                availability="available", first_seq=seq, created_at=AT)
            db.add(row)
            await db.flush()
        return row

    async def reference(self, db, trajectory_id, value, kind, seq) -> dict:
        row = await self._row(db, trajectory_id, canonical(value), seq)
        return {"$ref": {"sha256": row.sha256, "size_bytes": row.size_bytes, "media_type": JSON, "kind": kind,
                         "payload_id": row.payload_id}}

    async def payload(self, db, trajectory_id, value, seq) -> dict:
        row = await self._row(db, trajectory_id, canonical(value), seq)
        return {"payload_id": row.payload_id, "sha256": row.sha256, "size_bytes": row.size_bytes,
                "media_type": JSON, "availability": "available"}


async def archive(store, trajectory_id: str, through_seq: int) -> None:
    """Move hot events archived_seq+1..through_seq into one segment, as the archive service does."""
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id)
        start = trajectory.archived_seq + 1
        events = TrajectoryEvent.__table__
        rows = (await db.execute(select(events).where(events.c.trajectory_id == trajectory_id,
            events.c.seq >= start, events.c.seq <= through_seq).order_by(events.c.seq))).mappings().all()
        stored, meta = encode_segment([segment_row(row) for row in rows])
        key = segment_key(trajectory_id, meta["from_seq"], meta["to_seq"])
        await store.put(key, stored, content_type="application/zstd", if_absent=False)
        db.add(TrajectorySegment(trajectory_id=trajectory_id, from_seq=meta["from_seq"], to_seq=meta["to_seq"],
            storage_key=key, event_count=meta["event_count"], raw_bytes=meta["raw_bytes"],
            stored_bytes=meta["stored_bytes"], sha256=meta["sha256"], compression="zstd", created_at=AT))
        await db.execute(delete(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == trajectory_id,
            TrajectoryEvent.seq >= start, TrajectoryEvent.seq <= through_seq))
        trajectory.archived_seq = through_seq


async def project_all(service, trajectory_id: str, *, max_events=None) -> int:
    projected = 0
    while True:
        count = await service.project(trajectory_id, max_events=max_events)
        await service.maybe_checkpoint(trajectory_id)
        if not count:
            return projected
        projected += count
