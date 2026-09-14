"""Side-effect boundaries refuse a revoked run: requests, paid submits, tools, output and the loop.

Revocation is done in the database only, as when another worker accepted new
input, so the runtime's own lease checks must refuse without in-process help.
"""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import db.base as database
from db.models.session import Session
from question import runtime
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.test_run_fencing_api import (  # noqa: F401
    acting_as,
    loop_harness,
    published,
    recording,
    supersede_elsewhere,
    trajectory_events,
)
from tool.tool import ToolContext, ToolResult, define_tool


class LabelArgs(BaseModel):
    label: str


@pytest.fixture
def provider(monkeypatch):
    """Scripted provider adapters and billing meter that count what reaches them."""
    import litellm
    from agent import llm
    from billing.service import UsageMeter
    calls = SimpleNamespace(meters=[], streams=[], completions=[])

    async def start(**kwargs):
        calls.meters.append(kwargs["kind"])
        return None

    async def adapter(*args, purpose="chat", **kwargs):
        calls.streams.append(purpose)
        yield {"type": "finish", "reason": "stop", "usage": {}}

    async def completion(**kwargs):
        calls.completions.append(kwargs["model"])
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(
            index=0, finish_reason="stop", message=SimpleNamespace(content="Launch video plan"))])
    monkeypatch.setattr(UsageMeter, "start", start)
    monkeypatch.setattr(llm, "_stream_litellm_direct", adapter)
    monkeypatch.setattr(llm, "_stream_responses_api", adapter)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(litellm, "drop_params", litellm.drop_params)
    # Every check below must reach the database rather than reuse an earlier one.
    monkeypatch.setattr(runtime, "VERIFIED_REUSE_SECONDS", 0)
    return calls


async def _drain(stream) -> None:
    async for _ in stream:
        pass


def _llm_request(ctx, billing_kind: str = "chat"):
    from agent import llm
    return _drain(llm.stream_llm(agent_def=None, system=[], messages=[], tools={}, model_id="test/model",
                                 ctx=ctx, billing_kind=billing_kind))


async def _paid_submit(submitted: list) -> None:
    from agent.trajectory import capture_service_dispatch
    async with capture_service_dispatch(purpose="video_generation", provider="fixture", model="fixture",
                                        operation="submit", body={"prompt": "clip"}, profile="video_generation"):
        submitted.append(True)


async def test_revoked_run_starts_no_provider_request_meter_or_paid_submit(state, provider):
    from agent.llm import metered_completion
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1")
    submitted = []
    requests = {
        "stream_llm": lambda: _llm_request(ctx),
        "metered_completion": lambda: metered_completion(ctx=ctx, billing_kind="bash_judge",
                                                         model="test/model", messages=[]),
        "capture_service_dispatch": lambda: _paid_submit(submitted),
    }
    ticket = await runtime.start_run("s1", "u1")
    with acting_as(ticket):
        for request in requests.values():
            await request()
    await runtime.finish_run(ticket)
    reached = (list(provider.meters), list(provider.streams), list(provider.completions), list(submitted))
    assert reached == (["chat", "bash_judge"], ["chat"], ["test/model"], [True])

    for name, request in requests.items():
        ticket = await runtime.start_run("s1", "u1")
        assert ticket is not None, name
        await supersede_elsewhere()
        with acting_as(ticket), pytest.raises(runtime.RunRevoked) as refused:
            await request()
        assert refused.value.reason == "superseded", name
    assert (provider.meters, provider.streams, provider.completions, submitted) == reached


async def test_title_and_suggestions_may_follow_the_finished_run_but_never_a_new_turn(state, provider):
    from agent import loop
    from session.session import create_user_message, get_messages
    async with database.get_db_session() as db:
        (await db.get(Session, "s1")).title = ""
    prompt = await create_user_message("s1", "Plan a launch video", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    await runtime.finish_run(ticket, completed=True)
    user_message = next(message for message in await get_messages("s1", user_id="u1")
                        if message.id == prompt.id)
    suggestions_ctx = ToolContext(session_id="s1", user_id="u1", message_id=prompt.id)

    await runtime.run_auxiliary(ticket, "title", loop._ensure_title("s1", user_message, user_id="u1"))
    await runtime.run_auxiliary(ticket, "suggestions", _llm_request(suggestions_ctx, "suggestions"))
    assert (await read(Session, "s1")).title == "Launch video plan"
    assert len(provider.completions) == 1 and provider.streams == ["suggestions"]

    await create_user_message("s1", "Actually, a podcast", user_id="u1")
    async with database.get_db_session() as db:
        (await db.get(Session, "s1")).title = ""
    # A stale title task ends quietly: nobody awaits it.
    await runtime.run_auxiliary(ticket, "title", loop._ensure_title("s1", user_message, user_id="u1"))
    with pytest.raises(runtime.RunRevoked):
        await runtime.run_auxiliary(ticket, "suggestions", _llm_request(suggestions_ctx, "suggestions"))
    assert (await read(Session, "s1")).title == ""
    assert len(provider.completions) == 1 and provider.streams == ["suggestions"]


@pytest.fixture
def tools(state, monkeypatch):
    import tool.registry as registry
    from agent.hooks import ToolHooks
    from permission.permission import Rule
    executed = []

    async def effect(args, ctx):
        executed.append(args.label)
        return ToolResult(output=args.label)
    defined = define_tool("effect", description="test only", parameters=LabelArgs, execute=effect,
                          sandbox_required=False)
    monkeypatch.setitem(registry._tools, "effect", defined)
    hooks = ToolHooks("s1", "u1", config_rules=[Rule(permission="*", pattern="*", action="allow")])
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1",
                      available_tools=frozenset({"batch", "effect"}))
    ctx._authorize_tool = hooks.authorize_tool
    return SimpleNamespace(hooks=hooks, defined=defined, ctx=ctx, executed=executed)


