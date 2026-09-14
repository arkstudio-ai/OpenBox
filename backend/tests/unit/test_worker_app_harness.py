"""Shared fixtures for the trajectory worker service tests; this module has no tests of its own.

``ReadLayer`` stands in for the trace-database read functions of
trajectory.repository, trajectory.payload and trajectory.export: a small
implementation of their documented semantics over a seeded trace database. The
service tests therefore pin the worker's routes, authorization, audit and
socket contract independently of the projection and archive internals. The
viewer backend is the real ``api.internal`` router over a business database,
reached through the worker's HTTP client.
"""
import asyncio
import copy
import hashlib
import io
import json
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth.jwt as jwt_module
import auth.middleware as middleware
import auth.ticket as tickets
import db.base as database
from auth.jwt import create_access_token
from cache.memory_cache import MemoryCache
from core.config import OpenBoxConfig
from db.base import Base
from db.models.user import User
from trajectory import auth as trajectory_auth
from trajectory.storage import MemoryBlobStore, blob_key, decode_blob, encode_blob, export_key
from trajectory.store.database import TraceBase, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryEvent, TrajectoryExport,
    TrajectoryMetaAsset, TrajectoryMetaSession, TrajectoryMetaUser, TrajectoryMetaWorkspace, TrajectoryPayload,
    TrajectoryRecord)
from trajectory.types import CorruptContent, TrajectoryError, canonical, iso, now, sequence

SECRET = "trajectory-worker-service-tests-key"
INTERNAL_TOKEN = "trajectory-internal-test-token"
PRIVATE = b"PRIVATE_MEDIA_CONTENT"
PREFIX = "/api/admin/trajectories"
SESSION = f"{PREFIX}/sessions/session_a_1"
SYSTEM_PROMPT = {"role": "system", "content": "You are the fixture assistant. " * 60}
SYSTEM_SHA = hashlib.sha256(canonical(SYSTEM_PROMPT)).hexdigest()
MEDIA_SHA = hashlib.sha256(PRIVATE).hexdigest()
ENCODED_ARTIFACTS = ("file:/workspace/report.md", "file:/workspace/100%/literal%2Fname.md",
                     "file:/workspace/报告 ?draft#1.md")


def token(user_id: str = "admin", role: str = "admin", **claims) -> str:
    return create_access_token(user_id, role, session_claims=claims or None)


def export_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("payloads/pld_media", PRIVATE)
    return buffer.getvalue()


# -- Read layer stand-in --

def _event(row: TrajectoryEvent) -> dict:
    return {"event_id": row.event_id, "trajectory_id": row.trajectory_id, "user_id": row.user_id,
            "session_id": row.session_id, **row.context, "seq": str(row.seq), "type": row.type,
            "version": row.version, "occurred_at": iso(row.occurred_at), "recorded_at": iso(row.recorded_at),
            "data": row.data}


