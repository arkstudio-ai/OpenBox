"""Real request/decision/grant transactions; Redis never grants authority."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event as sa_event, select

from agent.hooks import ToolHooks, _bind_tool_context
from assistant import permission_requests as approvals
from assistant.policy import AssistantError
from assistant.retry import read_command
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantTask
from db.models.part import Part
from db.models.permission import PermissionRule
from db.models.question import SessionExecution
from db.models.workspace import WorkspaceMember
from models.message import ToolPartData
from permission import permission as permissions
from question import runtime
from session.session import create_assistant_message, save_part
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running
from tool.tool import ToolContext, ToolResult


@contextmanager
def bound(call):
    token = runtime.current_run.set(call.ticket)
    try:
        with _bind_tool_context(call.ctx):
            yield
    finally:
        runtime.current_run.reset(token)


@pytest.fixture
async def call(monkeypatch):
    monkeypatch.setattr(permissions, "_pending", {})
    monkeypatch.setattr(permissions, "_approved", {})
    monkeypatch.setattr(permissions, "_loaded_users", set())
    monkeypatch.setattr(permissions, "_get_redis_client", lambda: None)
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-permission-test-only")
    args, created, lease, batch = await running()
    ticket = await runtime.start_run(lease.session_id, lease.user_id, driver_lease=lease)
    fence = (lease.session_id, lease.run_id, lease.generation)
    message = await create_assistant_message(lease.session_id, batch.messages[0].id, agent="build",
        model_id="test/model", user_id=lease.user_id, run_fence=fence)
    part = ToolPartData(session_id=lease.session_id, message_id=message.id, tool="read", status="running",
        input={"file_path": "review/report.txt"})
    await save_part(part, is_new=True, user_id=lease.user_id, run_fence=fence)
    ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id, message_id=message.id,
        part_id=part.id, run_id=lease.run_id, run_generation=lease.generation)
    value = SimpleNamespace(lease=lease, ticket=ticket, ctx=ctx, part=part, created=created,
        scope={key: args[key] for key in ("user_id", "workspace_id", "main_id")})
    yield value
    await runtime.finish_run(ticket)
    await lease.release(session_status="idle")


async def register(call):
    with bound(call):
        request = await approvals.register(permissions.PermissionRequest(id="not-the-durable-identity",
            user_id=call.lease.user_id, session_id=call.lease.session_id, tool="read",
            input={"file_path": "review/report.txt"}, patterns=["review/report.txt"], always=["review/**"],
            created_at=runtime.now().isoformat()))
    binding = {"reply_id": "permission-reply-1", "expected_request_revision": request.assistant["request_revision"],
        "options_hash": request.assistant["options_hash"], "source_ref": {"kind": "card"}}
    return request, binding


async def consume(call, request):
    with bound(call):
        return await approvals.consume(request)


@pytest.mark.parametrize("action", ["once", "always", "reject"])
async def test_decision_and_bottom_apply_are_distinct_idempotent_transactions(call, action):
    request, binding = await register(call)
    page = await approvals.list_requests(**call.scope)
    assert page["items"][0]["id"] == request.id
    assert page["items"][0]["assistant"]["generation"] == call.lease.generation
    assert permissions.list_pending(call.lease.user_id) == []  # Another API worker still sees SQL.
    first, replay = await asyncio.gather(*(permissions.reply(request.id, action,
        user_id=call.lease.user_id, **binding) for _ in range(2)))
    assert first["command_id"] == replay["command_id"]
    assert first["state"] in {"accepted", "applying"}
    async with get_db_session() as db:
        assert not (await db.scalars(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))).all()
    left, right = await asyncio.gather(consume(call, request), consume(call, request))
    assert left == right and left["action"] == action
    receipt = await permissions.reply(request.id, action, user_id=call.lease.user_id, **binding)
    assert receipt["state"] == "applied"
    assert (await read_command(**call.scope, command_id=receipt["command_id"]))["receipt"] == receipt
    async with get_db_session() as db:
        rules = (await db.scalars(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))).all()
        assert len(rules) == (1 if action == "always" else 0)
        if rules:
            assert receipt["rule_ids"] == [rules[0].id]
    assert (await approvals.list_requests(**call.scope))["items"] == []


async def test_competing_reply_ids_and_changed_body_conflict(call):
    request, binding = await register(call)
    replies = await asyncio.gather(permissions.reply(request.id, "once", user_id=call.lease.user_id, **binding),
        permissions.reply(request.id, "reject", user_id=call.lease.user_id,
            **{**binding, "reply_id": "another-device"}), return_exceptions=True)
    assert len([r for r in replies if isinstance(r, dict)]) == 1
    assert [r.status for r in replies if isinstance(r, AssistantError)] == [409]
    winner = next(r for r in replies if isinstance(r, dict))
    with pytest.raises(AssistantError) as error:
        await permissions.reply(request.id, "always", user_id=call.lease.user_id,
            **{**binding, "reply_id": winner["reply_id"]})
    assert error.value.status == 409


async def test_actor_admission_does_not_block_event_fk_in_waiting_session(call):
    from assistant.policy import lock_actor
    from session.internal_parts import begin_session_write
    async with get_db_session() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("Independent PostgreSQL row-lock ordering")
    request, _ = await register(call)
    session_held, actor_held = asyncio.Event(), asyncio.Event()

    async def worker():
        async with runtime.transaction(request.session_id, request.user_id, fence=False) as (db, session, _):
            session_held.set()
            await actor_held.wait()
            event = await approvals.event_for(db, request.id, request.user_id)
            await approvals.changed(db, session, event, "concurrent-event")

    async def admission():
        await session_held.wait()
        async with get_db_session() as db:
            await begin_session_write(db)
            await lock_actor(db, request.user_id)
            actor_held.set()
            event = await approvals.event_for(db, request.id, request.user_id)
            await approvals.scope_for(db, event, lock=True)

    await asyncio.wait_for(asyncio.gather(worker(), admission()), timeout=8)


async def test_recovery_isolates_transient_failure_and_closes_missing_original(call, monkeypatch):
    from db.models.assistant import AssistantCommand
    requests = [await register(call) for _ in range(3)]
    receipts = []
    for i, (request, binding) in enumerate(requests):
        receipts.append(await permissions.reply(request.id, "once", user_id=call.lease.user_id,
            **{**binding, "reply_id": f"recover-{i}"}))
    original = approvals.stage_delivery
    seen = []

    async def staged(request_id, user_id):
        seen.append(request_id)
        if request_id == requests[0][0].id:
            raise RuntimeError("Transient delivery failure")
        if request_id == requests[1][0].id:
            raise LookupError("Missing original")
        return await original(request_id, user_id)

    monkeypatch.setattr(approvals, "stage_delivery", staged)
    await approvals.recover_decisions(limit=1000)
    assert all(request.id in seen for request, _ in requests)
    async with get_db_session() as db:
        rows = [await db.get(AssistantCommand, receipt["command_id"]) for receipt in receipts]
        assert [row.state for row in rows] == ["applying", "failed", "applying"]
        assert rows[1].receipt["retryable"] is False
    assert (await consume(call, requests[2][0]))["action"] == "once"


@pytest.mark.parametrize("change", ["driver", "turn", "pause", "part", "expiry", "hash", "revision"])
async def test_stale_request_never_accepts_or_grants(call, monkeypatch, change):
    request, binding = await register(call)
    async with get_db_session() as db:
        if change == "driver": (await db.get(AgentDriverState, request.session_id)).generation += 1
        if change == "turn": (await db.get(SessionExecution, request.session_id)).generation += 1
        if change == "pause": (await db.get(AssistantTask, call.created["task_id"])).desired_state = "paused"
        if change == "part": (await db.get(Part, call.part.id)).data = {**call.part.model_dump(), "status": "completed"}
    if change == "expiry":
        later = runtime.now() + timedelta(seconds=approvals.REQUEST_TTL_SECONDS + 1)
        monkeypatch.setattr(runtime, "now", lambda: later)
    if change == "hash": binding["options_hash"] = "0" * 64
    if change == "revision": binding["expected_request_revision"] = "0" * 64
    with pytest.raises(AssistantError) as error:
        await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    assert error.value.status == 410
    async with get_db_session() as db:
        assert await approvals.decision_for(db, request.id) is None


async def test_replaced_after_acceptance_fails_original_receipt_without_reapproval(call):
    request, binding = await register(call)
    saved = await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    async with get_db_session() as db:
        (await db.get(AgentDriverState, request.session_id)).generation += 1
    await approvals.recover_decisions()
    replay = await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    assert replay["command_id"] == saved["command_id"] and replay["state"] == "failed"
    assert replay["error_code"] == "PERMISSION_GONE" and not replay["retryable"]
    with pytest.raises(AssistantError):
        await consume(call, request)
    async with get_db_session() as db:
        assert not (await db.scalars(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))).all()


async def test_acceptance_survives_delivery_crash_and_engine_restart(call, monkeypatch):
    request, binding = await register(call)
    deliver = approvals.stage_delivery
    async def crash(*_args):
        raise RuntimeError("after SQL acceptance")
    monkeypatch.setattr(approvals, "stage_delivery", crash)
    with pytest.raises(RuntimeError):
        await permissions.reply(request.id, "once", user_id=call.lease.user_id, **binding)
    from db import base
    url = base._engine.url.render_as_string(hide_password=False)
    await base.close_engine()
    base.init_engine(url)
    replay = await permissions.reply(request.id, "once", user_id=call.lease.user_id, **binding)
    assert replay["state"] == "accepted"
    monkeypatch.setattr(approvals, "stage_delivery", deliver)
    await approvals.recover_decisions()
    assert (await consume(call, request))["reply_id"] == binding["reply_id"]


async def test_rule_failure_rolls_back_then_same_decision_recovers_once(call):
    request, binding = await register(call)
    await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    from db import base
    engine = base._engine.sync_engine
    def fail_rule(_conn, _cursor, statement, *_args):
        if statement.lower().startswith("insert into permission_rules"):
            raise RuntimeError("injected storage failure")
    sa_event.listen(engine, "before_cursor_execute", fail_rule)
    try:
        with pytest.raises(RuntimeError):
            await consume(call, request)
    finally:
        sa_event.remove(engine, "before_cursor_execute", fail_rule)
    await approvals.application_failed(request)
    failed = await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    assert failed["state"] == "failed" and failed["retryable"]
    async with get_db_session() as db:
        assert not (await db.scalars(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))).all()
    await approvals.recover_decisions()
    await consume(call, request)
    done = await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    assert done["state"] == "applied" and "error_code" not in done
    assert len(done["rule_ids"]) == 1


@pytest.mark.parametrize("when", ["before_reply", "before_apply"])
async def test_revoked_authority_blocks_reads_replies_and_rule_application(call, when):
    request, binding = await register(call)
    if when == "before_apply":
        await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    async with get_db_session() as db:
        member = await db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == call.lease.user_id,
            WorkspaceMember.workspace_id == call.scope["workspace_id"]))
        member.status = "inactive"
    for action in (lambda: approvals.list_requests(**call.scope),
                   lambda: permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding),
                   lambda: consume(call, request)):
        with pytest.raises(AssistantError):
            await action()
    await approvals.recover_decisions()
    async with get_db_session() as db:
        if when == "before_apply":
            assert (await approvals.decision_for(db, request.id)).state == "failed"
        assert not (await db.scalars(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))).all()


async def test_original_http_route_requires_binding_and_rejects_forged_source(call, monkeypatch):
    from tests.unit.test_assistant_api import client_for
    request, binding = await register(call)
    from api.permissions import router
    async with client_for(call.lease.user_id, call.scope["workspace_id"], monkeypatch) as client:
        client._transport.app.include_router(router, prefix="/api/agent")
        path = f"/api/agent/permission/{request.id}"
        assert (await client.post(path, json={"action": "once"})).status_code == 409
        forged = await client.post(path, json={"action": "always", **binding, "source_ref": {"kind": "tool_result"}})
        assert forged.status_code == 403
        assert (await client.post('/api/agent/permission/missing', json={"action": "once", **binding})).status_code == 404
        first = await client.post(path, json={"action": "once", **binding})
        assert first.status_code == 200 and first.json()["state"] == "applying"
        assert (await client.get('/api/agent/permission')).json() == []


async def test_actual_hook_waits_for_sql_despite_forged_or_missing_redis_reply(call, monkeypatch):
    from tests.unit.test_permission_cross_worker import FakeRedis
    redis = FakeRedis({"id": "unrelated"})
    monkeypatch.setattr(permissions, "_get_redis_client", lambda: redis)
    asked = asyncio.Event()
    def publish(kind, _data):
        if kind == permissions.PERMISSION_ASKED:
            asked.set()
    monkeypatch.setattr(permissions.bus, "publish", publish)
    hooks = ToolHooks(call.ctx.session_id, call.ctx.user_id,
        config_rules=[permissions.Rule(permission="read", pattern="*", action="ask")])
    executed = []
    async def body(args, _ctx):
        executed.append(args)
        return ToolResult(output="read completed")
    with bound(call):
        waiter = asyncio.create_task(hooks.wrap_execute("read", body, call.part.input, call.ctx, part_id=call.part.id))
    try:
        await asyncio.wait_for(asked.wait(), 3)
        [request] = permissions.list_pending(call.ctx.user_id)
        # Another API worker sees the card from SQL, even with no local waiter.
        local = permissions._pending.pop(request.id)
        assert (await approvals.pending_for_user(call.ctx.user_id))[0]["id"] == request.id
        redis.values[f"perm_reply:{request.id}"] = '{"action":"always"}'
        redis.values[f"perm_resp:{request.id}"] = '{"action":"once"}'
        assert not executed and not waiter.done()
        binding = {"reply_id": "real-human", "expected_request_revision": request.assistant["request_revision"],
            "options_hash": request.assistant["options_hash"], "source_ref": {"kind": "card"}}
        await permissions.reply(request.id, "once", user_id=call.ctx.user_id, **binding)
        redis.values.clear()  # Simulates complete Redis loss in this isolated fake.
        assert (await asyncio.wait_for(waiter, 4)).output == "read completed"
        assert executed == [call.part.input]
        receipt = await permissions.reply(request.id, "once", user_id=call.ctx.user_id, **binding)
        assert receipt["state"] == "applied" and local.result == "once"
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_closed_request_is_known_gone_without_any_redis_record(call):
    request, binding = await register(call)
    await approvals.close(request)
    assert (await approvals.list_requests(**call.scope))["items"] == []
    with pytest.raises(AssistantError) as error:
        await permissions.reply(request.id, "once", user_id=call.lease.user_id, **binding)
    assert error.value.status == 410


async def test_saved_once_cannot_be_changed_into_an_always_grant(call):
    request, binding = await register(call)
    await permissions.reply(request.id, "once", user_id=call.lease.user_id, **binding)
    async with get_db_session() as db:
        command = await approvals.decision_for(db, request.id)
        command.source_ref = {**command.source_ref, "decision": {"action": "always", "message": None}}
    with pytest.raises(AssistantError):
        await consume(call, request)
    async with get_db_session() as db:
        assert not (await db.scalars(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))).all()


async def test_applied_always_never_recreates_or_overwrites_a_revoked_grant(call):
    request, binding = await register(call)
    await permissions.reply(request.id, "always", user_id=call.lease.user_id, **binding)
    await consume(call, request)
    async with get_db_session() as db:
        rule = await db.scalar(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))
        rule.action = "deny"
    with pytest.raises(AssistantError):
        await consume(call, request)
    async with get_db_session() as db:
        assert (await db.scalar(select(PermissionRule).where(PermissionRule.user_id == call.lease.user_id))).action == "deny"


async def test_request_lifecycle_replay_contains_only_refs_and_preserves_canonical_surface(call):
    from assistant.events import event_cursor, project_task_events, read_events
    from session.agent_event_log import verify_agent_event_parity
    request, binding = await register(call)
    await permissions.reply(request.id, "once", user_id=call.lease.user_id, **binding)
    await consume(call, request)
    await project_task_events(call.created["task_id"])
    page = await read_events(user_id=call.scope["user_id"], workspace_id=call.scope["workspace_id"],
        after=event_cursor(**call.scope, sequence=0), limit=200)
    changes = [e for e in page["events"] if e["kind"] == "assistant.request.changed"]
    assert len(changes) == 4
    assert all(e["request_id"] == request.id and e["request_kind"] == "permission" for e in changes)
    assert "review/report.txt" not in str(page)
    assert (await verify_agent_event_parity(request.session_id, user_id=call.lease.user_id, require_closed=False)).ok


def test_ordinary_card_does_not_implicitly_resolve_a_linked_waiter(monkeypatch):
    linked = permissions.PendingPermission(request=permissions.PermissionRequest(id="linked", user_id="owner",
        session_id="session", tool="read", patterns=["*"], assistant={"kind": "permission"}))
    legacy = permissions.PendingPermission(request=permissions.PermissionRequest(id="legacy", user_id="owner",
        session_id="session", tool="read", patterns=["*"]), result="reject")
    monkeypatch.setattr(permissions, "_pending", {"linked": linked, "legacy": legacy})
    permissions._resolve_related_pending(legacy)
    assert not linked.event.is_set() and linked.result is None
