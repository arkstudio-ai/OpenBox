"""Production adapter/executor boundaries, independent of external providers."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent.trajectory import request_snapshot, responses_chunk_blocks
from tool.tool import ToolContext, ToolResult, define_tool
from trajectory.context import TraceContext


@pytest.fixture
def recorded(monkeypatch):
    import trajectory
    events = []

    async def record(kind, data, *, context=None, **kwargs):
        if context is not None:
            events.append({"type": kind, "data": data, "context": context})
    monkeypatch.setattr(trajectory, "record", record)
    monkeypatch.setattr(trajectory, "record_stream", lambda context, event:
        asyncio.create_task(record(event["type"], event["data"], context=context)))
    return events


def context():
    return ToolContext(session_id="session", user_id="owner", message_id="message",
        trace_context=TraceContext(user_id="owner", session_id="session", turn_id="turn",
                                   run_id="run", step_id="step", agent_id="agent"))


def test_request_snapshot_keeps_the_complete_body_without_transport_settings_and_credentials():
    class Part(BaseModel):
        type: str
        text: str
        provider_specific_fields: dict

    snapshot = request_snapshot({
        "model": "provider/model", "api_key": "sk-never-recorded", "api_base": "https://proxy.invalid/v1",
        "extra_headers": {"Authorization": "Bearer never-recorded"}, "timeout": 30,
        "previous_response_id": "resp_1", "include": ["reasoning.encrypted_content"],
        "messages": [{"role": "assistant", "encrypted_content": "gAAAAB-fixture",
                      "_responses_input_items": [{"type": "reasoning", "id": "rs_1"}],
                      "content": [{"type": "thinking", "thinking": "why", "signature": "sig-1"},
                                  Part(type="text", text="visible", provider_specific_fields={"cache": "hit"})]}],
        "extra_body": {"enable_thinking": True, "top_k": 20},
    })
    assert snapshot["omitted_fields"] == ["api_base", "api_key", "extra_headers", "timeout"]
    assert "never-recorded" not in json.dumps(snapshot) and "proxy.invalid" not in json.dumps(snapshot)
    assert snapshot["previous_response_id"] == "resp_1" and snapshot["include"] == ["reasoning.encrypted_content"]
    assert snapshot["extra_body"] == {"enable_thinking": True, "top_k": 20}
    assert snapshot["messages"] == [{"role": "assistant", "encrypted_content": "gAAAAB-fixture",
                                     "_responses_input_items": [{"type": "reasoning", "id": "rs_1"}],
                                     "content": [{"type": "thinking", "thinking": "why", "signature": "sig-1"},
                                                 {"type": "text", "text": "visible",
                                                  "provider_specific_fields": {"cache": "hit"}}]}]


@pytest.mark.asyncio
async def test_tool_denial_is_recorded_without_dispatch_or_execution_duration(recorded):
    from agent.hooks import ToolHooks
    hooks = ToolHooks("session", "owner")

    async def denied(*args):
        return ToolResult(title="Denied", output="No", metadata={"blocked": True})

    async def forbidden(*args):
        pytest.fail("denied tool entered executor")
    hooks.authorize_tool = denied
    result = await hooks.wrap_execute("example", forbidden, {}, context(), part_id="call")
    assert result.metadata["blocked"]
    assert [e["type"] for e in recorded] == ["tool.requested", "tool.finished"]
    assert recorded[-1]["data"]["status"] == "denied"
    assert recorded[-1]["data"]["duration_ms"] is None


@pytest.mark.asyncio
async def test_full_tool_result_is_retained_before_model_truncation(monkeypatch, recorded):
    from agent.hooks import ToolHooks
    import tool.truncation

    async def truncate(_):
        return SimpleNamespace(content="short", truncated=True)
    monkeypatch.setattr(tool.truncation, "truncate_output", truncate)

    class Args(BaseModel):
        value: str

    async def execute(args, ctx):
        assert recorded[-1]["type"] == "tool.started"
        await ctx.update_output("working")
        return ToolResult(output="complete original output")
    tool = define_tool("example", description="Example", parameters=Args, execute=execute, sandbox_required=False)
    hooks = ToolHooks("session", "owner")

    async def allow(*args):
        return None
    hooks.authorize_tool = allow
    result = await hooks.wrap_execute("example", tool.execute, {"value": "x"}, context(), part_id="call", tool_info=tool)
    assert result.output == "short"
    outputs = [e for e in recorded if e["type"] == "tool.output"]
    assert outputs[-1]["data"]["output"] == "complete original output"
    assert recorded[-1]["data"]["model_output"] == "short"
    assert all(e["context"].call_id == "call" for e in recorded)
    assert len([e for e in recorded if e["type"] == "tool.started"]) == 1


@pytest.mark.asyncio
async def test_invalid_tool_arguments_never_have_execution_start(recorded):
    from agent.hooks import ToolHooks

    class Args(BaseModel):
        number: int

    async def execute(*args):
        pytest.fail("invalid parameters entered the executor")
    tool = define_tool("example", description="Example", parameters=Args, execute=execute, sandbox_required=False)
    hooks = ToolHooks("session", "owner")

    async def allow(*args):
        return None
    hooks.authorize_tool = allow
    await hooks.wrap_execute("example", tool.execute, {}, context(), part_id="call", tool_info=tool)
    assert [e["type"] for e in recorded] == ["tool.requested", "tool.finished"]
    assert recorded[-1]["data"]["status"] == "failed"
    assert recorded[-1]["data"]["duration_ms"] is None


@pytest.mark.asyncio
async def test_custom_executor_full_output_slot_is_cleared_between_calls(recorded):
    from agent.hooks import ToolHooks
    hooks = ToolHooks("session", "owner")
    async def allow(*args):
        return None
    hooks.authorize_tool = allow
    ctx = context()
    async def first(args, executing):
        executing._trajectory_full_tool_output = "complete internally truncated output"
        return ToolResult(output="model preview")
    async def second(args, executing):
        assert executing._trajectory_full_tool_output is None
        return ToolResult(output="second result")
    await hooks.wrap_execute("custom", first, {}, ctx, part_id="first")
    await hooks.wrap_execute("custom", second, {}, ctx, part_id="second")
    outputs = [event["data"]["output"] for event in recorded if event["type"] == "tool.output"]
    assert outputs == ["complete internally truncated output", "second result"]
    assert [event["data"]["model_output"] for event in recorded if event["type"] == "tool.finished"] == [
        "model preview", "second result"]


@pytest.mark.asyncio
async def test_cumulative_output_pushes_are_recorded_as_suffixes_and_replay_to_the_final_output(recorded):
    from agent.hooks import ToolHooks
    from trajectory.projector import replay
    hooks = ToolHooks("session", "owner")

    async def allow(*args):
        return None
    hooks.authorize_tool = allow

    async def execute(args, ctx):
        # bash pushes its whole collected output on every chunk, repeats a push
        # while idle, and appends an idle notice that the next chunk drops again.
        for output in ("one", "one two", "one two", "one two three", "one two three\n[Waiting...]\n",
                       "one two three four"):
            await ctx.update_output(output)
        return ToolResult(output="one two three four")
    await hooks.wrap_execute("custom", execute, {}, context(), part_id="call")

    outputs = [event["data"] for event in recorded if event["type"] == "tool.output"]
    assert [(item["mode"], item["output"]) for item in outputs] == [
        ("delta", "one"), ("delta", " two"), ("delta", " three"), ("delta", "\n[Waiting...]\n"),
        ("replace", "one two three four"), ("replace", "one two three four")]
    assert [item.get("chunk_index") for item in outputs] == [0, 1, 2, 3, 4, None]
    assert outputs[-1]["stage"] == "executor_result" and outputs[-1]["final"] is True
    assert all("redaction" not in item for item in outputs)

    events = [{"seq": str(index), "version": 1, "type": event["type"], "data": event["data"],
               "event_id": f"evt_{index}", "occurred_at": "2026-09-15T00:00:00.000Z", "user_id": "owner",
               "session_id": "session", "source_session_id": "session", "call_id": "call"}
              for index, event in enumerate(recorded, 1)]
    streamed = [event for event in events if event["type"] != "tool.output" or event["data"].get("stage") == "executor_stream"]
    assert replay(streamed[:5])["records"]["tool:call"]["data"]["output"] == "one two three"
    assert replay(streamed)["records"]["tool:call"]["data"]["output"] == "one two three four"
    assert replay(events)["records"]["tool:call"]["data"]["output"] == "one two three four"


def test_responses_final_only_output_is_a_replace_checkpoint():
    blocks = responses_chunk_blocks({"type": "response.completed", "response": {"output": [
        {"id": "item", "type": "message", "content": [{"type": "output_text", "text": "answer"}]},
        {"id": "call", "type": "function_call", "call_id": "provider-id", "name": "read", "arguments": "{}"},
    ]}})
    assert blocks[0] == {"type": "text", "block_id": "item", "delta": "answer", "mode": "replace"}
    assert blocks[1]["provider_call_id"] == "provider-id"


@pytest.mark.asyncio
async def test_parallel_batch_keeps_parent_and_sibling_contexts_separate(monkeypatch, recorded):
    import tool.registry as registry
    from tool.batch import BatchArgs, Invocation, execute as batch

    class Args(BaseModel):
        label: str

    seen = []
    async def execute(args, ctx):
        await asyncio.sleep(0)
        seen.append((args.label, ctx.part_id, ctx.trace_context))
        await ctx.update_output(args.label)
        return ToolResult(output=args.label)
    tool = define_tool("example", description="Example", parameters=Args, execute=execute, sandbox_required=False)
    monkeypatch.setitem(registry._tools, "example", tool)
    ctx = context()
    ctx.trace_context = ctx.trace_context.derive(call_id="parent")
    ctx.available_tools = frozenset({"batch", "example"})

    async def allow(*args):
        return None
    ctx._authorize_tool = allow
    await batch(BatchArgs(invocations=[Invocation(tool="example", parameters={"label": label}) for label in ("a", "b")]), ctx)
    assert ctx.trace_context.call_id == "parent"
    assert ctx.part_id == ""
    assert len({part_id for _, part_id, _ in seen}) == 2
    assert {trace.parent_call_id for _, _, trace in seen} == {"parent"}
    assert {trace.session_id for _, _, trace in seen} == {"session"}


@pytest.mark.asyncio
async def test_closing_consumer_closes_provider_reader_and_records_cancel(monkeypatch, recorded):
    import litellm
    from agent import llm
    closed = asyncio.Event()

    async def provider():
        try:
            yield SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=None,
                delta=SimpleNamespace(content="prefix", reasoning_content=None, tool_calls=[]))], usage=None)
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def completion(**kwargs):
        return provider()
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {})
    stream = llm._stream_litellm_direct("provider/model", [], [], {}, trace_ctx=context())
    assert (await anext(stream))["text"] == "prefix"
    await stream.aclose()
    assert closed.is_set()
    assert recorded[-1]["type"] == "request.finished"
    assert recorded[-1]["data"]["status"] == "cancelled"