class ReadLayer:
    """Documented read semantics over the seeded trace tables; ``calls`` records every invocation."""

    REPOSITORY = ("get_trajectory", "watermark", "get_session_header", "list_sessions", "read_events",
                  "list_records", "get_record", "get_checkpoint", "search")
    PAYLOAD = ("validate_payload", "read_payload", "payload_meta", "read_blob")
    EXPORT = ("create_export", "export_status", "read_export")

    def __init__(self, blob: MemoryBlobStore):
        self.blob = blob
        self.calls: list[tuple[str, tuple, dict]] = []

    def install(self, monkeypatch) -> None:
        import trajectory.export as exports
        import trajectory.payload as payloads
        import trajectory.repository as repository
        for module, names in ((repository, self.REPOSITORY), (payloads, self.PAYLOAD), (exports, self.EXPORT)):
            for name in names:
                monkeypatch.setattr(module, name, self._recorded(name), raising=False)

    def _recorded(self, name):
        function = getattr(self, name)
        if asyncio.iscoroutinefunction(function):
            async def call(*args, **kwargs):
                self.calls.append((name, args[1:], kwargs))
                return await function(*args, **kwargs)
        else:
            def call(*args, **kwargs):
                self.calls.append((name, args[1:], kwargs))
                return function(*args, **kwargs)
        return call

    def called(self, name) -> list[tuple[tuple, dict]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    # trajectory.repository
    async def get_trajectory(self, db, session_id, *, optional=False):
        meta = await db.get(TrajectoryMetaSession, session_id)
        if meta is not None and meta.is_deleted:
            raise LookupError("Session not found")
        trajectory = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == session_id,
                                                                     SessionTrajectory.deleted_at.is_(None)))
        if meta is None and trajectory is None:
            raise LookupError("Session not found")
        if trajectory is None and not optional:
            raise LookupError("Session has not started recording")
        return meta, trajectory

    def watermark(self, trajectory, requested=None):
        head = trajectory.committed_seq if trajectory is not None else 0
        return head if requested is None else sequence(requested, maximum=head)

    async def _row(self, db, meta, trajectory, through=None) -> dict:
        owner = await db.get(TrajectoryMetaUser, meta.user_id)
        workspace = await db.get(TrajectoryMetaWorkspace, meta.workspace_id)
        committed = str(trajectory.committed_seq) if trajectory else "0"
        return {"session_id": meta.id, "user_id": meta.user_id, "trajectory_id": trajectory.id if trajectory else None,
                "title": meta.title, "owner": {"user_id": meta.user_id, "username": owner.username if owner else None,
                                               "email": owner.email if owner else None},
                "workspace": {"id": meta.workspace_id, "name": workspace.name if workspace else None},
                "workspace_id": meta.workspace_id, "running_status": meta.status,
                "recording_status": trajectory.recording_status if trajectory else "not_recorded",
                "coverage_start": iso(trajectory.started_at) if trajectory else None,
                "last_activity_at": iso(trajectory.last_activity_at if trajectory else meta.updated_at),
                "model": meta.model, "agent": meta.agent, "committed_seq": committed,
                "projected_through_seq": str(trajectory.projected_seq) if trajectory else "0",
                "through_seq": str(through) if through is not None else committed,
                "statistics": {"request_count": 0, "tool_count": 0, "error_count": 0, "unknown_count": 0,
                               "input_tokens": None, "output_tokens": None, "usage_complete": True,
                               "duration_ms": None, "through_seq": committed,
                               "coverage_start": iso(trajectory.started_at) if trajectory else None}}

    async def get_session_header(self, db, session_id, through_seq=None):
        meta, trajectory = await self.get_trajectory(db, session_id, optional=True)
        head = self.watermark(trajectory, through_seq)
        return {**await self._row(db, meta, trajectory, head), "agents": [], "projector_version": 1,
                "capabilities": {"recording": True, "admin_read": True, "export": trajectory is not None},
                "unsupported_events": []}

    async def list_sessions(self, db, *, user_id=None, user_query=None, q=None, workspace_id=None, status=None,
                            recording_status=None, activity_from=None, activity_to=None, include_unrecorded=False,
                            cursor=None, limit=50, sort="last_activity_desc"):
        if sort not in {"last_activity_desc", "last_activity_asc"}:
            raise TrajectoryError("Unsupported session sort")
        offset = 0
        if cursor is not None:
            if not cursor.startswith("offset-") or not cursor[7:].isdigit():
                raise TrajectoryError("Invalid trajectory cursor")
            offset = int(cursor[7:])
        rows = []
        for meta in (await db.scalars(select(TrajectoryMetaSession).where(TrajectoryMetaSession.is_deleted.is_(False))
                                      .order_by(TrajectoryMetaSession.id))).all():
            trajectory = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == meta.id,
                                                                         SessionTrajectory.deleted_at.is_(None)))
            if (trajectory is None and not include_unrecorded) or (user_id and meta.user_id != user_id):
                continue
            rows.append(await self._row(db, meta, trajectory))
        page = rows[offset:offset + limit]
        more = offset + limit < len(rows)
        return {"items": page, "next_cursor": f"offset-{offset + limit}" if more else None, "has_more": more}

    async def read_events(self, db, trajectory, *, after_seq=0, until_seq=None, limit=500, include_data=True):
        until = self.watermark(trajectory, until_seq)
        after = sequence(after_seq, maximum=until)
        rows = (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == trajectory.id,
            TrajectoryEvent.seq > after, TrajectoryEvent.seq <= until).order_by(TrajectoryEvent.seq)
            .limit(limit + 1))).all()
        has_more = len(rows) > limit
        events, expected = [], after + 1
        for row in rows[:limit]:
            if row.seq != expected:
                raise CorruptContent(f"Trajectory sequence gap before {row.seq}")
            expected += 1
            events.append(_event(row))
        if not has_more and expected - 1 != until:
            raise CorruptContent("Committed trajectory tail is missing")
        return {"events": events, "from_seq": str(after + 1) if events else str(after),
                "through_seq": events[-1]["seq"] if events else str(after), "until_seq": str(until),
                "has_more": has_more, "committed_seq": str(trajectory.committed_seq)}

    async def list_records(self, db, trajectory, *, through_seq=None, before=None, limit=100, kind=None, status=None,
                           agent_id=None):
        head = self.watermark(trajectory, through_seq)
        if before is not None:
            raise TrajectoryError("Record cursor belongs to another watermark")
        query = select(TrajectoryRecord.summary).where(TrajectoryRecord.trajectory_id == trajectory.id,
                                                       TrajectoryRecord.start_seq <= head)
        for value, column in ((kind, TrajectoryRecord.kind), (status, TrajectoryRecord.status),
                              (agent_id, TrajectoryRecord.agent_id)):
            if value:
                query = query.where(column == value)
        items = (await db.scalars(query.order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id))).all()
        return {"items": list(items)[-limit:], "next_cursor": None, "has_more": len(items) > limit,
                "through_seq": str(head), "projector_version": 1, "unsupported_events": []}

    async def _expand_refs(self, db, trajectory, value, through):
        if isinstance(value, list):
            return [await self._expand_refs(db, trajectory, child, through) for child in value]
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("$ref"), dict):
            return json.loads(await self.read_blob(db, trajectory, value["$ref"]["sha256"], through_seq=through))
        return {key: await self._expand_refs(db, trajectory, child, through) for key, child in value.items()}

    async def get_record(self, db, trajectory, record_id, *, through_seq=None, expand="full"):
        head = self.watermark(trajectory, through_seq)
        row = await db.get(TrajectoryRecord, (trajectory.id, record_id))
        if row is None or row.start_seq > head:
            raise LookupError("Record is not available at this position")
        record = copy.deepcopy(row.data)
        if expand == "full":
            record["data"] = await self._expand_refs(db, trajectory, record["data"], head)
        events = (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == trajectory.id,
            TrajectoryEvent.seq >= row.start_seq, TrajectoryEvent.seq <= head).order_by(TrajectoryEvent.seq))).all()
        return {"record": {**record, "as_of_seq": str(head), "events": [_event(event) for event in events]},
                "through_seq": str(head), "projector_version": 1}

    async def get_checkpoint(self, db, trajectory, at_seq):
        row = await db.scalar(select(TrajectoryCheckpoint).where(TrajectoryCheckpoint.trajectory_id == trajectory.id,
            TrajectoryCheckpoint.through_seq <= at_seq).order_by(TrajectoryCheckpoint.through_seq.desc()).limit(1))
        if row is None:
            return None
        return {"through_seq": str(row.through_seq), "projector_version": row.projector_version,
                "state": row.state, "digest": row.digest}

    async def search(self, db, trajectory, *, q, through_seq=None, cursor=None, limit=50):
        head = self.watermark(trajectory, through_seq)
        if cursor is not None:
            raise TrajectoryError("Search cursor belongs to another query or watermark")
        rows = (await db.scalars(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory.id,
            TrajectoryRecord.start_seq <= head).order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id))).all()
        hits = [{"record_id": row.record_id, "seq": row.summary["as_of_seq"], "kind": row.kind,
                 "preview": row.search_doc[:240]} for row in rows if q.casefold() in row.search_doc.casefold()]
        return {"items": hits[:limit], "next_cursor": None, "has_more": len(hits) > limit, "through_seq": str(head)}

    # trajectory.payload
    async def validate_payload(self, db, trajectory_id, payload_id, *, through_seq):
        row = await db.scalar(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
            TrajectoryPayload.payload_id == payload_id, TrajectoryPayload.first_seq <= through_seq))
        if row is None:
            raise LookupError("Trajectory payload is not available at this position")
        if row.availability != "available":
            raise FileNotFoundError("Trajectory content has been deleted")
        if row.source_asset_id:
            asset = await db.get(TrajectoryMetaAsset, row.source_asset_id)
            if asset is None or asset.is_deleted or asset.deleted_at is not None:
                raise FileNotFoundError("Source attachment has been deleted")
        return row

    async def read_payload(self, db, trajectory_id, payload_id, *, through_seq):
        row = await self.validate_payload(db, trajectory_id, payload_id, through_seq=through_seq)
        try:
            content = decode_blob(await self.blob.get(row.storage_key), row.encoding)
        except FileNotFoundError as exc:
            raise CorruptContent("Retained trajectory blob is missing") from exc
        if row.sha256 is not None and hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent("Trajectory content digest mismatch")
        return row, content

    async def payload_meta(self, db, trajectory, payload_id, *, through_seq):
        row = await self.validate_payload(db, trajectory.id, payload_id, through_seq=through_seq)
        return {"payload_id": row.payload_id, "availability": row.availability, "media_type": row.media_type,
                "size_bytes": row.size_bytes, "sha256": row.sha256}

    async def read_blob(self, db, trajectory, sha256, *, through_seq):
        row = await db.scalar(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory.id,
            TrajectoryPayload.sha256 == sha256, TrajectoryPayload.storage_kind == "blob")
            .order_by(TrajectoryPayload.first_seq).limit(1))
        if row is None or row.first_seq > through_seq:
            raise LookupError("Blob is not available at this position")
        if row.availability != "available":
            raise FileNotFoundError("Trajectory content has been deleted")
        try:
            content = decode_blob(await self.blob.get(row.storage_key), row.encoding)
        except FileNotFoundError as exc:
            raise CorruptContent("Retained trajectory blob is missing") from exc
        if hashlib.sha256(content).hexdigest() != sha256:
            raise CorruptContent("Trajectory content digest mismatch")
        return content

    # trajectory.export
    def create_export(self, db, trajectory, viewer_id, through_seq):
        timestamp = now()
        row = TrajectoryExport(id=f"exp_{uuid4().hex}", trajectory_id=trajectory.id, viewer_id=viewer_id,
                               through_seq=through_seq, status="pending", created_at=timestamp, updated_at=timestamp)
        db.add(row)
        return row

    def export_status(self, row, session_id):
        return {"export_id": row.id, "status": row.status, "through_seq": str(row.through_seq), "error": row.error,
                "download_url": f"{PREFIX}/sessions/{session_id}/exports/{row.id}/download"
                if row.status == "completed" else None}

    async def read_export(self, db, trajectory, export_id):
        row = await db.scalar(select(TrajectoryExport).where(TrajectoryExport.id == export_id,
                                                            TrajectoryExport.trajectory_id == trajectory.id))
        if row is None:
            raise LookupError("Export does not belong to this trajectory")
        try:
            content = await self.blob.get(row.storage_key)
        except FileNotFoundError as exc:
            raise CorruptContent("Export blob is missing") from exc
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent("Export digest mismatch")
        return row, content


