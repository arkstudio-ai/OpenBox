"""Actual standalone tools, HTTP/SSE and SQL resource admission boundaries."""
import asyncio
from contextlib import suppress

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from agent.hooks import ToolHooks
from db.base import get_db_session
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from sandbox.client import SandboxClient
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, close, drain  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway, new_tool_call  # noqa: F401
from tests.unit.test_assistant_resource_commands import remote, accept  # noqa: F401
from tool.bash import bash_tool
from tool.read import read_tool
from tool.write import write_tool
from tool.tool import ToolInfo, ToolResult, define_tool


class EmptyArgs(BaseModel):
    pass


async def rows(ctx):
    async with get_db_session() as db:
        return list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == ctx.session_id, ExternalEffect.adapter == "sandbox_tool"))).all())


async def prepare(ctx, tool, arguments, monkeypatch):
    await new_tool_call(ctx, tool.id, arguments)
    hooks = ToolHooks(ctx.session_id, ctx.user_id)
    async def allow(*args):
        return None
    monkeypatch.setattr(hooks, "authorize_tool", allow)
    prepared = await hooks.prepare_execute(tool.id, tool.execute, arguments, ctx,
        part_id=ctx.part_id, isolate_context=True)
    return hooks, prepared


async def dispatch(hooks, prepared):
    result = await hooks.dispatch_execute(prepared)
    return await hooks.finalize_execute(prepared, result)


@pytest.fixture
async def standalone(gateway):
    ctx, sent, transport = gateway
    async def respond(request):
        await transport(request)  # Confirms SQL submitting before every send.
        if request.url.path == "/read_file":
            return httpx.Response(200, json={"content": "1: fixture"})
        if request.url.path == "/execute_stream":
            return httpx.Response(200, text='data: {"type":"stdout","content":"fixture"}\n\ndata: {"exit_code":0}\n\n',
                headers={"content-type": "text/event-stream"})
        if request.url.path.startswith("/mcp/tools/"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "fixture"}], "isError": False})
        return httpx.Response(200, json={"exit_code": 0, "stdout": "123", "stderr": ""})
    ctx.sandbox._transport = httpx.MockTransport(respond)
    return ctx, sent, respond


@pytest.mark.parametrize("kind", ["read", "write", "bash", "mcp"])
async def test_actual_standalone_tools_bind_one_effect_and_cannot_replay(standalone, monkeypatch, kind):
    ctx, sent, _ = standalone
    if kind == "mcp":
        from tool.mcp_tool import _make_mcp_executor
        tool = ToolInfo(id="mcp_fixture_echo", description="fixture", parameters=EmptyArgs,
            execute=_make_mcp_executor("fixture", "echo", "mcp_fixture_echo"))
        args = {}
    else:
        tool, args = {
            "read": (read_tool, {"file_path": "/workspace/fixture.txt"}),
            "write": (write_tool, {"file_path": "/workspace/fixture.txt", "content": "fixture"}),
            "bash": (bash_tool, {"command": "printf fixture"}),
        }[kind]
    hooks, prepared = await prepare(ctx, tool, args, monkeypatch)
    result = await dispatch(hooks, prepared)
    assert not result.metadata.get("error")
    row, = await rows(ctx)
    assert row.state == "succeeded" and row.resource_epoch == 1 and row.attempt_count == 1
    assert sent and all(request.headers["X-OpenBox-Resource-Operation"] == row.id for request in sent)
    before = len(sent)
    if kind == "mcp":
        retried = await hooks.prepare_execute(tool.id, tool.execute, args, ctx,
            part_id=ctx.part_id, isolate_context=True)
        retry = await dispatch(hooks, retried)
    else:
        retry = await tool.execute(args, prepared.run_ctx)
    assert len(sent) == before
    assert retry.metadata["error"]


