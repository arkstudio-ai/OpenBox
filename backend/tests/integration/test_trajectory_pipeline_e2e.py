"""End to end: business producers, the spool, the worker services and the admin API (SPEC §1, §8, §14).

Real producers write the spool: a chat run through the agent loop over a scripted provider (request
capture, streamed deltas, a tool call with output), an asset upload, the metadata sync and a session
deletion. The real WorkerServices ingest, project, checkpoint, archive, export and collect garbage on a
SQLite trace database, and on PostgreSQL when TRAJECTORY_TRACE_TEST_DATABASE_URL names a local disposable
openbox_trace_test_* database. Everything is read back through the worker app over ASGI, its socket
included. The last test runs ``python -m trajectory.worker`` as a process.
"""
import asyncio
import hashlib
import io
import json
import os
import signal
import socket as sockets
import subprocess
import sys
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import bus.bus as bus_module
import db.base as database
from db.models.file_asset import FileAsset
from db.models.user import User
from question import runtime
from session.session import create_user_message, delete_session
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401
from tests.unit.test_worker_app_harness import INTERNAL_TOKEN, Backend, auth_stores, socket, token  # noqa: F401
from tests.unit.test_worker_ingest import SpoolWriter, event as spool_event
from tests.unit.trajectory_producer_support import recording_spool  # noqa: F401
from tool.tool import ToolResult, define_tool
from trajectory import TraceContext, emit
from trajectory.payload import reset_blob_cache
from trajectory.storage import MemoryBlobStore
from trajectory.store.database import TraceBase, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryEvent, TrajectoryExport,
    TrajectoryPayload, TrajectoryRecord, TrajectorySegment)
from trajectory.worker.metrics import COUNTERS, GAUGES, get_metrics, reset_metrics_for_tests
from trajectory.worker.services import WorkerServices
from trajectory.worker.settings import WorkerSettings

BACKEND = Path(__file__).resolve().parents[2]
PREFIX = "/api/admin/trajectories"
SESSION = f"{PREFIX}/sessions/s1"
WATERMARK = {"user_id", "owner_user_id", "session_id", "trajectory_id", "committed_seq"}
PHOTO = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
PHOTO_KEY = "assets/u1/asset-e2e/photo.png"
TRACE_PG_URL = os.environ.get("TRAJECTORY_TRACE_TEST_DATABASE_URL", "")
#: The bus as imported, before business fixtures capture publish(): trajectory.available must reach the socket.
REAL_PUBLISH = bus_module.publish


class _Label(BaseModel):
    label: str


def _chunk(*, content=None, name=None, arguments=None):
    calls = [] if arguments is None else [SimpleNamespace(
        index=0, id="provider-call", function=SimpleNamespace(name=name, arguments=arguments))]
    return SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=None, delta=SimpleNamespace(
        content=content, reasoning_content=None, tool_calls=calls))], usage=None)


@pytest.fixture
def scripted_provider(loop_harness, monkeypatch):  # noqa: F811
    """The real stream_llm, adapter and RequestCapture over a scripted LiteLLM stream: a tool call, then text."""
    import litellm
    from agent import llm
    from billing.service import UsageMeter
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        answered = any(isinstance(message, dict) and message.get("role") == "tool"
                       for message in kwargs.get("messages") or [])

        async def stream():
            if kwargs.get("tools") and not answered:
                yield _chunk(name=kwargs["tools"][0]["function"]["name"], arguments='{"label": "inventory"}')
            else:
                for piece in ("The inventory ", "is complete: ", "7 crates counted."):
                    yield _chunk(content=piece)
        return stream()
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_needs_responses_api", lambda _model: False)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr(llm, "_get_variant_kwargs", lambda *_args: {})
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda _model: 100)
    monkeypatch.setattr(UsageMeter, "start", AsyncMock(return_value=None))
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=None))
    return calls