# -- Seeded trace database --

def _record(record_id: str, kind: str, start_seq: int, data: dict, *, status: str = "completed") -> dict:
    return {"record_id": record_id, "kind": kind, "title": kind, "preview": None, "result_preview": None,
            "status": status, "status_reason": None, "start_seq": str(start_seq), "end_seq": str(start_seq),
            "as_of_seq": str(start_seq), "started_at": None, "finished_at": None, "duration_ms": None,
            "timing_source": None, "data": data, "blocks": [], "usage": {}}


async def seed_trace(blob: MemoryBlobStore) -> None:
    """Two users' sessions: session_a_1 (3 events, records, payloads, exports), session_a_2 unrecorded, session_b_1."""
    stamp = now()
    system_stored, system_encoding = encode_blob(canonical(SYSTEM_PROMPT), "application/json")
    await blob.put(blob_key("trj_a1", SYSTEM_SHA), system_stored, content_type="application/json")
    await blob.put(blob_key("trj_a1", MEDIA_SHA), PRIVATE, content_type="image/png")
    await blob.put("assets/a/race.png", PRIVATE, content_type="image/png")
    archive = export_zip()
    archive_sha = hashlib.sha256(archive).hexdigest()
    await blob.put(export_key("exp_ready", archive_sha), archive, content_type="application/zip")
    system_ref = {"$ref": {"sha256": SYSTEM_SHA, "size_bytes": len(canonical(SYSTEM_PROMPT)),
                           "media_type": "application/json", "kind": "system", "payload_id": "pld_system"}}
    media_ref = {"payload_id": "pld_media", "sha256": MEDIA_SHA, "size_bytes": len(PRIVATE),
                 "media_type": "image/png", "availability": "available"}
    async with trace_session() as db:
        for user_id, role in (("admin", "admin"), ("a", "user"), ("b", "user")):
            db.add(TrajectoryMetaUser(id=user_id, username=user_id, email=f"{user_id}@example.test", role=role,
                                      updated_at=stamp, synced_at=stamp))
            db.add(TrajectoryMetaWorkspace(id=f"ws_{user_id}", name=f"Workspace {user_id}", updated_at=stamp,
                                           synced_at=stamp))
        for session_id, user_id in (("session_a_1", "a"), ("session_a_2", "a"), ("session_b_1", "b")):
            db.add(TrajectoryMetaSession(id=session_id, user_id=user_id, workspace_id=f"ws_{user_id}",
                                         kind="chat", title=f"Session {session_id}", status="idle", model="fixture",
                                         agent="build", updated_at=stamp, synced_at=stamp, created_at=stamp))
        db.add(TrajectoryMetaAsset(id="race_asset", user_id="a", workspace_id="ws_a", session_id="session_a_1",
                                   oss_key="assets/a/race.png", mime="image/png", size=len(PRIVATE), status="ready",
                                   updated_at=stamp, synced_at=stamp))
        for trajectory_id, session_id, user_id, head in (("trj_a1", "session_a_1", "a", 3), ("trj_b1", "session_b_1", "b", 1)):
            db.add(SessionTrajectory(id=trajectory_id, user_id=user_id, session_id=session_id,
                                     workspace_id=f"ws_{user_id}", started_at=stamp, updated_at=stamp,
                                     last_activity_at=stamp, next_seq=head + 1, committed_seq=head, projected_seq=head,
                                     event_count=head))
        await db.flush()
        events = (("trj_a1", "session_a_1", "a", 1, "trajectory.started", {"coverage_start": iso(stamp)}),
                  ("trj_a1", "session_a_1", "a", 2, "request.prepared", {"input": {"system": system_ref}}),
                  ("trj_a1", "session_a_1", "a", 3, "artifact.recorded", {"artifact_id": "race_asset", "payload": media_ref}),
                  ("trj_b1", "session_b_1", "b", 1, "trajectory.started", {"coverage_start": iso(stamp)}))
        for trajectory_id, session_id, user_id, seq, kind, data in events:
            db.add(TrajectoryEvent(trajectory_id=trajectory_id, seq=seq, recorded_on=stamp.date(),
                                   event_id=f"evt_{trajectory_id}_{seq}", type=kind, version=1, user_id=user_id,
                                   session_id=session_id, source_session_id=session_id, context={}, data=data,
                                   content_hash="0" * 64, occurred_at=stamp, recorded_at=stamp))
        records = [_record("baseline:trj_a1", "baseline", 1, {"coverage_start": iso(stamp)}),
                   _record("request:req_a", "request", 2, {"input": {"system": system_ref}}),
                   _record("artifact:race_asset", "artifact", 3, {"artifact_id": "race_asset", "payload": media_ref,
                                                                   "text": "race fixture"})]
        records += [_record(f"artifact:{artifact_id}", "artifact", 3, {"artifact_id": artifact_id, "text": "stored fixture"})
                    for artifact_id in ENCODED_ARTIFACTS]
        for value in records:
            db.add(TrajectoryRecord(trajectory_id="trj_a1", record_id=value["record_id"], kind=value["kind"],
                                    status=value["status"], start_seq=int(value["start_seq"]),
                                    end_seq=int(value["end_seq"]), applied_seq=int(value["as_of_seq"]),
                                    projector_version=1, data=value,
                                    summary={key: item for key, item in value.items() if key not in {"data", "blocks"}},
                                    search_doc=f"{value['kind']} {value['record_id']} {json.dumps(value['data'])}"[:4000]))
        payloads = (("pld_system", SYSTEM_SHA, len(canonical(SYSTEM_PROMPT)), len(system_stored), "application/json",
                     system_encoding, "blob", blob_key("trj_a1", SYSTEM_SHA), None, 2),
                    ("pld_media", MEDIA_SHA, len(PRIVATE), len(PRIVATE), "image/png", "identity", "blob",
                     blob_key("trj_a1", MEDIA_SHA), "race_asset", 3),
                    ("pld_asset", None, len(PRIVATE), 0, "image/png", "identity", "asset", "assets/a/race.png",
                     "race_asset", 3))
        for payload_id, sha, size, stored, media_type, encoding, kind, key, asset_id, first_seq in payloads:
            db.add(TrajectoryPayload(payload_id=payload_id, trajectory_id="trj_a1", dedupe_key=payload_id.ljust(64, "0"),
                                     sha256=sha, size_bytes=size, stored_bytes=stored, media_type=media_type,
                                     encoding=encoding, storage_kind=kind, storage_key=key, source_asset_id=asset_id,
                                     first_seq=first_seq, created_at=stamp))
        db.add(TrajectoryCheckpoint(trajectory_id="trj_a1", through_seq=2, projector_version=1,
                                    state={"projector_version": 1, "through_seq": "2", "records": {},
                                           "unsupported_events": [], "coverage_start": iso(stamp)},
                                    digest="d" * 64, created_at=stamp))
        db.add(TrajectoryExport(id="exp_ready", trajectory_id="trj_a1", viewer_id="admin", through_seq=3,
                                status="completed", storage_key=export_key("exp_ready", archive_sha),
                                sha256=archive_sha, created_at=stamp, updated_at=stamp, size_bytes=len(archive)))
        db.add(TrajectoryExport(id="exp_pending", trajectory_id="trj_a1", viewer_id="admin", through_seq=3,
                                status="pending", created_at=stamp, updated_at=stamp))