async def test_tool_metadata_cannot_exempt_actual_sandbox_io_and_pure_work_needs_no_claim(standalone, resource, monkeypatch):
    ctx, sent, _ = standalone
    async def body(_args, context):
        await context.sandbox.write_file("/workspace/fixture.txt", "fixture")
        return ToolResult(output="ok")
    tool = define_tool("fixture_optional", description="fixture", parameters=EmptyArgs,
        execute=body, sandbox_required=False)
    hooks, prepared = await prepare(ctx, tool, {}, monkeypatch)
    await close(resource)
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"] and not sent and not await rows(ctx)
    async def text_only(_args, _ctx):
        return ToolResult(output="pure text remains available")
    pure = define_tool("fixture_pure", description="fixture", parameters=EmptyArgs,
        execute=text_only, sandbox_required=False)
    hooks, prepared = await prepare(ctx, pure, {}, monkeypatch)
    assert (await dispatch(hooks, prepared)).output == "pure text remains available"
    assert not await rows(ctx)


@pytest.mark.parametrize("writing", [False, True])
async def test_approval_then_epoch_change_blocks_first_file_request(standalone, resource, monkeypatch, writing):
    ctx, sent, _ = standalone
    args = {"file_path": "/workspace/fixture.txt"}
    if writing:
        args["content"] = "fixture"
    hooks, prepared = await prepare(ctx, write_tool if writing else read_tool, args, monkeypatch)
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        row.epoch += 1
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"] and not sent and not await rows(ctx)


@pytest.mark.parametrize("truncate", [False, True])
async def test_bash_stream_loss_never_resends_through_execute(standalone, monkeypatch, truncate):
    ctx, sent, respond = standalone
    async def lost(request):
        await respond(request)
        if truncate:
            return httpx.Response(200, text='data: {"type":"stdout","content":"partial"}\n\n')
        raise httpx.ReadTimeout("fixture lost response", request=request)
    ctx.sandbox._transport = httpx.MockTransport(lost)
    hooks, prepared = await prepare(ctx, bash_tool, {"command": "printf fixture"}, monkeypatch)
    result = await dispatch(hooks, prepared)
    assert result.metadata["resource_outcome"] == "outcome_unknown"
    assert [request.url.path for request in sent] == ["/execute_stream"]
    row, = await rows(ctx)
    assert row.state == "outcome_unknown" and row.attempt_count == 1


async def test_swallowed_write_failure_cannot_send_a_fallback_or_publish_success(standalone, monkeypatch):
    ctx, sent, respond = standalone
    async def lost(request):
        await respond(request)
        raise httpx.ReadTimeout("fixture response lost", request=request)
    ctx.sandbox._transport = httpx.MockTransport(lost)
    async def body(_args, context):
        for _ in range(2):
            with suppress(Exception):
                await context.sandbox.write_file("/workspace/fixture.txt", "fixture")
        return ToolResult(output="claimed success")
    tool = define_tool("fixture_swallow", description="fixture", parameters=EmptyArgs, execute=body)
    hooks, prepared = await prepare(ctx, tool, {}, monkeypatch)
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"] and result.metadata["resource_outcome"] == "outcome_unknown"
    assert len(sent) == 1 and (await rows(ctx))[0].state == "outcome_unknown"


async def test_compound_close_blocks_next_file_request_and_keeps_unknown(standalone, resource, monkeypatch):
    ctx, sent, _ = standalone
    async def body(_args, context):
        await context.sandbox.write_file("/workspace/fixture.txt", "fixture")
        await close(resource)
        await context.sandbox.write_file("/workspace/late.txt", "late")
        return ToolResult(output="unreachable")
    tool = define_tool("fixture_compound", description="fixture", parameters=EmptyArgs, execute=body)
    hooks, prepared = await prepare(ctx, tool, {}, monkeypatch)
    assert (await dispatch(hooks, prepared)).metadata["error"]
    assert len(sent) == 1
    row, = await rows(ctx)
    assert row.state == "outcome_unknown"
    assert (await drain(resource))["blocking_effect_ids"] == [row.id]


async def test_tool_cannot_switch_to_another_sandbox_client(standalone, monkeypatch):
    ctx, sent, _ = standalone
    other = SandboxClient("wrong-fixture.invalid", 80, "fixture-key")
    async def body(_args, _ctx):
        await other.write_file("/workspace/wrong.txt", "wrong")
        return ToolResult(output="unreachable")
    tool = define_tool("fixture_switch", description="fixture", parameters=EmptyArgs, execute=body)
    hooks, prepared = await prepare(ctx, tool, {}, monkeypatch)
    try:
        assert (await dispatch(hooks, prepared)).metadata["error"]
        assert not sent and not await rows(ctx)
    finally:
        await other.aclose()


