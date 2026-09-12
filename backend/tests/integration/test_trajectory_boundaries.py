"""Real application routes and producer transactions with external services isolated."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

import auth.middleware as middleware
import auth.ticket as tickets
from auth.jwt import create_access_token, decode_access_token, init_auth
from cache.memory_cache import MemoryCache
from db.models.file_asset import FileAsset
from db.models.question import SessionExecution
from db.models.session import Session
from db.models.trajectory import SessionTrajectory, TrajectoryEvent, TrajectoryPayload
from db.models.user import User
from db.models.video_job import VideoJob
from tests.integration.test_trajectory_storage import tracedb
from trajectory import TraceContext, bind, record
from trajectory.types import now


@pytest.fixture
async def app_client(tracedb, monkeypatch):
    from core.config import OpenBoxConfig
    from main import create_app

    cache = MemoryCache()
    monkeypatch.setattr(middleware, "_cache", cache)
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(tickets, "_cache", cache)
    init_auth("isolated-trajectory-boundary-tests-key", 15, 7)
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(
        jwt_secret="isolated-trajectory-boundary-tests-key"))
    # ASGI transport runs the complete route assembly without starting cron,
    # sandbox reconciliation, or network providers in the server lifespan.
    app = create_app()
    token = create_access_token("admin", "admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://testserver",
                                headers={"Authorization": f"Bearer {token}"}) as client:
        yield app, client, cache, token


@asynccontextmanager
async def socket(app, ticket):
    inbound, outbound = asyncio.Queue(), asyncio.Queue()
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "scheme": "ws", "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
             "path": "/ws/admin/trajectories", "raw_path": b"/ws/admin/trajectories",
             "query_string": f"ticket={ticket}".encode(), "root_path": "",
             "headers": [], "subprotocols": [], "state": {}}
    task = asyncio.create_task(app(scope, inbound.get, outbound.put))
    await inbound.put({"type": "websocket.connect"})

    async def receive():
        value = await asyncio.wait_for(outbound.get(), timeout=3)
        return json.loads(value["text"]) if value["type"] == "websocket.send" else value

    async def send(value):
        await inbound.put({"type": "websocket.receive", "text": json.dumps(value)})

    try:
        yield send, receive
    finally:
        await inbound.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_scoped_tickets_are_atomic_and_cannot_authenticate_execute_socket(app_client):
    _, client, _, _ = app_client
    response = await client.post("/api/admin/trajectories/ticket")
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert await tickets.consume_ticket(ticket) is None
    claims = await asyncio.gather(*(tickets.consume_ticket(ticket, audience="admin_trajectories")
                                    for _ in range(20)))
    assert sum(value is not None for value in claims) == 1
    identity = next(value for value in claims if value)
    assert identity["user_id"] == "admin" and identity["auth_jti"]
    assert identity["auth_expires_at"] > time.time()
    normal = create_access_token("a", "admin")  # A stale/forged role claim is insufficient.
    denied = await client.post("/api/admin/trajectories/ticket",
                               headers={"Authorization": f"Bearer {normal}"})
    assert denied.status_code == 403


async def test_readonly_socket_has_no_execution_side_effect_and_rechecks_live_role(
        app_client, tracedb, monkeypatch):
    app, client, _, _ = app_client
    factory, _ = tracedb
    context = TraceContext("a", "session_a_1")
    await record("input.accepted", {"text": "new monitored input"}, context=context)
    execute = AsyncMock(side_effect=AssertionError("Admin monitoring must never execute"))
    monkeypatch.setattr("api.ws._ensure_user_container", execute)
    monkeypatch.setattr("api.ws._handle_client_message", execute)
    ticket = (await client.post("/api/admin/trajectories/ticket")).json()["ticket"]
    async with socket(app, ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1"})
        subscribed = await receive()
        assert subscribed["type"] == "subscribed"
        assert subscribed["data"]["owner_user_id"] == "a"
        for action in ("permission.reply", "question.reply", "session.prompt", "session.cancel"):
            await send({"type": action, "session_id": "session_a_1", "answers": [["yes"]]})
            assert (await receive())["data"]["code"] == "READ_ONLY"
        await record("input.accepted", {"text": "visible only through an authorized read"}, context=context)
        watermark = await receive()
        assert watermark["type"] == "trajectory.available"
        assert set(watermark["data"]) == {"user_id", "owner_user_id", "session_id", "trajectory_id", "committed_seq"}
        async with factory.begin() as db:
            assert (await db.get(Session, "session_a_1")).status == "idle"
            assert await db.get(SessionExecution, "session_a_1") is None
            admin = await db.get(User, "admin")
            admin.role = "user"
        assert (await receive())["code"] == 4403
    execute.assert_not_called()
    assert (await client.get("/api/admin/trajectories/sessions")).status_code == 403


async def test_idle_subscription_stops_after_token_revocation(app_client):
    app, client, cache, token = app_client
    ticket = (await client.post("/api/admin/trajectories/ticket")).json()["ticket"]
    async with socket(app, ticket) as (_, receive):
        assert (await receive())["type"] == "websocket.accept"
        await cache.set(f"jwt_bl:{decode_access_token(token)['jti']}", True, ttl=60)
        assert (await receive())["code"] == 4401
    assert (await client.get("/api/admin/trajectories/sessions")).status_code == 401


async def test_ordinary_websocket_never_receives_owned_trace_content(monkeypatch):
    from api import ws
    direct, broadcast = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ws.ws_manager, "send_to_user", direct)
    monkeypatch.setattr(ws.ws_manager, "broadcast", broadcast)
    await ws._on_bus_event({"type": "trajectory.available", "data": {"userId": "a", "raw": "secret"}})
    await ws._on_bus_event({"type": "trajectory.event", "data": {"userId": "a", "raw": "secret"}})
    direct.assert_not_called()
    broadcast.assert_not_called()
    await ws._on_bus_event({"type": "part.updated", "data": {"userId": "a", "text": "chat"}})
    direct.assert_awaited_once()


async def test_late_job_callbacks_use_submission_identity_and_cannot_revive_deleted_trace(tracedb):
    from trajectory.jobs import CONTEXT_KEY, record_job_in_tx
    from trajectory.lifecycle import delete_trajectory_in_tx
    from trajectory.repository import state_at

    factory, _ = tracedb
    original = TraceContext("a", "session_a_1", turn_id="original_turn", run_id="original_run",
                            call_id="original_call", agent_id="original_agent")
    await record("run.started", {"status": "running"}, context=original)
    await record("run.interrupted", {"status": "unknown", "reason": "lease_expired"}, context=original)
    async with factory.begin() as db:
        job = VideoJob(id="video_job", user_id="a", session_id="session_a_1", kind="render",
                       idempotency_key="fixture", status="queued", request_data={}, result_data={},
                       created_at=now(), updated_at=now())
        db.add(job)
        with bind(original):
            await record_job_in_tx(db, job, submitted=True)
        assert job.request_data[CONTEXT_KEY]["run_id"] == "original_run"
    unrelated = TraceContext("a", "session_a_2", turn_id="new_turn", run_id="new_run")
    with bind(unrelated):
        for _ in range(2):
            async with factory.begin() as db:
                job = await db.get(VideoJob, "video_job")
                job.status, job.result_data = "completed", {"output": "late completion"}
                await record_job_in_tx(db, job)
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.type.in_(["job.finished", "operation.late_result"])))).all()
        assert len(events) == 2
        assert all(event.session_id == "session_a_1" and event.context["run_id"] == "original_run" for event in events)
        trajectory = await db.scalar(select(SessionTrajectory))
        state = await state_at(db, trajectory)
        assert state["records"]["run:original_run"]["status"] == "unknown"
    async with factory.begin() as db:
        await delete_trajectory_in_tx(db, "session_a_1", "a")
        (await db.get(Session, "session_a_1")).is_deleted = True
    async with factory.begin() as db:
        await record_job_in_tx(db, await db.get(VideoJob, "video_job"))
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(TrajectoryEvent)) == 0
        assert await db.scalar(select(func.count()).select_from(SessionTrajectory)) == 1


async def test_retained_attachment_reuse_and_explicit_delete(tracedb):
    from trajectory.artifacts import capture_asset_in_tx, revoke_asset_in_tx
    from trajectory.payload import read_payload

    factory, _ = tracedb
    context = TraceContext("a", "session_a_1", turn_id="first_turn", call_id="first_call")
    async with factory.begin() as db:
        asset = FileAsset(id="asset_immutable", user_id="a", workspace_id="ws_a", session_id="session_a_1",
                          name="snapshot.png", oss_key="assets/a/immutable", mime="image/png", size=3,
                          status="ready", is_deleted=False, created_at=now())
        db.add(asset)
        first = await capture_asset_in_tx(db, context, asset, content=b"one")
    async with factory.begin() as db:
        asset = await db.get(FileAsset, "asset_immutable")
        again = await capture_asset_in_tx(db, context.derive(turn_id="second_turn", call_id="second_call"),
                                         asset, content=b"one")
        assert again["payload_id"] == first["payload_id"]
    async with factory() as db:
        row = await db.get(TrajectoryPayload, first["payload_id"])
        _, content = await read_payload(db, row.trajectory_id, row.payload_id, through_seq=100)
        assert content == b"one"
    async with factory.begin() as db:
        asset = await db.get(FileAsset, "asset_immutable")
        asset.is_deleted, asset.deleted_at = True, now()
        await revoke_asset_in_tx(db, asset.id)
    async with factory() as db:
        with pytest.raises(FileNotFoundError):
            await read_payload(db, row.trajectory_id, row.payload_id, through_seq=100)


async def test_permission_commit_survives_lost_wakeup_and_keeps_original_call(tracedb, monkeypatch):
    from permission import permission as permissions
    factory, _ = tracedb
    monkeypatch.setattr(permissions, "_pending", {})
    monkeypatch.setattr(permissions, "_approved", {})
    monkeypatch.setattr(permissions, "_get_redis_client", lambda: None)
    monkeypatch.setattr(permissions, "_push_waiting", AsyncMock())
    monkeypatch.setattr(permissions, "_push_resolved", AsyncMock())
    asked = asyncio.Event()
    monkeypatch.setattr("permission.permission.bus.publish",
                        lambda event, _data: asked.set() if event == permissions.PERMISSION_ASKED else None)
    original = TraceContext("a", "session_a_1", turn_id="approval_turn", run_id="approval_run",
                            call_id="approval_call")
    with bind(original):
        waiting = asyncio.create_task(permissions.ask("session_a_1", "bash", ["deploy"],
                                                       input_data={"command": "deploy --dry-run"}, user_id="a"))
    await asyncio.wait_for(asked.wait(), timeout=2)
    pending = next(iter(permissions._pending.values()))
    request_id = pending.request.id
    with pytest.raises(PermissionError):
        await permissions.reply(request_id, "once", user_id="b")
    assert not waiting.done()
    # Another worker commits a corrected rejection; the original waiter has
    # not received any Redis/local notification yet.
    with bind(TraceContext("a", "session_a_2", run_id="unrelated")):
        await permissions._trace_permission(pending.request, "permission.resolved", {
            "decision": "reject", "message": "use a preview first", "status": "denied",
        }, saved=pending.trace_context)
    assert await permissions._read_recorded_reply(request_id, pending)
    assert pending.result == "reject" and pending.error_message == "use a preview first"
    pending.event.set()
    with pytest.raises(permissions.PermissionCorrectedError):
        await waiting
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).where(
            TrajectoryEvent.type.like("permission.%")).order_by(TrajectoryEvent.seq))).all()
        assert [event.type for event in events] == ["permission.requested", "permission.resolved"]
        assert all(event.session_id == "session_a_1" and event.call_id == "approval_call" for event in events)


async def test_old_pending_question_adopts_baseline_and_resumes_without_tool_execution(tracedb, monkeypatch):
    from db.base import get_db_session
    from db.models.message import Message
    from db.models.part import Part
    from question import question as questions, runtime
    from question.continuation import apply_answers

    factory, _ = tracedb
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    async with get_db_session() as db:
        db.add(Message(id="question_message", session_id="session_a_1", user_id="a", role="assistant", created_at=now()))
        await db.flush()
        db.add(Part(id="question_part", session_id="session_a_1", message_id="question_message", user_id="a",
                    type="tool", data={"id": "question_part", "type": "tool", "tool": "question", "status": "running"},
                    created_at=now()))
    with pytest.raises(questions.QuestionSuspended) as suspended:
        await questions.ask("session_a_1", [questions.Question(question="Continue?", options=[questions.QuestionOption(label="Yes")])],
                            {"messageID": "question_message", "callID": "question_part"}, "a")
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    await questions.reply(suspended.value.request_id, [["Yes"]], "a")
    generation = await apply_answers("session_a_1", "a")
    assert generation == 0
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).order_by(TrajectoryEvent.seq))).all()
        types = [event.type for event in events]
        assert "baseline.captured" in types and "question.resolved" in types
        assert "question.asked" not in types and "tool.started" not in types
        assert types.count("tool.finished") == 1
        baseline = next(event.data for event in events if event.type == "baseline.captured")
        assert baseline["preexisting"] and baseline["pending_questions"][0]["status"] == "pending"
        assert (await db.get(Part, "question_part")).data["status"] == "completed"
    ticket = await runtime.start_run("session_a_1", "a", expected_generation=generation)
    assert ticket
    await runtime.finish_run(ticket, completed=True)


async def test_file_tools_retain_actual_edits_and_patch_does_not_repeat_last_operation(tracedb, monkeypatch):
    from types import SimpleNamespace
    from tool.tool import ToolContext
    from tool import edit, write, apply_patch, multiedit
    factory, _ = tracedb
    monkeypatch.setattr("core.config.get_config", lambda: SimpleNamespace(auto_format=False, lsp_diagnostics=False))

    class Sandbox:
        def __init__(self):
            self.files = {"/workspace/file.txt": "before\n"}
            self.writes = []
        async def read_file(self, path, **_kwargs):
            return "\n".join(f"{index + 1}\t{line}" for index, line in enumerate(self.files[path].split("\n")))
        async def write_file(self, path, content):
            self.files[path] = content
            self.writes.append((path, content))
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(exit_code=1, stdout="", stderr="not a real shell")

    sandbox = Sandbox()
    context = TraceContext("a", "session_a_1", turn_id="file_turn", call_id="file_call")
    ctx = ToolContext(session_id="session_a_1", user_id="a", workspace_id="ws_a", sandbox=sandbox, trace_context=context)
    await edit.execute(edit.EditArgs(file_path="/workspace/file.txt", old_string="before", new_string="after"), ctx)
    await multiedit.execute(multiedit.MultiEditArgs(file_path="/workspace/file.txt", edits=[
        multiedit.EditEntry(old_string="after", new_string="again")]), ctx)
    await write.execute(write.WriteArgs(file_path="/workspace/file.txt", content="written\n"), ctx)
    await apply_patch.execute(apply_patch.ApplyPatchArgs(patch="*** Begin Patch\n*** Add File: /workspace/new.txt\n+new\n*** End Patch"), ctx)
    assert len(sandbox.writes) == 4
    async with factory() as db:
        events = (await db.scalars(select(TrajectoryEvent).where(TrajectoryEvent.type == "artifact.recorded")
                                   .order_by(TrajectoryEvent.seq))).all()
        assert len(events) == 4
        first = events[0].data
        assert first["before"]["text"] == "before\n" and first["after"]["text"] == "after\n"
        assert "-before" in first["diff"] and "+after" in first["diff"]
        assert events[-1].data["after"]["text"] == "new"