async def _recreate_trace_database(url: str, *, create: bool = True) -> None:
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1"} or not (parsed.database or "").startswith("openbox_trace_test_"):
        raise ValueError("The PostgreSQL pipeline test requires a local disposable openbox_trace_test_* database")
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            if create:
                await connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        await admin.dispose()


def _upgrade_trace_database() -> None:
    from alembic import command
    from alembic.config import Config
    config = Config(str(BACKEND / "alembic_trajectory.ini"))
    config.set_main_option("script_location", str(BACKEND / "trajectory" / "store" / "migrations"))
    command.upgrade(config, "head")


@pytest.fixture(params=["sqlite", "postgresql"])
async def trace_url(request, tmp_path, monkeypatch):
    """A trace database with its schema: SQLite through create_all (as embedded mode), PostgreSQL through alembic."""
    if request.param == "sqlite":
        path = tmp_path / "trace.db"
        engine = create_engine(f"sqlite:///{path}")
        TraceBase.metadata.create_all(engine)
        engine.dispose()
        yield f"sqlite+aiosqlite:///{path}"
        return
    if not TRACE_PG_URL:
        pytest.skip("TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")
    parsed = make_url(TRACE_PG_URL)
    url = parsed.set(database=f"{parsed.database}_pipeline").render_as_string(hide_password=False)
    await _recreate_trace_database(url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", url)
    # env.py runs asyncio.run(), which needs a thread without a running loop.
    await asyncio.to_thread(_upgrade_trace_database)
    yield url
    await _recreate_trace_database(url, create=False)


class _Driven:
    """The real WorkerServices behind the app lifespan, without their loops: the test drains explicitly."""

    def __init__(self, services: WorkerServices):
        self.services = services

    @property
    def is_writer(self) -> bool:
        return self.services.is_writer

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.services.stop()


@pytest.fixture
async def pipeline(state, recording_spool, loop_harness, scripted_provider, auth_stores, trace_url,  # noqa: F811
                   monkeypatch):
    import api.internal as internal
    from core.config import OpenBoxConfig
    from trajectory.worker.app import create_app

    for key in ("TRAJECTORY_ADMIN_USER_IDS", "TRAJECTORY_RECORD_USER_IDS", "TRAJECTORY_AUTH_CACHE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRAJECTORY_ADMIN_ENABLED", "true")

    def publish(event_type, data):
        # The business fixture keeps events instead of publishing them; watermarks go to the worker's socket.
        if event_type == "trajectory.available":
            REAL_PUBLISH(event_type, data)
        else:
            state.append((event_type, data))
    monkeypatch.setattr("bus.bus.publish", publish)
    monkeypatch.setattr(internal, "get_config", lambda: OpenBoxConfig(internal_api_token=INTERNAL_TOKEN))
    async with database.get_db_session() as db:
        db.add(User(id="admin", username="admin", role="admin", is_active=True, is_deleted=False,
                    created_at=runtime.now(), updated_at=runtime.now()))
    reset_metrics_for_tests()
    reset_blob_cache()
    store = MemoryBlobStore()
    assets = {PHOTO_KEY: PHOTO}

    async def read_asset(key: str) -> bytes:
        if key not in assets:
            raise FileNotFoundError(key)
        return assets[key]

    # Low thresholds: full segments, checkpoints and $ref record values from one short conversation.
    settings = replace(WorkerSettings.from_env(), spool_dir=recording_spool.root, database_url=trace_url,
                       segment_events=4, segment_idle_seconds=86400, checkpoint_interval=6, record_inline_bytes=256)
    driven: list[_Driven] = []

    def services_factory(blob_store):
        driven.append(_Driven(WorkerServices(settings, blob_store=blob_store)))
        return driven[-1]

    backend = Backend().client()
    app = create_app(database_url=trace_url, blob_store=store, services_factory=services_factory, backend=backend,
                     cache=auth_stores, asset_reader=read_asset)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker",
                                         headers={"Authorization": f"Bearer {token('admin')}"}) as client:
                yield SimpleNamespace(app=app, client=client, services=driven[0].services, store=store,
                                      harness=loop_harness)
    finally:
        await backend.close()