async def test_parallel_nested_calls_keep_distinct_effects_and_original_parts(standalone, resource, monkeypatch):
    from tool.batch import batch_tool
    ctx, sent, _ = standalone
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    async def body(_args, context):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await release.wait()
        await context.sandbox.write_file("/workspace/" + context.part_id, "fixture")
        return ToolResult(output="ok")
    child = define_tool("fixture_nested", description="fixture", parameters=EmptyArgs,
        execute=body, parallel_safe=True)
    ctx._tool_execution_lookup = {"fixture_nested": child}
    ctx.available_tools = frozenset({"fixture_nested", "batch"})
    hooks, prepared = await prepare(ctx, batch_tool,
        {"invocations": [{"tool": "fixture_nested", "parameters": {}} for _ in range(2)]}, monkeypatch)
    pending = asyncio.create_task(dispatch(hooks, prepared))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        release.set()
        result = await pending
        assert not result.metadata.get("error")
        recorded = await rows(ctx)
        assert len(recorded) == 2 and all(row.state == "succeeded" for row in recorded)
        assert len({row.safe_context["tool_part_id"] for row in recorded}) == 2
        assert ctx.part_id not in {row.safe_context["tool_part_id"] for row in recorded}
        assert {request.headers["X-OpenBox-Resource-Operation"] for request in sent} == {row.id for row in recorded}
        from session.agent_event_log import load_canonical_model_surface
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected_ids = {part.id for message in surface.messages for part in message.parts}
        assert ctx.part_id in projected_ids
        assert not projected_ids.intersection(row.safe_context["tool_part_id"] for row in recorded)

        # A recovered parent reuses completed child receipts, never new IDs.
        from dataclasses import replace
        from agent.driver import reserve_run
        from db.base import close_engine, get_engine, init_engine
        await resource[2].release(session_status="idle")
        url = get_engine().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        recovered = await reserve_run(ctx.session_id, ctx.user_id)
        try:
            await recovered.set_phase("running")
            restored = replace(ctx, run_id=recovered.run_id, run_generation=recovered.generation)
            retried = await hooks.prepare_execute("batch", batch_tool.execute, prepared.args, restored,
                part_id=ctx.part_id, isolate_context=True)
            assert not (await dispatch(hooks, retried)).metadata.get("error")
            assert count == 2 and len(sent) == 2
            assert {row.id for row in await rows(ctx)} == {row.id for row in recorded}
            changed_args = {"invocations": [{"tool": child.id, "parameters": {"changed": True}}]}
            changed = await hooks.prepare_execute("batch", batch_tool.execute, changed_args, restored,
                part_id=ctx.part_id, isolate_context=True)
            assert (await dispatch(hooks, changed)).metadata["error"]
            assert count == 2 and len(sent) == 2
        finally:
            await recovered.release(session_status="idle")
    finally:
        release.set()
        await pending


async def test_unclassified_nested_tools_keep_serial_execution(standalone, monkeypatch):
    from tool.batch import batch_tool
    ctx, _, _ = standalone
    order = []
    async def body(_args, context):
        identity = context.part_id
        order.append((identity, "start"))
        await context.sandbox.write_file("/workspace/" + identity, "fixture")
        order.append((identity, "end"))
        return ToolResult(output="ok")
    child = define_tool("fixture_serial", description="fixture", parameters=EmptyArgs, execute=body)
    ctx._tool_execution_lookup = {child.id: child}
    ctx.available_tools = frozenset({child.id, "batch"})
    hooks, prepared = await prepare(ctx, batch_tool,
        {"invocations": [{"tool": child.id, "parameters": {}} for _ in range(2)]}, monkeypatch)
    assert not (await dispatch(hooks, prepared)).metadata.get("error")
    assert [stage for _, stage in order] == ["start", "end", "start", "end"]
    assert len(await rows(ctx)) == 2


