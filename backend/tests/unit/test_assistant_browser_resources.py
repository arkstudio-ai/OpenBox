"""SQL/JWT/HTTP handoff integration with the real finite remote journal.

Docker inspection and the Chromium pipe are the only external substitutes.
The separate Chromium suite supplies physical browser evidence; this suite
does not infer a browser sandbox from an empty SQL queue.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from api import assistant_browser as api
from assistant import browser_resources as service
from assistant.service import ensure_main_session
from auth import jwt, middleware
from cache.memory_cache import MemoryCache
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.browser_resource import BrowserResourceBinding, BrowserResourceSession
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from db.models.user import User
from sandbox import browser_resource_client as client_module
from sandbox.private_runtime import resolve_private_runtime
from tests.unit.test_private_runtime import assistant_database, private_world  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "container"))
from browser_resource import BrowserJournal, BrowserSupervisor, create_app  # noqa: E402
from browser_isolation import CHECKS  # noqa: E402


class FixturePipe:
    live = True
    fixture_no_sandbox = False  # Schema fixture only; physical proof lives in Chromium tests.
    isolation = "container_uid"
    uid = gid = 1100

    def __init__(self):
        self.calls, self.text = [], ""
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.delay, self.fail = False, False

    async def execute(self, kind, args):
        self.calls.append((kind, deepcopy(args)))
        self.entered.set()
        if self.delay:
            await self.release.wait()
        if self.fail:
            raise OSError("Fixture pipe lost its acknowledgement")
        if kind == "text":
            self.text += args["text"]
        if kind == "capture":
            return {"url": "http://127.0.0.1/test", "frame_id": "original-frame", "loader_id": "original-loader",
                "sha256": "d" * 64, "width": 1024, "height": 768, "png_base64": "fixture-image"}
        return {"delivered": True}


@pytest.fixture
async def browser_world(private_world, monkeypatch, tmp_path):
    w = private_world
    w.config.private_runtime.browser_image = "private-browser-fixture:local"
    w.config.private_runtime.browser_isolation = "container_uid"
    main = await ensure_main_session(user_id=w.owner, workspace_id=w.workspace)
    peer_main = await ensure_main_session(user_id=w.peer, workspace_id=w.workspace)
    route = await resolve_private_runtime(session_id=main.id, user_id=w.owner, workspace_id=w.workspace,
        kind="browser_profile")
    journal = BrowserJournal(tmp_path / "browser-journal", route.resource_id, w.workspace)
    pipe = FixturePipe()
    supervisor = BrowserSupervisor(journal, pipe)
    # Fabricated remote startup report for SQL/HTTP authority tests only.
    # test_browser_resource_chromium exercises the actual verify() boundary.
    supervisor.isolation = {"mode": "container_uid", "verification": "passed", "supervisor_uid": 0,
        "browser_uid": 1100, "browser_gid": 1100, "checks": {name: True for name in CHECKS}}
    app = create_app(supervisor, route.api_key, manage_lifespan=False)
    state = SimpleNamespace(w=w, main=main, peer_main=peer_main, route=route, journal=journal,
        pipe=pipe, supervisor=supervisor, requests=[], revoke_after_takeover=False, lose_takeover_response=False,
        first_status_unavailable=False)
    actual_client = httpx.AsyncClient
    remote_transport = httpx.ASGITransport(app=app)

    class RemoteTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert (request.url.host, request.url.port) == (route.host, route.port)
            state.requests.append(request.url.path)
            if request.url.path == "/v1/status" and state.first_status_unavailable:
                state.first_status_unavailable = False
                raise httpx.ConnectError("Fixture Chromium has not opened its original listener yet", request=request)
            response = await remote_transport.handle_async_request(request)
            if request.url.path == "/v1/control/takeover":
                if state.revoke_after_takeover:
                    async with get_db_session() as db:
                        (await db.get(User, w.owner)).is_active = False
                if state.lose_takeover_response:
                    state.lose_takeover_response = False
                    raise httpx.ReadError("Fixture response lost after grant", request=request)
            return response

    namespace = dict(vars(httpx))
    namespace["AsyncClient"] = lambda **args: actual_client(**{**args, "transport": RemoteTransport()})
    monkeypatch.setattr(client_module, "httpx", SimpleNamespace(**namespace))
    monkeypatch.setattr(jwt, "_secret", "browser-api-fixture-signing-key")
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(middleware, "_cache", MemoryCache())
    backend = FastAPI()
    backend.include_router(api.router)
    state.scope = dict(user_id=w.owner, workspace_id=w.workspace, main_id=main.id)
    state.headers = lambda user=None: {"Authorization": "Bearer " + jwt.create_access_token(user or w.owner, "user"),
        "X-Workspace-Id": w.workspace}
    async with actual_client(transport=httpx.ASGITransport(app=backend), base_url="http://backend.test") as client:
        state.api = client
        try:
            yield state
        finally:
            journal.release_lock()


async def ensure(w):
    response = await w.api.post("/api/assistant/browser-resources/ensure", headers=w.headers())
    assert response.status_code == 200, response.text
    w.resource_id = response.json()["resource_id"]
    return response.json()


async def command(w, action, epoch, key):
    return await w.api.post(f"/api/assistant/browser-resources/{w.resource_id}/control", headers=w.headers(),
        json={"action": action, "expected_epoch": epoch, "idempotency_key": key})


def operation(grant, key="op-1", kind="capture", args=None):
    return {"fence": grant["fence"], "human_token": grant["human_token"], "operation_id": key,
        "kind": kind, "args": args or {}}


async def test_initial_listener_delay_retries_only_the_original_supplied_browser(browser_world):
    from db.models.private_runtime import PrivateRuntimeBinding

    w = browser_world
    before = [call for call in w.w.daemon.calls if call[0].endswith(("create", "start"))]
    w.first_status_unavailable = True
    result = await ensure(w)
    assert result["fence"]["epoch"] == 1 and result["remote_available"]
    assert w.requests.count("/v1/status") >= 2 and not w.pipe.calls
    assert [call for call in w.w.daemon.calls if call[0].endswith(("create", "start"))] == before
    async with get_db_session() as db:
        runtimes = list((await db.scalars(select(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.actor_user_id == w.w.owner, PrivateRuntimeBinding.kind == "browser_profile"))).all())
        bindings = list((await db.scalars(select(BrowserResourceBinding).where(
            BrowserResourceBinding.actor_user_id == w.w.owner))).all())
        assert len(runtimes) == len(bindings) == 1
        assert runtimes[0].id == bindings[0].private_runtime_id == w.route.binding_id
        assert bindings[0].identity == w.journal.identity


async def test_http_takeover_human_input_giveback_and_old_token_are_real_durable_transitions(browser_world):
    w = browser_world
    initial = await ensure(w)
    assert initial["can_takeover"] and initial["fence"]["epoch"] == 1
    grant = await command(w, "takeover", 1, "takeover-one")
    assert grant.status_code == 200, grant.text
    grant = grant.json()
    assert grant["state"] == "applied" and grant["fence"]["epoch"] == 2
    path = f"/api/assistant/browser-resources/{w.resource_id}/operations"
    captured = await w.api.post(path, headers=w.headers(), json=operation(grant))
    assert captured.status_code == 200 and captured.json()["result"]["observation"]["eligible"]
    typed = await w.api.post(path, headers=w.headers(), json=operation(grant, "text-1", "text", {"text": "HUMAN_CANARY"}))
    assert typed.status_code == 200 and w.pipe.text == "HUMAN_CANARY"
    replay = await command(w, "takeover", 1, "takeover-one")
    assert replay.json() == grant
    returned = await command(w, "giveback", 2, "giveback-one")
    assert returned.status_code == 200, returned.text
    assert returned.json()["fence"]["epoch"] == 3 and returned.json()["fresh_observation_required"]
    before = len(w.pipe.calls)
    assert (await w.api.post(path, headers=w.headers(), json=operation(grant, "stale", "text", {"text": "NEVER"}))).status_code == 423
    assert len(w.pipe.calls) == before
    async with get_db_session() as db:
        rows = list((await db.scalars(select(AssistantCommand).where(AssistantCommand.target_id == w.resource_id))).all())
        assert len(rows) == 2 and all(row.state == "applied" for row in rows)
        assert all(grant["human_token"] not in str(row.receipt) + str(row.source_ref) for row in rows)
    assert w.supervisor.status()["fresh_observation_required"]


async def test_remote_inflight_blocks_grant_until_original_operation_receipt_completes(browser_world):
    w = browser_world
    await ensure(w)
    remote = client_module.BrowserResourceClient(f"http://{w.route.host}:{w.route.port}", w.route.api_key, identity=w.journal.identity)
    fence = w.supervisor.status()["control"]["fence"]
    first = await remote.operate(fence=fence, operation_id="before-close-frame", kind="capture")
    w.pipe.delay = True
    w.pipe.entered.clear()
    pending = asyncio.create_task(remote.operate(fence=fence, operation_id="inflight", kind="navigate",
        args={"url": "http://127.0.0.1/delayed"}, observation_id=first["receipt"]["result"]["observation"]["observation_id"]))
    await asyncio.wait_for(w.pipe.entered.wait(), 2)
    try:
        result = await command(w, "takeover", 1, "takeover-drain")
        assert result.status_code == 200 and result.json()["state"] == "draining"
        assert "human_token" not in result.json()
        assert w.supervisor.status()["control"]["admission"] == "closed"
    finally:
        w.pipe.release.set()
        await pending
    finished = await command(w, "takeover", 1, "takeover-drain")
    assert finished.status_code == 200 and finished.json()["fence"]["epoch"] == 2


async def test_lost_grant_response_reopens_sql_and_recovers_exact_command_without_second_epoch(browser_world):
    w = browser_world
    await ensure(w)
    command_id = await service.accept_control(**w.scope, resource_id=w.resource_id, action="takeover",
        expected_epoch=1, idempotency_key="lost-grant")
    w.lose_takeover_response = True
    with pytest.raises(httpx.ReadError):
        await service.dispatch_control(command_id)
    assert w.supervisor.status()["control"]["fence"]["epoch"] == 2
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    recovered = await service.dispatch_control(command_id)
    assert recovered["state"] == "applied" and recovered["fence"]["epoch"] == 2
    assert w.supervisor.status()["control"]["fence"]["epoch"] == 2
    with w.journal.transaction() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 2


async def test_lost_grant_that_expires_recovers_only_closed_history_then_explicit_giveback(browser_world, monkeypatch):
    import time
    import browser_resource as remote_module

    w = browser_world
    await ensure(w)
    # Advance the remote clock instead of sleeping through the real 120s
    # lease. Its immutable grant expires before the current SQL clock; no
    # receipt, command, authority row or control state is patched by the test.
    clock = [time.time() - 125]
    monkeypatch.setattr(remote_module, "time", SimpleNamespace(time=lambda: clock[0]))
    w.lose_takeover_response = True
    lost = await command(w, "takeover", 1, "lost-expiring-grant")
    assert lost.status_code == 503
    async with get_db_session() as db:
        original = await db.scalar(select(AssistantCommand).where(AssistantCommand.target_id == w.resource_id,
            AssistantCommand.action == "browser_takeover"))
        assert original.state == "accepted"
        assert (await db.get(ResourceControlLease, w.resource_id)).epoch == 1
        command_id = original.id
    receipt = w.journal.receipt("command", command_id)
    # An expired SQL deadline is insufficient while the original remote
    # clock still has its input gate open. The accepted command stays closed
    # locally and cannot return a token or publish a guessed remote hold.
    still_open = await command(w, "takeover", 1, "lost-expiring-grant")
    assert still_open.status_code == 423 and "human_token" not in still_open.text
    assert w.supervisor.status()["control"]["admission"] == "open"
    async with get_db_session() as db:
        assert (await db.get(AssistantCommand, command_id)).state == "accepted"
        assert (await db.get(ResourceControlLease, w.resource_id)).epoch == 1
    clock[0] = time.time()
    recovered = await command(w, "takeover", 1, "lost-expiring-grant")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["state"] == "applied" and recovered.json()["human_grant_expired"]
    assert recovered.json()["fence"]["epoch"] == 2 and "human_token" not in recovered.json()
    assert w.journal.receipt("command", command_id) == receipt
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, w.resource_id)
        assert row.epoch == 2 and row.status == "hold" and row.admission_state == "closed"
        assert (await db.get(AssistantCommand, command_id)).state == "applied"
    replay = await command(w, "takeover", 1, "lost-expiring-grant")
    assert replay.json() == recovered.json() and "human_token" not in replay.json()
    returned = await command(w, "giveback", 2, "explicit-expired-giveback")
    assert returned.status_code == 200, returned.text
    assert returned.json()["fence"]["epoch"] == 3 and returned.json()["fresh_observation_required"]
    assert not w.pipe.calls


async def test_current_actor_revocation_after_remote_grant_never_returns_the_token(browser_world):
    w = browser_world
    await ensure(w)
    w.revoke_after_takeover = True
    response = await command(w, "takeover", 1, "revoked-grant")
    assert response.status_code in {403, 404, 423}, response.text
    assert "human_token" not in response.text
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, w.resource_id)
        assert row.epoch == 1 and row.admission_state == "closed"


async def test_private_browser_identity_and_human_input_do_not_leak_to_peer_or_anonymous(browser_world):
    w = browser_world
    await ensure(w)
    path = f"/api/assistant/browser-resources/{w.resource_id}"
    before = len(w.requests)
    assert (await w.api.get(path)).status_code in {401, 403}
    assert (await w.api.get(path, headers=w.headers(w.w.peer))).status_code == 404
    assert (await w.api.get("/api/assistant/browser-resources/current", headers=w.headers(w.w.peer))).json() == {"resource": None}
    assert len(w.requests) == before


async def test_unsupported_or_forged_input_never_enters_the_private_pipe(browser_world):
    w = browser_world
    await ensure(w)
    grant = (await command(w, "takeover", 1, "finite-only")).json()
    path = f"/api/assistant/browser-resources/{w.resource_id}/operations"
    for change in ({"kind": "execute", "args": {"command": "never"}},
                   {"kind": "navigate", "args": {"url": "file:///data/browser.sqlite3"}},
                   {"fence": {**grant["fence"], "owner_id": w.w.peer}}):
        denied = await w.api.post(path, headers=w.headers(), json={**operation(grant), **change})
        assert denied.status_code in {422, 423}, denied.text
    assert w.pipe.calls == []


async def test_two_device_command_race_has_one_human_epoch(browser_world):
    w = browser_world
    await ensure(w)
    results = await asyncio.gather(command(w, "takeover", 1, "device-a"), command(w, "takeover", 1, "device-b"))
    assert sorted(result.status_code for result in results) == [200, 423]
    assert w.supervisor.status()["control"]["fence"]["epoch"] == 2


@pytest.mark.parametrize("change", ["missing", "diagnostic", "failed-check", "wrong-uid", "mode-mismatch"])
async def test_unverified_browser_never_enrolls_even_when_live(browser_world, change):
    w = browser_world
    if change == "missing":
        w.supervisor.isolation["checks"] = {}
    elif change == "diagnostic":
        w.supervisor.isolation["mode"] = "diagnostic"
    elif change == "failed-check":
        w.supervisor.isolation["checks"]["browser_cannot_signal_supervisor"] = False
    elif change == "wrong-uid":
        w.supervisor.isolation["browser_uid"] = 0
    else:
        w.supervisor.isolation["mode"] = "chromium_sandbox"
    response = await w.api.post("/api/assistant/browser-resources/ensure", headers=w.headers())
    assert response.status_code == 423 and response.json()["detail"]["code"] == "BROWSER_ISOLATION_UNVERIFIED"
    async with get_db_session() as db:
        assert await db.get(BrowserResourceBinding, w.route.resource_id) is None


async def test_unknown_human_operation_is_held_and_never_repeated_on_control_retry(browser_world):
    w = browser_world
    await ensure(w)
    grant = (await command(w, "takeover", 1, "unknown-takeover")).json()
    w.pipe.fail = True
    path = f"/api/assistant/browser-resources/{w.resource_id}/operations"
    response = await w.api.post(path, headers=w.headers(), json=operation(grant, "unknown-input", "text", {"text": "ONCE"}))
    assert response.status_code == 423
    assert len(w.pipe.calls) == 1
    assert w.supervisor.status()["blocking_operations"] == [{"id": "unknown-input", "state": "unknown"}]
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, w.resource_id)
        assert row.admission_state == "closed" and row.status == "hold"
    for _ in range(2):
        retry = await command(w, "giveback", 2, "unknown-giveback")
        assert retry.status_code == 200 and retry.json()["state"] == "draining"
    assert len(w.pipe.calls) == 1
    current = (await w.api.get(f"/api/assistant/browser-resources/{w.resource_id}", headers=w.headers())).json()
    assert current["pending_control"] == {"action": "giveback", "expected_epoch": 2,
        "idempotency_key": "unknown-giveback", "command_id": retry.json()["command_id"]}


async def test_human_heartbeat_and_expiry_do_not_return_control_to_automation(browser_world):
    w = browser_world
    await ensure(w)
    grant = (await command(w, "takeover", 1, "expire-takeover")).json()
    body = {key: grant[key] for key in ("fence", "human_token")}
    path = f"/api/assistant/browser-resources/{w.resource_id}"
    renewed = await w.api.post(path + "/heartbeat", headers=w.headers(), json={**body, "command_id": "heartbeat-one"})
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"] >= grant["expires_at"]
    with w.journal.transaction() as db:
        db.execute("UPDATE control SET expires_at=1 WHERE singleton=1")
    expired = (await w.api.get(path, headers=w.headers())).json()
    assert expired["fence"] == grant["fence"] and expired["admission"] == "closed"
    assert not expired["can_takeover"]
    before = len(w.pipe.calls)
    denied = await w.api.post(path + "/operations", headers=w.headers(), json=operation(grant))
    assert denied.status_code == 423 and len(w.pipe.calls) == before


async def test_sql_submitted_unknown_effect_blocks_takeover_even_with_empty_remote_queue(browser_world):
    w = browser_world
    await ensure(w)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(ExternalEffect(id="browser-effect-unknown", tenant_id=w.w.owner, session_id=w.main.id,
            run_id="original-run", run_generation=1, adapter="private_browser", provider=service.PROVIDER,
            operation="text", idempotency_key="original-part", request_hash="a" * 64, safe_context={},
            resource_id=w.resource_id, resource_epoch=1, resource_owner_kind="automation", resource_owner_id=w.w.workspace,
            state="outcome_unknown", prepared_at=now, submitting_at=now, created_at=now, updated_at=now))
    result = await command(w, "takeover", 1, "effect-drain")
    assert result.status_code == 200 and result.json()["state"] == "draining"
    assert not w.supervisor.status()["blocking_operations"]
    assert w.supervisor.status()["control"]["fence"]["epoch"] == 1
    async with get_db_session() as db:
        effect = await db.get(ExternalEffect, "browser-effect-unknown")
        effect.state, effect.completed_at = "succeeded", now
    applied = await command(w, "takeover", 1, "effect-drain")
    assert applied.status_code == 200 and applied.json()["fence"]["epoch"] == 2


async def test_same_journal_after_supervisor_restart_never_adopts_replacement_runtime(browser_world):
    w = browser_world
    await ensure(w)
    original = dict(w.journal.identity)
    w.journal.release_lock()
    reopened = BrowserJournal(w.journal.path.parent, w.resource_id, w.w.workspace)
    w.supervisor.journal = reopened
    try:
        current = await w.api.get(f"/api/assistant/browser-resources/{w.resource_id}", headers=w.headers())
        assert current.status_code == 200
        assert current.json()["admission"] == "closed" and not current.json()["remote_available"]
        assert not current.json()["can_takeover"]
        assert reopened.identity["runtime_id"] != original["runtime_id"]
        async with get_db_session() as db:
            assert (await db.get(BrowserResourceBinding, w.resource_id)).identity == original
        denied = await command(w, "takeover", 1, "no-adoption")
        assert denied.status_code == 409 and "human_token" not in denied.text
    finally:
        reopened.release_lock()


@pytest.mark.parametrize("later_control", [None, "before-giveback", "after-accept-giveback", "new-takeover", "after-resume-accept"])
async def test_takeover_waits_for_real_driver_and_giveback_preserves_newer_task_controls(browser_world, monkeypatch, later_control):
    from agent import inbox
    from agent.driver import reserve_run
    from assistant import control as task_controls
    from assistant.commands import accept_task_command
    from models.message import TextPart
    from session.session import create_assistant_message, save_part, update_message_info

    w = browser_world
    await ensure(w)
    created = await accept_task_command(**w.scope, project_id=w.main.project_id,
        idempotency_key="browser-task", prompt="Use the dedicated browser")
    lease = await reserve_run(created["execution_session_id"], w.w.owner)
    resumed_leases = []
    original_recover = task_controls.recover_controls
    async def recover_without_provider(**args):
        if later_control == "after-resume-accept":
            await service.accept_control(**w.scope, resource_id=w.resource_id,
                action="takeover", expected_epoch=3, idempotency_key="later-takeover")
        changed, leases = await original_recover(**args, launch=False)
        resumed_leases.extend(leases)
        return changed, leases
    monkeypatch.setattr(task_controls, "recover_controls", recover_without_provider)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        await lease.set_phase("running")
        async with get_db_session() as db:
            db.add(BrowserResourceSession(resource_id=w.resource_id, session_id=lease.session_id,
                created_at=datetime.now(timezone.utc)))
        pending = await command(w, "takeover", 1, "task-takeover")
        assert pending.status_code == 200 and pending.json()["state"] == "draining"
        assert lease.abort.is_set()
        fence = (lease.session_id, lease.run_id, lease.generation)
        answer = await create_assistant_message(lease.session_id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=lease.user_id, run_fence=fence)
        await save_part(TextPart(session_id=lease.session_id, message_id=answer.id, text="Paused for the user"),
            is_new=True, user_id=lease.user_id, run_fence=fence)
        answer.finish = "aborted"
        await update_message_info(answer, user_id=lease.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=answer.id, outcome="aborted")
        await lease.release(session_status="idle")
        await original_recover(task_id=created["task_id"], launch=False)
        grant = await command(w, "takeover", 1, "task-takeover")
        assert grant.status_code == 200 and grant.json()["state"] == "applied", grant.text
        async with get_db_session() as db:
            task = await db.get(AssistantTask, created["task_id"])
            takeover = await db.get(AssistantCommand, grant.json()["command_id"])
            assert task.desired_state == task.observed_state == "paused"
            revision = task.control_revision
            assert takeover.source_ref["resumable_tasks"] == [{"task_id": task.id, "expected_revision": revision, "expected_run": None}]

        async def manual_cancel():
            await task_controls.accept_control_command(**w.scope, task_id=created["task_id"],
                action="cancel", idempotency_key="later-manual-cancel", expected_revision=revision, expected_run=None)
        if later_control == "before-giveback":
            await manual_cancel()
        return_id = await service.accept_control(**w.scope, resource_id=w.resource_id,
            action="giveback", expected_epoch=2, idempotency_key="task-giveback")
        if later_control == "after-accept-giveback":
            await manual_cancel()
        if later_control == "new-takeover":
            original_accept = task_controls.accept_control_command
            async def interleave_takeover(**args):
                if args["action"] == "resume":
                    await service.accept_control(**w.scope, resource_id=w.resource_id,
                        action="takeover", expected_epoch=3, idempotency_key="later-takeover")
                return await original_accept(**args)
            monkeypatch.setattr(task_controls, "accept_control_command", interleave_takeover)
        returned = await service.dispatch_control(return_id)
        assert returned["fence"]["epoch"] == 3
        async with get_db_session() as db:
            task = await db.get(AssistantTask, created["task_id"])
            if later_control is None:
                assert task.desired_state == "running"
                assert returned["resume_requested_task_ids"] == [task.id]
                assert len(resumed_leases) == 1
                driver = await db.get(AgentDriverState, resumed_leases[0].session_id)
                assert driver.trigger_message_id == batch.messages[0].id
            else:
                assert task.desired_state == ("running" if later_control == "after-resume-accept" else
                    "paused" if later_control == "new-takeover" else "canceled")
                assert not resumed_leases
                assert returned["resume_requested_task_ids"] == ([task.id] if later_control == "after-resume-accept" else [])
                if later_control == "after-resume-accept":
                    assert task.observed_state == "resuming"
        if later_control == "after-resume-accept":
            # The deferral must not manufacture a task revision that makes
            # the newer takeover's already accepted pause snapshot stale.
            later = await command(w, "takeover", 3, "later-takeover")
            assert later.status_code == 200 and later.json()["state"] == "applied", later.text
            assert later.json()["fence"]["epoch"] == 4
            async with get_db_session() as db:
                task = await db.get(AssistantTask, created["task_id"])
                assert task.desired_state == task.observed_state == "paused"
    finally:
        await lease.release(session_status="idle")
        for resumed in resumed_leases:
            await resumed.release(session_status="idle")


@pytest.mark.parametrize("new_takeover", [False, True])
async def test_queued_browser_resume_rechecks_the_fence_at_wake_and_actual_reservation(browser_world, monkeypatch, new_takeover):
    from agent import inbox
    from agent.driver import reserve_run
    from assistant import control as task_controls
    from assistant.commands import accept_task_command
    from assistant.scheduling import TaskSchedulingHeld

    w = browser_world
    await ensure(w)
    created = await accept_task_command(**w.scope, project_id=w.main.project_id,
        idempotency_key="queued-browser-task", prompt="Original queued browser input")
    async with get_db_session() as db:
        db.add(BrowserResourceSession(resource_id=w.resource_id, session_id=created["execution_session_id"],
            created_at=datetime.now(timezone.utc)))
    grant = await command(w, "takeover", 1, "queued-takeover")
    assert grant.status_code == 200 and grant.json()["state"] == "applied", grant.text
    wakes, leases = [], []
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args, **kwargs: wakes.append((args, kwargs)))
    original_recover = task_controls.recover_controls

    async def recover_at_actual_wake(**args):
        async with get_db_session() as db:
            resumed = await db.scalar(select(AssistantCommand).where(AssistantCommand.target_id == created["task_id"],
                AssistantCommand.action == "task_resume"))
            assert resumed.state == "applied" and resumed.source_ref["control"]["mode"] == "input_queue"
        if new_takeover:
            await service.accept_control(**w.scope, resource_id=w.resource_id,
                action="takeover", expected_epoch=3, idempotency_key="queued-later-takeover")
        result = await original_recover(**args, launch=True)
        if new_takeover:
            # An already queued worker also crosses the real Driver admission
            # transaction; suppressing a new wake alone is insufficient.
            denied = None
            try:
                leases.append(await reserve_run(created["execution_session_id"], w.w.owner))
            except TaskSchedulingHeld as error:
                denied = error
            assert denied is not None and denied.hold.state == "browser_control"
            assert not wakes
        else:
            assert len(wakes) == 1
            lease = await reserve_run(created["execution_session_id"], w.w.owner)
            leases.append(lease)
            batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
            from db.models.agent_inbox import AgentInboxItem
            async with get_db_session() as db:
                original = await db.get(AgentInboxItem, created["inbox_id"])
                assert original.state == "claimed" and original.run_id == lease.run_id
                assert batch.messages and batch.messages[0].id == original.message_id
        return result

    monkeypatch.setattr(task_controls, "recover_controls", recover_at_actual_wake)
    try:
        returned = await command(w, "giveback", 2, "queued-giveback")
        assert returned.status_code == 200, returned.text
        if new_takeover:
            later = await command(w, "takeover", 3, "queued-later-takeover")
            assert later.status_code == 200 and later.json()["state"] == "applied", later.text
            async with get_db_session() as db:
                task = await db.get(AssistantTask, created["task_id"])
                assert task.desired_state == task.observed_state == "paused"
    finally:
        for lease in leases:
            await lease.release(session_status="idle")