def _refs(value):
    """Every ``$ref`` envelope inside a JSON value."""
    if isinstance(value, dict):
        if len(value) == 1 and isinstance(value.get("$ref"), dict):
            yield value["$ref"]
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


async def _trajectory(session_id: str = "s1") -> SessionTrajectory:
    async with trace_session() as db:
        return await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == session_id))


async def _count(model, trajectory_id: str) -> int:
    async with trace_session() as db:
        return await db.scalar(select(func.count()).select_from(model).where(model.trajectory_id == trajectory_id))


async def test_business_facts_reach_the_admin_api_and_socket_until_the_session_is_deleted(pipeline, monkeypatch):
    from api import assets as assets_api
    from trajectory.meta_sync import MetaSync
    client, services, store = pipeline.client, pipeline.services, pipeline.store

    async def count(arguments, ctx):
        await ctx.update_output("counting")
        return ToolResult(output=f"counted {arguments.label}: 7 crates")
    pipeline.harness.tools["count"] = define_tool("count", description="test only", parameters=_Label,
                                                  execute=count, sandbox_required=False)

    # 1. Producers: a chat run with a tool call, an asset upload and the metadata snapshot.
    await create_user_message("s1", "Count the crates in the inventory", user_id="u1")
    assert await asyncio.wait_for(pipeline.harness.loop.run_loop("s1", user_id="u1"), timeout=30) is not None
    oss = SimpleNamespace(head=AsyncMock(return_value={"size": len(PHOTO)}), delete=AsyncMock(),
                          presign_get=lambda key, **_kwargs: f"https://oss.example/{key}")
    monkeypatch.setattr(assets_api, "_oss_or_503", lambda: oss)
    async with database.get_db_session() as db:
        db.add(FileAsset(id="asset-e2e", user_id="u1", workspace_id="w1", session_id="s1", name="photo.png",
                         oss_key=PHOTO_KEY, mime="image/png", size=0, status="pending", created_at=runtime.now()))
    await assets_api.complete_asset("asset-e2e", current_user={"user_id": "u1", "workspace_id": "w1"},
                                    _workspace={})
    sync = MetaSync()
    while await sync.cycle():
        pass

    # 2. The worker: ingest, projection, checkpoints and archival of full segments; later events stay hot.
    drained = await services.drain(timeout=60, include_archive=True)
    assert drained["writer"] and not drained["timed_out"] and drained["archived"] > 0, drained
    trajectory = await _trajectory()
    assert trajectory.projected_seq == trajectory.committed_seq and trajectory.archived_seq >= 4
    archived = trajectory.archived_seq
    await create_user_message("s1", "Thanks, write that down", user_id="u1")
    await services.drain(timeout=60)
    trajectory = await _trajectory()
    head, tid = trajectory.committed_seq, trajectory.id
    assert head > archived and trajectory.projected_seq == head and trajectory.archived_seq == archived
    async with trace_session() as db:
        segments = [(row.from_seq, row.to_seq) for row in (await db.scalars(select(TrajectorySegment).where(
            TrajectorySegment.trajectory_id == tid).order_by(TrajectorySegment.from_seq))).all()]
        hot = list((await db.scalars(select(TrajectoryEvent.seq).where(TrajectoryEvent.trajectory_id == tid)
                                     .order_by(TrajectoryEvent.seq))).all())
    assert segments[0][0] == 1 and segments[-1][1] == archived
    assert all(later[0] == earlier[1] + 1 for earlier, later in zip(segments, segments[1:]))
    assert hot == list(range(archived + 1, head + 1))
    assert await _count(TrajectoryCheckpoint, tid) >= 1

    # 3. Reads through the worker app.
    listed = await client.get(f"{PREFIX}/sessions")
    assert listed.status_code == 200, listed.text
    assert "s1" in {item["session_id"] for item in listed.json()["items"]}
    header = await client.get(SESSION)
    assert header.status_code == 200, header.text
    assert header.json()["capabilities"]["refs"] is True and header.json()["through_seq"] == str(head)

    page = (await client.get(f"{SESSION}/events", params={"after_seq": "0", "limit": 2000})).json()
    events = page["events"]
    assert [int(item["seq"]) for item in events] == list(range(1, head + 1)) and page["committed_seq"] == str(head)
    assert not list(_refs(events))  # expanded, whether the event came from a segment or a hot row
    types = [item["type"] for item in events]
    assert types[0] == "trajectory.started"
    assert {"request.prepared", "request.started", "request.delta", "request.finished", "tool.requested",
            "tool.output", "tool.finished", "artifact.recorded", "run.finished"} <= set(types)
    assert any(item["data"].get("stage") == "executor_result" and item["data"].get("final") is True
               for item in events if item["type"] == "tool.output")

    records = (await client.get(f"{SESSION}/records", params={"limit": 500})).json()["items"]
    assert {"request", "tool"} <= {item["kind"] for item in records}
    referenced = None
    for item in records:
        path = f"{SESSION}/records/{quote(item['record_id'], safe='')}"
        detail = await client.get(path, params={"expand": "refs"})
        assert detail.status_code == 200, detail.text
        found = list(_refs(detail.json()["record"]))
        if found:
            referenced = (path, found[0])
            break
    assert referenced is not None, "record values above TRAJECTORY_RECORD_INLINE_BYTES are stored as $ref"
    path, ref = referenced
    full = (await client.get(path, params={"expand": "full"})).json()
    assert full["through_seq"] == str(head) and full["record"]["events"] and not list(_refs(full))
    blob = await client.get(f"{SESSION}/blobs/{ref['sha256']}", params={"through_seq": str(head)})
    assert blob.status_code == 200 and blob.headers["content-type"] == "application/json"
    assert hashlib.sha256(blob.content).hexdigest() == ref["sha256"]

    async with trace_session() as db:
        photo = await db.scalar(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == tid,
                                                                TrajectoryPayload.source_asset_id == "asset-e2e"))
    assert photo is not None and (photo.storage_kind, photo.storage_key) == ("asset", PHOTO_KEY)
    meta = await client.get(f"{SESSION}/payloads/{photo.payload_id}", params={"meta": 1})
    assert meta.status_code == 200, meta.text
    assert set(meta.json()) == {"payload_id", "availability", "media_type", "size_bytes", "sha256"}
    assert (meta.json()["payload_id"], meta.json()["availability"], meta.json()["media_type"]) == (
        photo.payload_id, "available", "image/png")
    download = await client.get(f"{SESSION}/payloads/{photo.payload_id}")
    assert download.status_code == 200 and download.content == PHOTO

    found = (await client.get(f"{SESSION}/search", params={"q": "crates"})).json()
    assert found["items"] and found["through_seq"] == str(head)
    checkpoint = (await client.get(f"{SESSION}/checkpoint", params={"at_seq": str(head)})).json()
    assert checkpoint["through_seq"] == str(head) and checkpoint["checkpoint"] is not None
    assert int(checkpoint["checkpoint"]["through_seq"]) <= head and checkpoint["checkpoint"]["state"]["records"]

    # 4. The socket: a subscription receives the watermark of a later commit.
    ticket = await client.post(f"{PREFIX}/ticket")
    assert ticket.status_code == 200, ticket.text
    async with socket(pipeline.app, ticket.json()["ticket"]) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "s1", "after_seq": str(head)})
        subscribed = await receive()
        assert subscribed["type"] == "subscribed" and set(subscribed["data"]) == WATERMARK
        await create_user_message("s1", "Please double-check the count", user_id="u1")
        await services.drain(timeout=60)
        hint = await receive(timeout=10)
        assert hint["type"] == "trajectory.available" and set(hint["data"]) == WATERMARK
        assert hint["data"]["session_id"] == "s1" and int(hint["data"]["committed_seq"]) > head
    head = (await _trajectory()).committed_seq

    # 5. An export, built by the worker's ExportService.
    created = await client.post(f"{SESSION}/export", json={})
    assert created.status_code == 202, created.text
    export_id = created.json()["export_id"]
    await services.drain(timeout=60)
    status = (await client.get(f"{SESSION}/exports/{export_id}")).json()
    assert status["status"] == "completed", status
    archive = await client.get(status["download_url"])
    assert archive.status_code == 200 and archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        exported = [json.loads(line) for line in bundle.read("events.jsonl").splitlines()]
        exported_photo = bundle.read(f"payloads/{photo.payload_id}")
    assert (manifest["format"], manifest["through_seq"], manifest["session_id"], manifest["trajectory_id"]) == (
        "openbox.session-trajectory", str(head), "s1", tid)
    assert [int(item["seq"]) for item in exported] == list(range(1, head + 1))
    assert exported_photo == PHOTO and not manifest["missing_payloads"]

    # 6. Deleting the session tombstones the trajectory: reads end, rows and objects go, late events are dropped.
    dropped = get_metrics().snapshot()["counters"]["deleted_drops"]
    assert await delete_session("s1", user_id="u1")
    deleted = await services.drain(timeout=60, include_archive=True)
    assert tid in deleted["deleted_trajectories"], deleted
    for suffix in ("", "/events", "/records", "/checkpoint", f"/payloads/{photo.payload_id}",
                   f"/blobs/{ref['sha256']}", f"/exports/{export_id}/download"):
        response = await client.get(f"{SESSION}{suffix}")
        assert response.status_code in {404, 410}, (suffix, response.status_code, response.text)
    trajectory = await _trajectory()
    assert trajectory.deleted_at is not None and trajectory.recording_status == "deleted"
    for model in (TrajectoryEvent, TrajectoryRecord, TrajectorySegment, TrajectoryCheckpoint):
        assert await _count(model, tid) == 0, model.__tablename__
    async with trace_session() as db:
        assert set((await db.scalars(select(TrajectoryPayload.availability).where(
            TrajectoryPayload.trajectory_id == tid))).all()) == {"deleted"}
        assert (await db.get(TrajectoryExport, export_id)).status == "deleted"
    assert not [key for key in store.objects if key.startswith(f"trajectories/{tid}/")]
    assert not [key for key in store.objects if key.startswith(f"trajectories/_exports/{export_id}/")]

    assert emit("input.accepted", {"text": "after the deletion"}, context=TraceContext("u1", "s1", workspace_id="w1"))
    await services.drain(timeout=30)
    assert (await _trajectory()).committed_seq == trajectory.committed_seq
    assert await _count(TrajectoryEvent, tid) == 0
    assert get_metrics().snapshot()["counters"]["deleted_drops"] > dropped


