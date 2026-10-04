"""Actual computer -> SandboxClient request hooks against the real SQL ledger."""
import asyncio
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from agent import effect_ledger as effects
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_event import AgentEvent
from db.models.external_effect import ExternalEffect
from db.models.message import Message
from db.models.resource_control import ResourceControlLease
from models.message import ToolPartData, ToolStatus
from sandbox.client import SandboxClient
from sandbox.resource_operation import prepare_desktop_tool
from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface
from session.session import create_assistant_message, save_part
from tool import computer
from tool.tool import ToolContext, ToolResult
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import close, drain, resource  # noqa: F401


@pytest.fixture
async def gateway(resource, monkeypatch):
    control, run, _, enrollment = resource
    sent = []

    async def subscribed(_workspace):
        return None

    monkeypatch.setattr("sandbox.entitlement.require_sandbox_subscription", subscribed)

    async def transport(request):
        sent.append(request)
        if request.url.path != "/desktop/lease/release":
            async with get_db_session() as db:
                row = await db.get(ExternalEffect, request.headers["X-OpenBox-Resource-Operation"])
                assert row.state == "submitting" and row.resource_id == control.resource_id
                assert row.resource_epoch == int(request.headers["X-OpenBox-Resource-Epoch"])
        if request.url.path == "/desktop/lease/acquire":
            return httpx.Response(200, json={"token": "fixture-token", "wait_ms": 0})
        return httpx.Response(200, json={"exit_code": 0, "stdout": "ok", "stderr": ""})

    client = SandboxClient("fixture.invalid", 80, "fixture-key", desktop_id=enrollment["desktop_id"],
        workspace_id=enrollment["workspace_id"], reuse_connections=True)
    client._transport = httpx.MockTransport(transport)
    ctx = ToolContext(session_id=run.session_id, user_id=run.tenant_id,
        workspace_id=enrollment["workspace_id"], sandbox=client,
        part_id="fixture-computer-part", run_id=run.run_id, run_generation=run.generation)
    await new_computer_call(ctx)

    async def body(_args, context):
        await context.sandbox.execute("fixture input")
        return ToolResult(title="fixture completed", output="ok")

    monkeypatch.setattr(computer, "_execute_locked", body)
    yield ctx, sent, transport
    await client.aclose()


async def new_computer_call(ctx, arguments=None):
    return await new_tool_call(ctx, "computer", arguments or {"action": "screenshot"})


async def new_tool_call(ctx, tool_id, arguments):
    async with get_db_session() as db:
        parent = await db.scalar(select(Message.id).where(Message.session_id == ctx.session_id,
            Message.role == "user").order_by(Message.created_at.desc()).limit(1))
    message = await create_assistant_message(ctx.session_id, parent, model_id="test/model",
        agent="build", user_id=ctx.user_id, run_fence=ctx.run_fence)
    ctx.message_id = message.id
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
        request_id="fixture:" + message.id, model_id="test/model", provider_binding_digest="a" * 64,
        tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
        expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
        message_id=message.id, resource_desktop_id=ctx.sandbox.desktop_id)
    part = ToolPartData(session_id=ctx.session_id, message_id=message.id, tool=tool_id,
        canonical_tool_id=tool_id, call_id=tool_id + "-" + message.id, status=ToolStatus.RUNNING,
        input=arguments, wire_tool_name=tool_id,
        provider_binding_digest="a" * 64, provider_dialect="test", stream_seq=0)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    ctx.part_id = part.id
    return part


async def invocation(ctx):
    return await computer.execute(computer.ComputerArgs(action="screenshot"), ctx)


async def recorded(ctx):
    async with get_db_session() as db:
        return list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == ctx.session_id, ExternalEffect.adapter == "computer"))).all())


async def test_real_client_sends_bound_identity_only_after_durable_admission_and_never_repeats(gateway, resource):
    ctx, sent, _ = gateway
    assert not (await invocation(ctx)).metadata.get("error")
    row, = await recorded(ctx)
    assert row.state == "succeeded" and row.attempt_count == 1
    assert row.provider_receipt["remote_exclusivity_verified"] is False
    assert [r.url.path for r in sent] == ["/desktop/lease/acquire", "/execute", "/desktop/lease/release"]
    assert sent[1].headers["X-OpenBox-Resource-Owner-Id"] == ctx.workspace_id
    assert (await invocation(ctx)).metadata["error"]
    assert len(sent) == 3 and len(await recorded(ctx)) == 1
    assert (await drain(resource))["tracked_operations_drained"]
    assert not (await drain(resource))["remote_exclusivity_verified"]