async def test_revoked_run_executes_no_tool_batch_child_or_defined_tool(state, tools):
    from tool import batch

    async def plain(args, ctx):
        tools.executed.append("plain")
        return ToolResult(output="plain")

    def batched(label):
        return batch.execute(batch.BatchArgs(invocations=[batch.Invocation(tool="effect",
                                                                           parameters={"label": label})]), tools.ctx)
    ticket = await runtime.start_run("s1", "u1")
    with acting_as(ticket):
        await tools.hooks.wrap_execute("effect", tools.defined.execute, {"label": "owned"}, tools.ctx,
                                       part_id="owned", tool_info=tools.defined)
        await batched("owned-child")
    assert tools.executed == ["owned", "owned-child"]

    await supersede_elsewhere()
    with acting_as(ticket):
        with pytest.raises(runtime.RunRevoked):
            await tools.hooks.wrap_execute("effect", tools.defined.execute, {"label": "late"}, tools.ctx,
                                           part_id="late", tool_info=tools.defined)
        with pytest.raises(runtime.RunRevoked):
            await tools.hooks.wrap_execute("custom", plain, {}, tools.ctx, part_id="late-custom")
        with pytest.raises(runtime.RunRevoked):
            await batched("late-child")
    assert tools.executed == ["owned", "owned-child"]


async def test_tool_output_stops_publishing_once_the_run_is_revoked(state, tools):
    ticket = await runtime.start_run("s1", "u1")

    async def streaming(args, ctx):
        await ctx.update_output("before revocation")
        runtime.revoke(ticket.run_id, "superseded")
        await ctx.update_output("after revocation")
        return ToolResult(output="finished after revocation")
    with acting_as(ticket):
        await tools.hooks.wrap_execute("stream", streaming, {}, tools.ctx, part_id="stream")
    outputs = [data["part"]["output"] for data in published(state, "part.updated")
               if data["part"].get("id") == "stream"]
    assert outputs == ["before revocation"]
    assert not [data for data in published(state, "tool.completed") if data["partId"] == "stream"]


async def test_revoked_run_ends_as_an_abort_with_its_tool_recorded_cancelled(
        state, recording, loop_harness, monkeypatch):
    import trajectory
    from session.session import create_user_message, update_part_data
    recorded = []
    if not recording:
        async def capture(kind, data, **kwargs):
            recorded.append((kind, data))
        monkeypatch.setattr(trajectory, "record", capture)

    async def replaced_while_running(args, ctx):
        await supersede_elsewhere()  # Another worker accepted new input meanwhile.
        await update_part_data(ctx.part_id, {"id": ctx.part_id, "type": "tool", "tool": "effect",
                                             "status": "running", "title": "progress"}, user_id="u1")
        return ToolResult(output="never saved")
    loop_harness.tools["effect"] = define_tool("effect", description="test only", parameters=LabelArgs,
                                               execute=replaced_while_running, sandbox_required=False)

    async def stream(**kwargs):
        yield {"type": "tool_call", "tool": next(iter(kwargs["tools"])), "call_id": "effect-call",
               "invalid": False, "args": {"label": "work"}}
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}
    monkeypatch.setattr(loop_harness.processor, "stream_llm", stream)
    await create_user_message("s1", "Use the tool", user_id="u1")
    assert await asyncio.wait_for(loop_harness.loop.run_loop("s1", user_id="u1"), timeout=10) is None

    assert not published(state, "session.error")
    assert (await read(Session, "s1")).status != "error"
    if recording:
        finished = [event.data for event in await trajectory_events("tool.finished")]
    else:
        finished = [data for kind, data in recorded if kind == "tool.finished"]
    assert [data["status"] for data in finished] == ["cancelled"]