async def test_cancellation_during_request_keeps_one_unknown_effect(standalone, monkeypatch):
    ctx, sent, respond = standalone
    entered = asyncio.Event()
    async def never_finishes(request):
        await respond(request)
        entered.set()
        await asyncio.Event().wait()
    ctx.sandbox._transport = httpx.MockTransport(never_finishes)
    hooks, prepared = await prepare(ctx, write_tool,
        {"file_path": "/workspace/fixture.txt", "content": "fixture"}, monkeypatch)
    pending = asyncio.create_task(dispatch(hooks, prepared))
    await asyncio.wait_for(entered.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    row, = await rows(ctx)
    assert len(sent) == 1 and row.state == "outcome_unknown" and row.attempt_count == 1


@pytest.mark.parametrize("writing", [False, True])
async def test_actual_v2_server_admits_standalone_process_and_file_operations(remote, resource, tmp_path, monkeypatch, writing):
    from starlette.responses import StreamingResponse
    from assistant import resource_commands as commands
    from tests.unit.test_action_server_desktop_lease import server
    # The shared Action Server test loader stubs the optional SSE package.
    # Keep its real generator/process and ASGI framing through StreamingResponse.
    def sse_response(events):
        async def encoded():
            async for event in events:
                yield f"event: {event['event']}\r\ndata: {event['data']}\r\n\r\n"
        return StreamingResponse(encoded(), media_type="text/event-stream")
    monkeypatch.setattr(server, "EventSourceResponse", sse_response)
    ctx = remote[2]
    bound = await accept(remote, resource, "bind")
    assert await commands.dispatch(bound["command_id"])
    ctx.workdir = str(tmp_path)
    target = tmp_path / "standalone-fixture.txt"
    tool, args = (write_tool, {"file_path": str(target), "content": "standalone-fixture"}) if writing else (
        bash_tool, {"command": "printf standalone-fixture"})
    hooks, prepared = await prepare(ctx, tool, args, monkeypatch)
    result = await dispatch(hooks, prepared)
    assert not result.metadata.get("error")
    if writing:
        assert target.read_text() == "standalone-fixture"
    else:
        assert result.output == "standalone-fixture"
    row, = await rows(ctx)
    assert row.state == "succeeded"
    assert row.safe_context["resource_journal_id"] == remote[1].status()["journal_id"]
    assert row.provider_receipt["remote_exclusivity_verified"] is False


@pytest.mark.parametrize("phase", ["before_request", "preparing", "sending"])
async def test_detached_http_cannot_escape_a_finished_tool_scope(standalone, monkeypatch, phase):
    ctx, sent, respond = standalone
    ready, release = asyncio.Event(), asyncio.Event()
    pending = None
    async def delayed_response(request):
        await respond(request)
        ready.set()
        await release.wait()
        return httpx.Response(200, json={})
    if phase == "sending":
        ctx.sandbox._transport = httpx.MockTransport(delayed_response)
    elif phase == "preparing":
        from sandbox import resource_operation
        original = resource_operation._original_call_resource
        async def delayed_preparation(*args, **kwargs):
            binding = await original(*args, **kwargs)
            ready.set()
            await release.wait()
            return binding
        monkeypatch.setattr(resource_operation, "_original_call_resource", delayed_preparation)
    async def later(context):
        if phase == "before_request":
            ready.set()
            await release.wait()
        await context.sandbox.write_file("/workspace/fixture.txt", "fixture")
    async def body(_args, context):
        nonlocal pending
        pending = asyncio.create_task(later(context))
        await ready.wait()
        return ToolResult(output="returned before child")
    tool = define_tool("fixture_detach", description="fixture", parameters=EmptyArgs, execute=body)
    hooks, prepared = await prepare(ctx, tool, {}, monkeypatch)
    try:
        result = await dispatch(hooks, prepared)
        if phase == "sending":
            assert result.metadata["resource_outcome"] == "outcome_unknown"
            assert (await rows(ctx))[0].state == "outcome_unknown"
        else:
            assert not sent and not await rows(ctx)
        release.set()
        with suppress(Exception):
            await pending
        assert len(sent) == int(phase == "sending")
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