async def test_prepared_and_admitted_operation_arriving_after_epoch_change_sends_no_http(gateway, resource, monkeypatch):
    ctx, sent, _ = gateway
    original = effects.mark_effect_submitting

    async def change_after_admission(claim):
        await original(claim)
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            row.epoch += 1

    monkeypatch.setattr(effects, "mark_effect_submitting", change_after_admission)
    assert (await invocation(ctx)).metadata["error"]
    assert not sent
    row, = await recorded(ctx)
    assert row.state == "outcome_unknown" and row.resource_epoch == 1
    assert (await drain(resource))["blocking_effect_ids"] == [row.id]


async def test_close_between_compound_requests_blocks_late_input_but_allows_token_release(gateway, resource, monkeypatch):
    ctx, sent, _ = gateway

    async def compound(_args, context):
        await context.sandbox.execute("first fixture input")
        await close(resource)
        await context.sandbox.execute("late fixture input")
        pytest.fail("a closed resource admitted a second input")

    monkeypatch.setattr(computer, "_execute_locked", compound)
    assert (await invocation(ctx)).metadata["error"]
    assert [r.url.path for r in sent] == ["/desktop/lease/acquire", "/execute", "/desktop/lease/release"]
    row, = await recorded(ctx)
    assert row.state == "outcome_unknown"
    assert (await drain(resource))["blocking_effect_ids"] == [row.id]


@pytest.mark.parametrize("error", [httpx.ReadTimeout("fixture response lost"), asyncio.CancelledError()])
async def test_response_loss_or_local_cancellation_is_durable_unknown_and_cannot_resend(gateway, resource, error):
    ctx, sent, transport = gateway

    async def interrupted(request):
        response = await transport(request)
        if request.url.path == "/execute":
            raise error
        return response

    ctx.sandbox._transport = httpx.MockTransport(interrupted)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await invocation(ctx)
    else:
        assert (await invocation(ctx)).metadata["error"]
    row, = await recorded(ctx)
    assert row.state == "outcome_unknown"
    assert (await drain(resource))["blocking_effect_ids"] == [row.id]
    before = len(sent)
    assert (await invocation(ctx)).metadata["error"]
    assert len(sent) == before


@pytest.mark.parametrize("result", [ToolResult(metadata={"error": True}), ToolResult(metadata={"observation_error": True})])
async def test_swallowed_tool_failures_cannot_become_successful_resource_receipts(gateway, monkeypatch, result):
    ctx, _, _ = gateway

    async def failed(_args, context):
        await context.sandbox.execute("fixture input")
        return result

    monkeypatch.setattr(computer, "_execute_locked", failed)
    await invocation(ctx)
    row, = await recorded(ctx)
    assert row.state == "outcome_unknown" and row.provider_receipt is None


async def test_compound_operation_cannot_switch_sandbox_clients(gateway, monkeypatch):
    ctx, sent, _ = gateway
    other = SandboxClient("second-fixture.invalid", 80, "fixture-key")

    async def switched(_args, _context):
        await other.execute("wrong resource input")
        pytest.fail("a resource operation switched physical clients")

    monkeypatch.setattr(computer, "_execute_locked", switched)
    try:
        assert (await invocation(ctx)).metadata["error"]
        assert [r.url.path for r in sent] == ["/desktop/lease/acquire", "/desktop/lease/release"]
    finally:
        await other.aclose()


@pytest.mark.parametrize("receipt", [
    {"X-OpenBox-Remote-Operation": "wrong-step", "X-OpenBox-Resource-Journal": "a" * 32},
    {"X-OpenBox-Resource-Journal": "a" * 32},
    {"X-OpenBox-Remote-Operation": "wrong-step", "X-OpenBox-Resource-Journal": "not-a-journal"},
])
async def test_forged_or_incomplete_remote_receipts_cannot_complete_the_effect(gateway, receipt):
    ctx, sent, transport = gateway
    async def mismatched(request):
        response = await transport(request)
        response.headers.update(receipt)
        return response
    ctx.sandbox._transport = httpx.MockTransport(mismatched)
    assert (await invocation(ctx)).metadata["error"]
    row, = await recorded(ctx)
    assert row.state == "outcome_unknown"
    assert len(sent) == 1  # Admission response fails before any desktop input.


