"""Processor integration for staged policy, parallel bodies and ordered commit."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent import processor as processor_mod
from agent.agent import AgentDef
from agent.driver import LeaseLostError
from agent.hooks import ToolHooks
from agent.processor import process_step
from models.message import ToolPartData
from tool.tool import ToolContext, ToolInfo, ToolResult


class Args:
    """ToolInfo parameter placeholder; the mocked provider never serializes it."""


def tool_info(tool_id: str, body, *, parallel_safe: bool) -> ToolInfo:
    return ToolInfo(
        id=tool_id,
        description=tool_id,
        parameters=Args,
        execute=body,
        sandbox_required=False,
        parallel_safe=parallel_safe,
    )


def tool_events(calls: list[tuple[str, str]]) -> list[dict]:
    return [
        *[
            {
                "type": "tool_call",
                "tool": tool,
                "args": {"id": call_id},
                "call_id": call_id,
                "invalid": False,
            }
            for call_id, tool in calls
        ],
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]


def stream_of(events: list[dict]):
    async def stream(**_kwargs):
        for event in events:
            yield event

    return stream


async def until(predicate) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.fixture
def processor_spy(monkeypatch):
    saves: list[dict] = []
    bus_events: list[tuple[str, dict]] = []

    async def save(part, *, is_new=False, **_kwargs):
        if isinstance(part, ToolPartData):
            saves.append({
                "id": part.id,
                "tool": part.tool,
                "status": part.status.value,
                "input": dict(part.input),
                "output": part.output,
                "error": part.error,
                "metadata": dict(part.metadata or {}),
                "is_new": is_new,
            })

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(processor_mod, "save_part", save)
    monkeypatch.setattr(processor_mod, "update_session", noop)
    monkeypatch.setattr(processor_mod, "update_message_info", noop)
    monkeypatch.setattr(
        processor_mod.bus,
        "publish",
        lambda event, payload: bus_events.append((event, payload)),
    )
    return saves, bus_events


async def run_step(
    monkeypatch,
    *,
    tools: dict[str, ToolInfo],
    hooks: ToolHooks,
    ctx: ToolContext,
    abort: asyncio.Event,
    stream,
    tool_body_timeout_seconds: float | None = None,
):
    monkeypatch.setattr(processor_mod, "stream_llm", stream)
    timeout_kwargs = (
        {"tool_body_timeout_seconds": tool_body_timeout_seconds}
        if tool_body_timeout_seconds is not None
        else {}
    )
    return await process_step(
        session_id="session",
        user_id="user",
        session=SimpleNamespace(),
        agent_def=AgentDef(name="build", description="test"),
        system=[],
        llm_messages=[],
        tools=tools,
        model_id="test/model",
        ctx=ctx,
        hooks=hooks,
        assistant_info=SimpleNamespace(id="assistant", error=None),
        sandbox=None,
        abort=abort,
        doom_loop_history=[],
        **timeout_kwargs,
    )


@pytest.mark.asyncio
async def test_processor_orders_permission_terminal_sse_parts_and_context(
    monkeypatch,
    processor_spy,
):
    saves, bus_events = processor_spy
    gates = {call_id: asyncio.Event() for call_id in ("c1", "c2", "c3")}
    authorized: list[str] = []
    started: list[tuple[str, str]] = []
    finished: list[str] = []

    async def body(args, ctx):
        call_id = args["id"]
        started.append((call_id, ctx.part_id))
        await gates[call_id].wait()
        finished.append(call_id)
        return ToolResult(title=call_id, output=f"done-{call_id}")

    info = tool_info("parallel", body, parallel_safe=True)
    hooks = ToolHooks("session", "user")

    async def authorize(_tool_id, args):
        authorized.append(args["id"])
        return None

    monkeypatch.setattr(hooks, "authorize_tool", authorize)
    ctx = ToolContext()
    running = asyncio.create_task(run_step(
        monkeypatch,
        tools={"parallel": info},
        hooks=hooks,
        ctx=ctx,
        abort=asyncio.Event(),
        stream=stream_of(tool_events([
            ("c1", "parallel"),
            ("c2", "parallel"),
            ("c3", "parallel"),
        ])),
    ))

    await until(lambda: len(started) == 3)
    assert authorized == ["c1", "c2", "c3"]
    assert len({part_id for _, part_id in started}) == 3

    gates["c3"].set()
    gates["c2"].set()
    await until(lambda: set(finished) == {"c2", "c3"})
    assert not [save for save in saves if save["status"] == "completed"]
    assert not [event for event, _ in bus_events if event == "tool.completed"]

    gates["c1"].set()
    result = await running

    completed = [save for save in saves if save["status"] == "completed"]
    assert [save["input"]["id"] for save in completed] == ["c1", "c2", "c3"]
    terminal = [payload for event, payload in bus_events if event == "tool.completed"]
    assert [payload["title"] for payload in terminal] == ["c1", "c2", "c3"]
    assert [part.input["id"] for part in result.completed_tool_parts] == [
        "c1",
        "c2",
        "c3",
    ]
    assert ctx.part_id == completed[-1]["id"]


@pytest.mark.asyncio
async def test_processor_exclusive_tool_is_a_barrier_through_result_commit(
    monkeypatch,
    processor_spy,
):
    saves, _bus_events = processor_spy
    gates = {name: asyncio.Event() for name in ("before", "exclusive", "after")}
    started: list[str] = []

    async def body(args, _ctx):
        name = args["id"]
        started.append(name)
        await gates[name].wait()
        return ToolResult(title=name, output=name)

    safe = tool_info("safe", body, parallel_safe=True)
    exclusive = tool_info("exclusive", body, parallel_safe=False)
    hooks = ToolHooks("session", "user")

    async def allow(*_args):
        return None

    monkeypatch.setattr(hooks, "authorize_tool", allow)
    running = asyncio.create_task(run_step(
        monkeypatch,
        tools={"safe": safe, "exclusive": exclusive},
        hooks=hooks,
        ctx=ToolContext(),
        abort=asyncio.Event(),
        stream=stream_of(tool_events([
            ("before", "safe"),
            ("exclusive", "exclusive"),
            ("after", "safe"),
        ])),
    ))

    await until(lambda: started == ["before"])
    gates["before"].set()
    await until(lambda: started == ["before", "exclusive"])
    assert any(
        save["status"] == "completed" and save["input"]["id"] == "before"
        for save in saves
    )
    gates["exclusive"].set()
    await until(lambda: started == ["before", "exclusive", "after"])
    assert any(
        save["status"] == "completed" and save["input"]["id"] == "exclusive"
        for save in saves
    )
    gates["after"].set()
    await running


@pytest.mark.asyncio
async def test_processor_pairs_every_call_aborted_before_dispatch(
    monkeypatch,
    processor_spy,
):
    saves, _bus_events = processor_spy
    abort = asyncio.Event()
    body_calls: list[str] = []

    async def body(args, _ctx):
        body_calls.append(args["id"])
        return ToolResult(title="unexpected", output="unexpected")

    async def aborting_stream(**_kwargs):
        for event in tool_events([("c1", "parallel"), ("c2", "parallel")]):
            yield event
        abort.set()

    hooks = ToolHooks("session", "user")

    async def should_not_authorize(*_args):
        raise AssertionError("pre-aborted calls must not enter permission")

    monkeypatch.setattr(hooks, "authorize_tool", should_not_authorize)
    await run_step(
        monkeypatch,
        tools={"parallel": tool_info("parallel", body, parallel_safe=True)},
        hooks=hooks,
        ctx=ToolContext(),
        abort=abort,
        stream=aborting_stream,
    )

    errors = [save for save in saves if save["status"] == "error"]
    assert body_calls == []
    assert [save["input"]["id"] for save in errors] == ["c1", "c2"]
    assert all(
        save["metadata"]["failure_code"] == "aborted_before_dispatch"
        and save["is_new"]
        for save in errors
    )


@pytest.mark.asyncio
async def test_processor_abort_drains_started_bodies_and_does_not_refill(
    monkeypatch,
    processor_spy,
):
    saves, _bus_events = processor_spy
    abort = asyncio.Event()
    gates = {call_id: asyncio.Event() for call_id in ("c1", "c2")}
    started: list[str] = []

    async def body(args, _ctx):
        call_id = args["id"]
        started.append(call_id)
        await gates[call_id].wait()
        return ToolResult(title=call_id, output=call_id)

    hooks = ToolHooks("session", "user")

    async def allow(*_args):
        return None

    monkeypatch.setattr(hooks, "authorize_tool", allow)
    real_scheduler = processor_mod.run_ordered_tool_calls

    async def capped_scheduler(calls, signal, **kwargs):
        return await real_scheduler(calls, signal, max_parallel=2, **kwargs)

    monkeypatch.setattr(processor_mod, "run_ordered_tool_calls", capped_scheduler)
    running = asyncio.create_task(run_step(
        monkeypatch,
        tools={"parallel": tool_info("parallel", body, parallel_safe=True)},
        hooks=hooks,
        ctx=ToolContext(),
        abort=abort,
        stream=stream_of(tool_events([
            ("c1", "parallel"),
            ("c2", "parallel"),
            ("c3", "parallel"),
            ("c4", "parallel"),
        ])),
    ))

    await until(lambda: started == ["c1", "c2"])
    abort.set()
    gates["c2"].set()
    await asyncio.sleep(0)
    assert not running.done(), "the first body must reach quiescence before return"
    gates["c1"].set()
    await running

    assert started == ["c1", "c2"]
    terminal = [
        (save["input"]["id"], save["status"], save["metadata"].get("failure_code"))
        for save in saves
        if save["status"] in {"completed", "error"}
    ]
    assert terminal == [
        ("c1", "completed", None),
        ("c2", "completed", None),
        ("c3", "error", "aborted_before_dispatch"),
        ("c4", "error", "aborted_before_dispatch"),
    ]


@pytest.mark.asyncio
async def test_stale_tool_outcome_never_reaches_ordered_finalize(
    monkeypatch,
    processor_spy,
):
    saves, bus_events = processor_spy
    stale = False

    async def assert_current():
        if stale:
            raise LeaseLostError("taken over")

    async def body(_args, _ctx):
        nonlocal stale
        stale = True
        return ToolResult(title="old outcome", output="must not commit")

    hooks = ToolHooks("session", "user")

    async def allow(*_args):
        return None

    monkeypatch.setattr(hooks, "authorize_tool", allow)
    ctx = ToolContext(_assert_current=assert_current)

    with pytest.raises(LeaseLostError, match="taken over"):
        await run_step(
            monkeypatch,
            tools={"exclusive": tool_info("exclusive", body, parallel_safe=False)},
            hooks=hooks,
            ctx=ctx,
            abort=asyncio.Event(),
            stream=stream_of(tool_events([("old", "exclusive")])),
        )

    assert any(save["status"] == "running" for save in saves)
    assert not any(save["status"] == "completed" for save in saves)
    assert not any(event == "tool.completed" for event, _ in bus_events)
    assert not any(event == "session.error" for event, _ in bus_events)


@pytest.mark.asyncio
async def test_processor_timeout_cleans_hook_context_and_commits_explicit_error(
    monkeypatch,
    processor_spy,
):
    saves, bus_events = processor_spy
    lifecycle: list[str] = []
    body_started = asyncio.Event()
    body_cancelled = asyncio.Event()
    request_exited = asyncio.Event()

    class Sandbox:
        @asynccontextmanager
        async def request_context(self, **_kwargs):
            lifecycle.append("request-enter")
            try:
                yield
            finally:
                lifecycle.append("request-exit")
                request_exited.set()

    async def body(_args, _ctx):
        body_started.set()
        lifecycle.append("body-start")
        try:
            await asyncio.Event().wait()
        finally:
            lifecycle.append("body-cancelled")
            body_cancelled.set()

    hooks = ToolHooks("session", "user")

    async def allow(*_args):
        return None

    monkeypatch.setattr(hooks, "authorize_tool", allow)
    previous_output = object()
    previous_nested_authorizer = object()
    ctx = ToolContext(
        sandbox=Sandbox(),
        _authorized_tool_id="outer-tool",
        _authorized_tool_args_key="outer-args",
        _on_output=previous_output,
        _authorize_tool=previous_nested_authorizer,
    )
    result = await run_step(
        monkeypatch,
        tools={"hung": tool_info("hung", body, parallel_safe=False)},
        hooks=hooks,
        ctx=ctx,
        abort=asyncio.Event(),
        stream=stream_of(tool_events([("hung-call", "hung")])),
        tool_body_timeout_seconds=0.02,
    )

    assert body_started.is_set()
    assert body_cancelled.is_set()
    assert request_exited.is_set()
    assert lifecycle == [
        "request-enter",
        "body-start",
        "body-cancelled",
        "request-exit",
    ]
    assert ctx._authorized_tool_id == "outer-tool"
    assert ctx._authorized_tool_args_key == "outer-args"
    assert ctx._on_output is previous_output
    assert ctx._authorize_tool is previous_nested_authorizer

    terminal = [save for save in saves if save["status"] == "error"]
    assert len(terminal) == 1
    assert terminal[0]["input"]["id"] == "hung-call"
    assert terminal[0]["metadata"]["failure_code"] == "tool_timeout"
    assert "exceeded 0.02 seconds" in terminal[0]["error"]
    assert [part.input["id"] for part in result.completed_tool_parts] == [
        "hung-call"
    ]
    error_events = [
        payload for event, payload in bus_events if event == "tool.error"
    ]
    assert len(error_events) == 1
    assert "exceeded 0.02 seconds" in error_events[0]["error"]


@pytest.mark.asyncio
async def test_processor_external_cancellation_is_not_persisted_as_timeout(
    monkeypatch,
    processor_spy,
):
    saves, bus_events = processor_spy
    body_started = asyncio.Event()
    body_cancelled = asyncio.Event()

    async def body(_args, _ctx):
        body_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            body_cancelled.set()

    hooks = ToolHooks("session", "user")

    async def allow(*_args):
        return None

    monkeypatch.setattr(hooks, "authorize_tool", allow)
    previous_output = object()
    ctx = ToolContext(
        _authorized_tool_id="outer-tool",
        _authorized_tool_args_key="outer-args",
        _on_output=previous_output,
    )
    running = asyncio.create_task(run_step(
        monkeypatch,
        tools={"hung": tool_info("hung", body, parallel_safe=False)},
        hooks=hooks,
        ctx=ctx,
        abort=asyncio.Event(),
        stream=stream_of(tool_events([("hung-call", "hung")])),
    ))
    await body_started.wait()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert body_cancelled.is_set()
    assert ctx._authorized_tool_id == "outer-tool"
    assert ctx._authorized_tool_args_key == "outer-args"
    assert ctx._on_output is previous_output
    assert not any(
        save["metadata"].get("failure_code") == "tool_timeout"
        for save in saves
    )
    assert not any(event == "tool.error" for event, _ in bus_events)
