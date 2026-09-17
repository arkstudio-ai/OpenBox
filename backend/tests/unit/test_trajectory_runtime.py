"""Production adapter/executor boundaries, independent of external providers."""
import asyncio
import hashlib
import json
import time
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


def allowing_hooks():
    from agent.hooks import ToolHooks
    hooks = ToolHooks("session", "owner")

    async def allow(*args):
        return None
    hooks.authorize_tool = allow
    return hooks


def trace_events(recorded, call_id="call"):
    """The recorded facts as stored events, in the order they were enqueued."""
    return [{"seq": str(index), "version": 1, "type": event["type"], "data": event["data"],
             "event_id": f"evt_{index}", "occurred_at": "2026-09-15T00:00:00.000Z", "user_id": "owner",
             "session_id": "session", "source_session_id": "session", "call_id": call_id}
            for index, event in enumerate(recorded, 1)]


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
async def test_cumulative_output_pushes_are_recorded_as_suffixes_and_replay_to_the_final_output(monkeypatch, recorded):
    from agent.hooks import ToolHooks
    from trajectory import tool_output
    from trajectory.projector import replay
    # Without an interval every change is recorded at once.
    monkeypatch.setattr(tool_output, "TOOL_OUTPUT_RECORD_SECONDS", 0)
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

    events = trace_events(recorded)
    streamed = [event for event in events if event["type"] != "tool.output" or event["data"].get("stage") == "executor_stream"]
    assert replay(streamed[:5])["records"]["tool:call"]["data"]["output"] == "one two three"
    assert replay(streamed)["records"]["tool:call"]["data"]["output"] == "one two three four"
    assert replay(events)["records"]["tool:call"]["data"]["output"] == "one two three four"


@pytest.mark.asyncio
async def test_hundreds_of_pushes_record_at_most_one_change_a_second_and_replay_to_the_output(monkeypatch, recorded):
    from agent import hooks as hooks_module
    from bus.events import PART_UPDATED
    from trajectory.projector import replay
    published = []
    monkeypatch.setattr(hooks_module.bus, "publish", lambda kind, data: published.append(kind))
    line = "x" * 230 + "\n"
    span = {}

    async def execute(args, ctx):
        output = ""
        span["start"] = time.monotonic()
        for _ in range(400):
            output += line
            await ctx.update_output(output)
            await asyncio.sleep(0.0025)
        span["end"] = time.monotonic()
        # The timer records what the burst left pending, within the interval.
        await asyncio.sleep(1.05)
        return ToolResult(output=output)
    await allowing_hooks().wrap_execute("custom", execute, {}, context(), part_id="call")

    final = line * 400
    assert len(final) // 1024 == 90
    assert published.count(PART_UPDATED) == 400  # the chat still gets every push
    events = trace_events(recorded)
    stream = [event["data"] for event in events if event["data"].get("stage") == "executor_stream"]
    assert 2 <= len(stream) <= span["end"] - span["start"] + 2
    assert [item["chunk_index"] for item in stream] == list(range(len(stream)))
    assert {item["mode"] for item in stream} == {"delta"}
    assert events[-2]["data"]["stage"] == "executor_result" and events[-1]["type"] == "tool.finished"
    streamed = [event for event in events if event["data"].get("stage") != "executor_result"]
    assert replay(streamed)["records"]["tool:call"]["data"]["output"] == final
    assert replay(events)["records"]["tool:call"]["data"]["output"] == final


@pytest.mark.asyncio
async def test_the_final_result_stays_the_last_output_while_a_flush_is_pending(monkeypatch, recorded):
    from trajectory import tool_output
    monkeypatch.setattr(tool_output, "TOOL_OUTPUT_RECORD_SECONDS", 0.05)

    class Args(BaseModel):
        value: str

    async def execute(args, ctx):
        await ctx.update_output("first")
        await ctx.update_output("first second")  # within the interval: its flush is pending
        return ToolResult(output="first second third")

    async def custom(args, ctx):
        return await execute(args, ctx)
    registered = define_tool("example", description="Example", parameters=Args, execute=execute,
                             sandbox_required=False)
    hooks = allowing_hooks()
    await hooks.wrap_execute("example", registered.execute, {"value": "x"}, context(), part_id="call")
    await hooks.wrap_execute("custom", custom, {}, context(), part_id="custom")
    await asyncio.sleep(0.12)  # past the interval: the cancelled flush records nothing

    for call_id in ("call", "custom"):
        outputs = [(event["data"]["stage"], event["data"]["output"]) for event in recorded
                   if event["type"] == "tool.output" and event["context"].call_id == call_id]
        assert outputs == [("executor_stream", "first"), ("executor_result", "first second third")]
    assert recorded[-1]["type"] == "tool.finished"