async def test_provider_request_pins_epoch_before_permission_or_effect_preparation(gateway, resource):
    ctx, sent, _ = gateway
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        row.epoch += 1
    assert (await invocation(ctx)).metadata["error"]
    assert not sent and not await recorded(ctx)
    async with get_db_session() as db:
        request = await db.scalar(select(AgentEvent).where(AgentEvent.message_id == ctx.message_id,
            AgentEvent.kind == "model.requested"))
        assert request.payload["resource_context"]["fence"]["epoch"] == 1


async def test_later_provider_checkpoint_does_not_rebind_an_existing_tool_call(gateway, resource):
    ctx, sent, _ = gateway
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        row.epoch += 1
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
        request_id="later:" + ctx.message_id, model_id="test/model", provider_binding_digest="a" * 64,
        tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
        expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
        message_id=ctx.message_id, resource_desktop_id=ctx.sandbox.desktop_id)
    assert (await invocation(ctx)).metadata["error"]
    assert not sent and not await recorded(ctx)


async def test_permission_wait_and_dispatch_keep_the_original_prepared_resource(gateway, resource, monkeypatch):
    from agent.hooks import ToolHooks
    ctx, sent, _ = gateway
    waiting, approved = asyncio.Event(), asyncio.Event()
    hooks = ToolHooks(ctx.session_id, ctx.user_id)

    async def approve(*args):
        row, = await recorded(ctx)
        assert row.state == "prepared" and row.resource_epoch == 1 and row.submitting_at is None
        waiting.set()
        await approved.wait()

    monkeypatch.setattr(hooks, "authorize_tool", approve)
    pending = asyncio.create_task(hooks.prepare_execute("computer", computer.computer_tool.execute,
        {"action": "screenshot"}, ctx, part_id=ctx.part_id, isolate_context=True))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            row.epoch += 1
        approved.set()
        prepared = await pending
        assert prepared.blocked_result is None
        outcome = await hooks.dispatch_execute(prepared)
        assert outcome.result.metadata["error"]
        assert not sent
        row, = await recorded(ctx)
        assert row.state == "prepared" and row.resource_epoch == 1 and row.attempt_count == 0
    finally:
        approved.set()
        await pending


@pytest.mark.parametrize("change_epoch", [False, True])
async def test_unsent_call_recovers_original_snapshot_after_database_and_driver_restart(gateway, resource, change_epoch):
    from agent.driver import reserve_run
    ctx, sent, _ = gateway
    prepared, _, _ = await prepare_desktop_tool(ctx, {"action": "screenshot"})
    await resource[2].release(session_status="idle")
    url = str(get_engine().url.render_as_string(hide_password=False))
    await close_engine()
    init_engine(url)
    if change_epoch:
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            row.epoch += 1
    recovered = await reserve_run(ctx.session_id, ctx.user_id)
    try:
        await recovered.set_phase("running")
        restored = replace(ctx, run_id=recovered.run_id, run_generation=recovered.generation)
        result = await invocation(restored)
        row, = await recorded(restored)
        assert row.id == prepared.snapshot.effect_id and row.resource_epoch == 1
        if change_epoch:
            assert result.metadata["error"] and not sent
            assert row.state == "prepared" and row.attempt_count == 0
        else:
            assert not result.metadata.get("error") and row.state == "succeeded"
            assert row.run_id == recovered.run_id and row.run_generation == recovered.generation
            assert row.attempt_count == 1 and len(sent) == 3
    finally:
        await recovered.release(session_status="idle")


async def test_call_without_original_resource_checkpoint_is_not_enrolled_at_dispatch(gateway):
    ctx, sent, _ = gateway
    async with get_db_session() as db:
        request = await db.scalar(select(AgentEvent).where(AgentEvent.message_id == ctx.message_id,
            AgentEvent.kind == "model.requested"))
        request.payload = {key:value for key,value in request.payload.items() if key != "resource_context"}
    assert (await invocation(ctx)).metadata["error"]
    assert not sent and not await recorded(ctx)


async def test_persisted_tool_arguments_cannot_be_replaced_before_first_dispatch(gateway):
    ctx, sent, _ = gateway
    result = await computer.execute(computer.ComputerArgs(action="type", text="different input"), ctx)
    assert result.metadata["error"]
    assert not sent and not await recorded(ctx)
