"""Production adapter/executor boundaries, independent of external providers."""
import asyncio
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


def test_request_snapshot_uses_allowlist_and_omits_private_provider_state():
    snapshot = request_snapshot({
        "model": "provider/model", "api_key": "never-persist", "headers": {"Authorization": "secret"},
        "messages": [{"role": "assistant", "content": "visible", "encrypted_content": "hidden",
                      "_responses_input_items": [{"secret": "private"}]}],
        "extra_body": {"reasoning_effort": "high", "access_token": "never-persist"},
        "custom_auth_option": "never-persist", "_hidden_params": {"key": "secret"},
    })
    assert snapshot["messages"] == [{"role": "assistant", "content": "visible"}]
    assert snapshot["extra_body"] == {"reasoning_effort": "high"}
    assert "never-persist" not in str(snapshot)
    assert "custom_auth_option" in snapshot["omitted_fields"]


@pytest.mark.asyncio
async def test_each_adapter_dispatch_has_identity_and_persists_chunks_before_delivery(monkeypatch, recorded):
    import litellm
    from agent import llm
    calls = []
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {"api_key": "not-in-trace"})
    monkeypatch.setattr(llm, "_get_variant_kwargs", lambda *_: {})
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda _: 100)

    class Chunk(SimpleNamespace):
        def model_dump(self, **_):
            return {"choices": [{"delta": {"content": self.choices[0].delta.content}}],
                    "_hidden_params": {"api_key": "not-in-trace"}}

    async def stream():
        for text in ("A", "B"):
            yield Chunk(choices=[SimpleNamespace(index=0, finish_reason=None,
                delta=SimpleNamespace(content=text, reasoning_content=None, tool_calls=[]))], usage=None)

    async def completion(**kwargs):
        assert recorded[-1]["type"] == "request.started"
        calls.append(kwargs)
        return stream()
    monkeypatch.setattr(litellm, "acompletion", completion)
    ctx = context()
    for attempt in range(2):
        async for event in llm._stream_litellm_direct("provider/model", [],
                [{"role": "user", "content": "question"}], {}, trace_ctx=ctx):
            if event["type"] == "text_delta":
                assert any(item["type"] == "request.delta" and
                    item["data"]["blocks"][0]["delta"] == event["text"] for item in recorded)
    assert len(calls) == 2
    starts = [e for e in recorded if e["type"] == "request.started"]
    assert len({e["context"].request_id for e in starts}) == 2
    assert {e["context"].step_id for e in starts} == {"step"}
    assert [e["data"]["chunk_index"] for e in recorded if e["type"] == "request.delta"] == [1, 2, 1, 2]
    assert "not-in-trace" not in str(recorded)


@pytest.mark.asyncio
async def test_failed_recording_never_dispatches_provider(monkeypatch, recorded):
    import litellm
    import trajectory
    from trajectory.types import TrajectoryError
    from agent import llm
    dispatched = False

    async def failed_record(*args, **kwargs):
        raise TrajectoryError("recording unavailable")

    async def completion(**kwargs):
        nonlocal dispatched
        dispatched = True
    monkeypatch.setattr(trajectory, "record", failed_record)
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {})
    with pytest.raises(TrajectoryError):
        async for _ in llm._stream_litellm_direct("provider/model", [], [], {}, trace_ctx=context()):
            pass
    assert not dispatched


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
async def test_stream_reader_can_fill_one_batch_before_first_receipt(monkeypatch, recorded):
    import trajectory
    from agent.trajectory import RequestCapture
    capture = await RequestCapture.start(context(), purpose="chat", model_id="provider/model",
        payload={"model": "provider/model"}, capture_level="adapter_input")
    receipts = []
    queued_all = asyncio.Event()

    def deferred_receipt(context, event):
        receipt = asyncio.get_running_loop().create_future()
        receipts.append(receipt)
        if len(receipts) == 3:
            queued_all.set()
        return receipt
    monkeypatch.setattr(trajectory, "record_stream", deferred_receipt)

    async def provider():
        for text in ("one", "two", "three"):
            yield {"type": "content", "delta": text}
    stream = capture.stream_chunks(provider(), lambda chunk: [{"type": "text", "delta": chunk["delta"]}])
    delivery = asyncio.create_task(anext(stream))
    await asyncio.wait_for(queued_all.wait(), timeout=1)
    assert not delivery.done()
    for receipt in receipts:
        receipt.set_result({"committed": True})
    assert (await delivery)["delta"] == "one"
    assert (await anext(stream))["delta"] == "two"
    assert (await anext(stream))["delta"] == "three"
    await stream.aclose()


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
