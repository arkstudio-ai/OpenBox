"""Pre-provider catalogue requests retain their own physical Driver origin."""
import asyncio
import json

import httpx
import pytest

from assistant.policy import AssistantError
from db.base import close_engine, get_engine, init_engine
from sandbox.runtime_operation import RuntimePreparationUncertain
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, close, drain  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway  # noqa: F401
from tests.unit.test_assistant_runtime_resource import runtime, rows  # noqa: F401


@pytest.fixture
def catalogue_transport(runtime):
    ctx, sent, transport, _ = runtime
    async def respond(request):
        await transport(request)  # Real HTTP hooks require durable admission.
        assert request.url.path == "/catalog" and request.method == "GET"
        return httpx.Response(200, headers={"etag": '"fixture-one"'}, json={
            "skills": [{"name": "fixture", "description": "PRIVATE_CATALOGUE_BYTES"}],
            "mcp_tools": [], "mcp_resources": [], "generation": "fixture-one"})
    ctx.sandbox._transport = httpx.MockTransport(respond)
    return respond


async def test_runtime_catalogue_does_not_join_another_callers_background_request(runtime, catalogue_transport):
    ctx, sent, _, _ = runtime
    unrelated = asyncio.get_running_loop().create_future()
    ctx.sandbox._catalogue_inflight = unrelated
    try:
        state = await asyncio.wait_for(ctx.sandbox.get_catalogue_projection_state(), 3)
        assert state.availability == "available" and len(sent) == 1
        assert not unrelated.done() and ctx.sandbox._catalogue_inflight is unrelated
        operation, = await rows(ctx)
        assert operation.operation == "catalogue_read" and operation.state == "succeeded"
        assert operation.provider_receipt["result"] == {"observed": True}
        assert "PRIVATE_CATALOGUE_BYTES" not in json.dumps(operation.provider_receipt)
        assert (await ctx.sandbox.get_catalogue_projection_state()).availability == "stale"
        assert len(sent) == len(await rows(ctx)) == 1
    finally:
        unrelated.cancel()
        ctx.sandbox._catalogue_inflight = None


async def test_cached_catalogue_is_not_resource_authority_after_close(runtime, catalogue_transport, resource):
    ctx, sent, _, _ = runtime
    assert (await ctx.sandbox.get_catalogue_projection_state()).availability == "available"
    await close(resource)
    with pytest.raises(AssistantError):
        await ctx.sandbox.get_catalogue_projection()
    assert (await ctx.sandbox.get_catalogue_projection_state()).availability == "unavailable"
    assert len(sent) == 1


async def test_lost_refresh_cannot_silently_return_old_catalogue_or_send_again_after_reopen(
        runtime, catalogue_transport):
    ctx, sent, _, _ = runtime
    assert (await ctx.sandbox.get_catalogue_projection_state()).availability == "available"
    ctx.sandbox._catalogue_ttl_seconds = 0
    ctx.sandbox._catalogue_clock = lambda: float("inf")
    async def lost(request):
        await catalogue_transport(request)
        raise httpx.ReadTimeout("fixture lost refresh response", request=request)
    ctx.sandbox._transport = httpx.MockTransport(lost)
    assert (await ctx.sandbox.get_catalogue_projection_state()).availability == "unavailable"
    before = await rows(ctx)
    assert [r.state for r in before] == ["succeeded", "outcome_unknown"]
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    ctx.sandbox._transport = httpx.MockTransport(catalogue_transport)
    with pytest.raises(RuntimePreparationUncertain):
        await ctx.sandbox.get_catalogue_projection()
    assert len(sent) == len(await rows(ctx)) == 2


async def test_cancelled_catalogue_read_drains_its_http_child_and_retains_unknown(runtime, resource):
    ctx, sent, transport, _ = runtime
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def hanging(request):
        await transport(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    ctx.sandbox._transport = httpx.MockTransport(hanging)
    reading = asyncio.create_task(ctx.sandbox.get_catalogue_projection())
    await asyncio.wait_for(entered.wait(), 3)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    assert stopped.is_set() and ctx.sandbox._catalogue_inflight is None
    operation, = await rows(ctx)
    assert operation.state == "outcome_unknown" and len(sent) == 1
    assert (await drain(resource))["blocking_effect_ids"] == [operation.id]


async def test_control_change_while_reading_cannot_return_an_available_projection(
        runtime, catalogue_transport, resource):
    ctx, sent, _, _ = runtime
    async def close_after_response(request):
        response = await catalogue_transport(request)
        await close(resource)
        return response
    ctx.sandbox._transport = httpx.MockTransport(close_after_response)
    assert (await ctx.sandbox.get_catalogue_projection_state()).availability == "unavailable"
    assert len(sent) == 1
    operation, = await rows(ctx)
    assert operation.state == "outcome_unknown"
