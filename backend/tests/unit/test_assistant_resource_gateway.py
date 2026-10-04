"""Actual computer -> SandboxClient request hooks against the real SQL ledger."""
import asyncio

import httpx
import pytest
from sqlalchemy import select

from agent import effect_ledger as effects
from db.base import get_db_session
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from sandbox.client import SandboxClient
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

    async def body(_args, context):
        await context.sandbox.execute("fixture input")
        return ToolResult(title="fixture completed", output="ok")

    monkeypatch.setattr(computer, "_execute_locked", body)
    yield ctx, sent, transport
    await client.aclose()


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