@pytest.mark.asyncio
async def test_a_2_mib_output_is_recorded_within_the_cap_with_its_size_and_digest(monkeypatch, recorded):
    from trajectory import tool_output
    from trajectory.projector import replay
    monkeypatch.setattr(tool_output, "TOOL_OUTPUT_RECORD_SECONDS", 0)
    offloaded = []
    to_thread = asyncio.to_thread

    async def spy(function, *args):
        offloaded.append(function.__name__)
        return await to_thread(function, *args)
    monkeypatch.setattr(tool_output.asyncio, "to_thread", spy)
    block = "".join(f"{index:06d} ✓ output line\n" for index in range(1000))
    produced = {}

    async def execute(args, ctx):
        output = ""
        while len(output.encode()) < 2 * 1024 * 1024:
            output += block
            await ctx.update_output(output)
        produced["output"] = output
        return ToolResult(output=output)
    await allowing_hooks().wrap_execute("custom", execute, {}, context(), part_id="call")

    encoded = produced["output"].encode()
    limit = tool_output.TOOL_OUTPUT_MAX_BYTES
    outputs = [event["data"] for event in recorded if event["type"] == "tool.output"]
    stream, final = outputs[:-1], outputs[-1]
    assert sum(len(item["output"].encode()) for item in stream) <= limit
    assert stream[-1]["stream_truncated"] is True and not any(item.get("stream_truncated") for item in stream[:-1])
    assert final["stage"] == "executor_result" and final["output_truncated"] is True
    assert final["output_bytes"] == len(encoded) >= 2 * 1024 * 1024
    assert final["output_sha256"] == hashlib.sha256(encoded).hexdigest()
    head = encoded[:limit // 2].decode("utf-8", "ignore")
    tail = encoded[len(encoded) - (limit - limit // 2):].decode("utf-8", "ignore")
    omitted = len(encoded) - len(head.encode()) - len(tail.encode())
    assert final["output"] == head + tool_output.OMITTED.format(size=omitted) + tail
    assert offloaded == ["_bounded"]
    assert replay(trace_events(recorded))["records"]["tool:call"]["data"]["output"] == final["output"]


@pytest.mark.asyncio
async def test_bash_without_an_output_callback_records_each_chunk_once(monkeypatch, recorded):
    import tool.truncation
    from tool.bash import MAX_STREAM_OUTPUT, bash_tool
    from trajectory import tool_output

    async def truncate(text):
        return SimpleNamespace(content=text[:100], truncated=True)
    monkeypatch.setattr(tool.truncation, "truncate_output", truncate)
    # Every chunk is recorded (no interval, no cap), so the recorded bytes show the growth directly.
    monkeypatch.setattr(tool_output, "TOOL_OUTPUT_RECORD_SECONDS", 0)
    monkeypatch.setenv("TRAJECTORY_TOOL_OUTPUT_MAX_BYTES", str(64 * 1024 * 1024))
    chunks = [f"{index:05d} {'y' * 993}\n" for index in range(300)]

    class Sandbox:
        async def execute_stream(self, **kwargs):
            for chunk in chunks:
                yield SimpleNamespace(content=chunk)
            yield 0
    ctx = context()
    ctx.sandbox = Sandbox()
    ctx.trace_context = ctx.trace_context.derive(call_id="call")
    result = await bash_tool.execute({"command": "cat big.log"}, ctx)

    full = "".join(chunks)
    assert result.metadata["exit_code"] == 0 and len(full) > 2 * MAX_STREAM_OUTPUT
    outputs = [event["data"] for event in recorded if event["type"] == "tool.output"]
    stream = [item for item in outputs if item["stage"] == "executor_stream"]
    # Linear: the output up to the chat budget once, then each later chunk once.
    assert sum(len(item["output"]) for item in stream) == len(full)
    assert "".join(item["output"] for item in stream) == full
    assert len(stream) == len(chunks) - MAX_STREAM_OUTPUT // len(chunks[0])
    assert {item["tool"] for item in stream} == {"bash"}
    assert outputs[-1]["stage"] == "executor_result" and outputs[-1]["output"] == full


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
    tool = define_tool("example", description="Example", parameters=Args, execute=execute, sandbox_required=False, parallel_safe=True)
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