# -- Business side and authentication --

class Backend:
    """The business backend's internal router over ASGI; ``down`` answers every call with 502."""

    def __init__(self):
        import api.internal as internal
        self.app = FastAPI()
        self.app.include_router(internal.router)
        self.paths: list[str] = []
        self.down = False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.paths.append(scope["path"])
            if self.down:
                await Response(status_code=502)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def client(self, **kwargs) -> trajectory_auth.HttpBackend:
        return trajectory_auth.HttpBackend("http://backend", INTERNAL_TOKEN,
                                           transport=httpx.ASGITransport(app=self), **kwargs)

    def viewer_calls(self) -> int:
        return self.paths.count(trajectory_auth.VIEWER_PATH)


@pytest.fixture
async def business_db(tmp_path, monkeypatch):
    """A business database holding the users (and audit_logs) the internal endpoints use."""
    import db.models  # noqa: F401
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'business.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    stamp = now()
    async with factory.begin() as db:
        for user_id, role, active, deleted in (("admin", "admin", True, False), ("a", "user", True, False),
                                               ("b", "user", True, False), ("default", "admin", True, False),
                                               ("retired", "admin", True, True), ("suspended", "admin", False, False)):
            db.add(User(id=user_id, username=user_id, role=role, is_active=active, is_deleted=deleted,
                        created_at=stamp, updated_at=stamp))
    yield factory
    await engine.dispose()