def _free_port() -> int:
    with sockets.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _poll(process, log_path: Path, request, ready, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            pytest.fail(f"the worker exited with {process.returncode}: {log_path.read_text(errors='replace')}")
        try:
            response = request()
            if ready(response):
                return response
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        if time.monotonic() > deadline:
            pytest.fail(f"the worker did not become ready: {log_path.read_text(errors='replace')}")
        time.sleep(0.2)


async def test_late_metadata_sees_a_summary_committing_at_the_same_time(trace_url):
    if not trace_url.startswith("postgresql"):
        pytest.skip("Concurrent row locks are a PostgreSQL behavior")
    from trajectory.store.database import close_trace_engine, init_trace_engine
    from trajectory.store.models import TrajectoryMetaSession, TrajectorySessionSummary
    from trajectory.worker.meta import MetaCache, apply_meta
    from trajectory.types import now, iso

    init_trace_engine(trace_url)
    at = now()
    syncing = None

    async def sync():
        async with trace_session() as db:
            await apply_meta(db, MetaCache(), "session.meta", {"session": {
                "id": "late", "user_id": "u", "updated_at": iso(at)}}, line_time=at, now=at)

    try:
        async with trace_session() as db:
            db.add(SessionTrajectory(id="late", session_id="late", user_id="u", workspace_id="w",
                                     started_at=at, updated_at=at, last_activity_at=at))
        async with trace_session() as db:
            await db.get(SessionTrajectory, "late", with_for_update=True)
            db.add(TrajectorySessionSummary(trajectory_id="late", session_id="late", user_id="u", workspace_id="w",
                last_activity_at=at, running_status="idle", recording_status="recording", applied_seq=1, statistics={}))
            await db.flush()
            # Projection has no metadata row to update yet. Its summary is still uncommitted.
            syncing = asyncio.create_task(sync())
            done, _ = await asyncio.wait({syncing}, timeout=0.1)
            assert not done
        await asyncio.wait_for(syncing, 5)
        async with trace_session() as db:
            row = await db.get(TrajectoryMetaSession, "late")
            assert row.projected_activity_at == at
    finally:
        if syncing is not None and not syncing.done():
            syncing.cancel()
            await asyncio.gather(syncing, return_exceptions=True)
        await close_trace_engine()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_the_worker_process_serves_health_and_metrics_and_stops_on_sigterm(tmp_path):
    database_path = tmp_path / "trace.db"
    engine = create_engine(f"sqlite:///{database_path}")
    TraceBase.metadata.create_all(engine)
    engine.dispose()
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(mode=0o700)
    (tmp_path / "blobs").mkdir()
    SpoolWriter(spool_dir).events(spool_event("input.accepted", session="ses_smoke", user="u_smoke"))
    port = _free_port()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TRAJECTORY_")}
    environment.update(
        TRAJECTORY_DATABASE_URL=f"sqlite+aiosqlite:///{database_path}", TRAJECTORY_SPOOL_DIR=str(spool_dir),
        TRAJECTORY_BLOB_PROVIDER="local", TRAJECTORY_BLOB_LOCAL_PATH=str(tmp_path / "blobs"),
        TRAJECTORY_WORKER_HOST="127.0.0.1", TRAJECTORY_WORKER_PORT=str(port), JWT_SECRET="", REDIS_URL="")
    log_path = tmp_path / "worker.log"
    with log_path.open("wb") as log_file:
        process = subprocess.Popen([sys.executable, "-m", "trajectory.worker"], cwd=BACKEND, env=environment,
                                   stdout=log_file, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        health = _poll(process, log_path, lambda: httpx.get(f"{base}/health", timeout=2, trust_env=False),
                       lambda response: response.status_code == 200 and response.json()["status"] == "ok")
        body = health.json()
        assert body == {"status": "ok", "writer": True, "db": True, "spool": True, "blob_store": True,
                        "version": body["version"]} and isinstance(body["version"], str)
        assert health.headers["cache-control"] == "no-store"
        # The writer loops ingest the spool file written before the process started.
        metrics = _poll(process, log_path, lambda: httpx.get(f"{base}/metrics", timeout=2, trust_env=False),
                        lambda response: response.json()["counters"]["ingest_events"] >= 1)
        snapshot = metrics.json()
        assert set(snapshot) == {"counters", "gauges", "uptime_seconds"}
        assert set(COUNTERS) <= set(snapshot["counters"]) and set(GAUGES) <= set(snapshot["gauges"])
        assert isinstance(snapshot["uptime_seconds"], (int, float)) and snapshot["uptime_seconds"] >= 0
        process.send_signal(signal.SIGTERM)
        # uvicorn shuts down gracefully, then re-raises the signal it caught, so the status reads "terminated".
        assert process.wait(timeout=30) in {0, -signal.SIGTERM}, log_path.read_text(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    output = log_path.read_text(errors="replace")
    assert "Traceback" not in output, output
    # A clean stop: the lifespan ran its shutdown (services, audit delivery, engine) to the end.
    assert output.index("Shutting down") < output.index("Trajectory worker stopped") < output.index(
        "Application shutdown complete"), output