@pytest.fixture
def admin_env(monkeypatch):
    for key in ("TRAJECTORY_ADMIN_ENABLED", "TRAJECTORY_RECORDING_ENABLED"):
        monkeypatch.setenv(key, "true")
    for key in ("TRAJECTORY_ADMIN_USER_IDS", "TRAJECTORY_RECORD_USER_IDS", "TRAJECTORY_AUTH_CACHE_SECONDS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def auth_stores(monkeypatch):
    """JWT verification, the token blacklist and the ticket store, as the worker lifespan installs them."""
    cache = MemoryCache()
    monkeypatch.setattr(jwt_module, "_secret", SECRET)
    monkeypatch.setattr(middleware, "_cache", cache)
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(tickets, "_cache", cache)
    monkeypatch.setattr(trajectory_auth, "_backend", None)
    monkeypatch.setattr(trajectory_auth, "_facts", {})
    monkeypatch.setattr(trajectory_auth, "_audited", {})
    return cache


@pytest.fixture
def internal_backend(business_db, monkeypatch):
    import api.internal as internal
    monkeypatch.setattr(internal, "get_config", lambda: OpenBoxConfig(internal_api_token=INTERNAL_TOKEN))
    return Backend()


@pytest.fixture
def trace_url(tmp_path):
    path = tmp_path / "trace.db"
    engine = create_engine(f"sqlite:///{path}")
    TraceBase.metadata.create_all(engine)
    engine.dispose()
    return f"sqlite+aiosqlite:///{path}"


@pytest.fixture
async def trace_engine(trace_url):
    from trajectory.store.database import close_trace_engine, init_trace_engine
    engine = init_trace_engine(trace_url)
    yield engine
    await close_trace_engine()


class FakeServices:
    """WorkerServices as the app lifespan sees it."""

    def __init__(self, blob_store=None, *, writer: bool = True):
        self.blob_store = blob_store
        self.is_writer = writer
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


@dataclass
class Worker:
    app: FastAPI
    client: httpx.AsyncClient
    cache: MemoryCache
    blob: MemoryBlobStore
    services: FakeServices
    layer: ReadLayer
    backend: Backend
    http_backend: trajectory_auth.HttpBackend
    business: async_sessionmaker
    token: str
    spool: object = field(default=None)

    def socket(self, ticket: str | None):
        return socket(self.app, ticket)

    async def delivered_audit(self) -> list:
        """Deliver the outbox now and return the business audit_logs rows, oldest first."""
        from db.models.audit_log import AuditLog
        while await trajectory_auth.AuditDelivery(self.http_backend).deliver_once():
            pass
        async with self.business() as db:
            return list((await db.scalars(select(AuditLog).order_by(AuditLog.created_at, AuditLog.id))).all())


@pytest.fixture
async def worker(trace_url, internal_backend, business_db, auth_stores, admin_env, monkeypatch, tmp_path):
    from trajectory.worker.app import create_app
    blob = MemoryBlobStore()
    layer = ReadLayer(blob)
    layer.install(monkeypatch)
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(spool))
    services = FakeServices(blob)
    http_backend = internal_backend.client()
    app = create_app(database_url=trace_url, blob_store=blob, services_factory=lambda store: services,
                     backend=http_backend, cache=auth_stores)
    admin = token("admin")
    async with app.router.lifespan_context(app):
        await seed_trace(blob)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     headers={"Authorization": f"Bearer {admin}"}) as client:
            yield Worker(app=app, client=client, cache=auth_stores, blob=blob, services=services, layer=layer,
                         backend=internal_backend, http_backend=http_backend, business=business_db, token=admin,
                         spool=spool)
    await http_backend.close()


@asynccontextmanager
async def socket(app, ticket: str | None):
    """A raw ASGI WebSocket session: ``send(value)`` and ``receive()`` (JSON frames or ASGI messages)."""
    inbound, outbound = asyncio.Queue(), asyncio.Queue()
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "scheme": "ws", "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
             "path": "/ws/admin/trajectories", "raw_path": b"/ws/admin/trajectories",
             "query_string": b"" if ticket is None else f"ticket={ticket}".encode(), "root_path": "",
             "headers": [], "subprotocols": [], "state": {}}
    task = asyncio.create_task(app(scope, inbound.get, outbound.put))
    await inbound.put({"type": "websocket.connect"})

    async def receive(timeout: float = 3):
        value = await asyncio.wait_for(outbound.get(), timeout=timeout)
        return json.loads(value["text"]) if value["type"] == "websocket.send" else value

    async def send(value):
        await inbound.put({"type": "websocket.receive", "text": value if isinstance(value, str) else json.dumps(value)})

    try:
        yield send, receive
    finally:
        await inbound.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
