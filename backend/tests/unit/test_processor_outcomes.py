"""Control-flow contract of a single agent step.

process_step classifies what happened in one LLM turn; the loop applies policy
to that classification. These assert the classification, since getting it wrong
either strands a run or spins it forever — and neither surfaces as an error.

The model, the persistence layer and the event bus are all stubbed: the subject
here is the branch taken, not what gets written.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agent import processor as P
from agent.processor import StepOutcome, process_step
from agent.retry import ContextOverflowError, RetryableError
from tool.tool import ToolResult


class Info:
    def __init__(self):
        self.id = "msg_assistant"
        self.error = None


class Ctx:
    """ToolContext stand-in — the step stamps message_id onto it."""

    message_id = ""
    part_id = ""


class NotAborted:
    """An abort signal that never fires.

    Needs `wait()` as well as `is_set()`: the processor races each stream chunk
    against `abort.wait()` so a stop takes effect mid-silence rather than at
    the next chunk. A never-resolving future is what "not aborted" means to
    that race.
    """

    @staticmethod
    def is_set():
        return False

    @staticmethod
    async def wait():
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def stub_side_effects(monkeypatch):
    """Neutralise persistence, events and compaction."""
    async def anoop(*a, **k):
        return None

    monkeypatch.setattr(P, "save_part", anoop)
    monkeypatch.setattr(P, "update_message_info", anoop)
    monkeypatch.setattr(P, "update_session", anoop)
    monkeypatch.setattr(P, "create_compaction", anoop)
    monkeypatch.setattr(P, "_history_for_compaction", anoop)
    monkeypatch.setattr(P.bus, "publish", lambda *a, **k: None)


def fake_stream(events=None, raises=None, raises_after=None):
    """Build a stand-in for stream_llm yielding the given events."""
    async def _stream(**kwargs):
        if raises is not None:
            raise raises
        for e in events or []:
            yield e
        if raises_after is not None:
            raise raises_after
    return _stream


def read_error() -> httpx.ReadError:
    return httpx.ReadError(
        "connection reset while reading response",
        request=httpx.Request("POST", "https://provider.example/v1/responses"),
    )


def test_actionable_validation_metadata_survives_persistence_filter():
    observed = P.persisted_tool_metadata(
        {
            "validation_failed": True,
            "retry_requires_changed_args": True,
            "failure_code": "prompt_lint_failed",
            "private_payload": {"do_not": "persist"},
        }
    )

    assert observed == {
        "validation_failed": True,
        "retry_requires_changed_args": True,
        "failure_code": "prompt_lint_failed",
    }


async def run(monkeypatch, **stream_kwargs):
    monkeypatch.setattr(P, "stream_llm", fake_stream(**stream_kwargs))
    return await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[], tools={}, model_id="test/model",
        ctx=Ctx(), hooks=None, assistant_info=Info(), sandbox=None,
        abort=NotAborted(), doom_loop_history=[],
    )


# ── plain completion ──

async def test_text_only_turn_continues(monkeypatch):
    result = await run(monkeypatch, events=[
        {"type": "text_delta", "text": "hello "},
        {"type": "text_delta", "text": "world"},
        {"type": "finish", "reason": "stop", "usage": {"input": 5, "output": 2, "total": 7}},
    ])
    assert result.outcome is StepOutcome.CONTINUE
    assert result.finish_reason == "stop"
    assert result.text == "hello world"


async def test_reasoning_is_collected_separately(monkeypatch):
    result = await run(monkeypatch, events=[
        {"type": "reasoning_delta", "text": "thinking..."},
        {"type": "text_delta", "text": "answer"},
        {"type": "finish", "reason": "stop", "usage": {}},
    ])
    assert result.reasoning == "thinking..."
    assert result.text == "answer"


async def test_streaming_parts_are_checkpointed_before_final_completion(monkeypatch):
    saved = []

    async def capture(part, *args, **kwargs):
        saved.append((part.type, part.text, kwargs.get("is_new", False)))

    monkeypatch.setattr(P, "save_part", capture)
    monkeypatch.setattr(P, "STREAM_CHECKPOINT_INTERVAL", 0)
    await run(monkeypatch, events=[
        {"type": "reasoning_delta", "text": "think "},
        {"type": "reasoning_delta", "text": "more"},
        {"type": "text_delta", "text": "hello "},
        {"type": "text_delta", "text": "world"},
        {"type": "finish", "reason": "stop", "usage": {}},
    ])

    # The penultimate full prefixes are durable while the stream is still in
    # progress; the last two writes are the ordinary final aggregates.
    assert ("reasoning", "think more", False) in saved[:-2]
    assert ("text", "hello world", False) in saved[:-1]


async def test_empty_stream_still_returns_a_result(monkeypatch):
    result = await run(monkeypatch, events=[])
    assert result.outcome is StepOutcome.CONTINUE
    assert result.finish_reason == "unknown"


# ── failure classification ──

async def test_context_overflow_asks_for_compaction(monkeypatch):
    result = await run(monkeypatch, raises=ContextOverflowError("prompt is too long"))
    assert result.outcome is StepOutcome.COMPACT
    assert result.finish_reason == "compact"


async def test_retryable_error_is_reported_not_retried(monkeypatch):
    """The step must not sleep or count attempts — that budget belongs to the
    loop, which is the only place that can see the whole run."""
    result = await run(monkeypatch, raises=RetryableError("rate limited", 429, {}))
    assert result.outcome is StepOutcome.RETRY
    assert result.retry_reason
    assert result.error


async def test_pre_event_read_error_remains_retryable(monkeypatch):
    result = await run(monkeypatch, raises=read_error())

    assert result.outcome is StepOutcome.RETRY
    assert result.retry_reason == "Network error"


async def test_first_adapter_error_event_remains_retryable(monkeypatch):
    """Real adapters normalize provider failures into an error event.

    That envelope must have the same pre-stream retry semantics as a generator
    that raises before yielding anything.
    """
    result = await run(
        monkeypatch,
        events=[{
            "type": "error",
            "error": RetryableError("service unavailable", 503, {}),
        }],
    )

    assert result.outcome is StepOutcome.RETRY
    assert result.retry_reason


async def test_adapter_error_after_visible_output_is_not_replayed(monkeypatch):
    result = await run(
        monkeypatch,
        events=[
            {"type": "text_delta", "text": "already visible"},
            {
                "type": "error",
                "error": RetryableError("service unavailable", 503, {}),
            },
        ],
    )

    assert result.outcome is StepOutcome.ERROR
    assert result.retry_reason is None


async def test_text_delta_then_read_error_is_terminal_not_replayed(monkeypatch):
    result = await run(
        monkeypatch,
        events=[{"type": "text_delta", "text": "already visible"}],
        raises_after=read_error(),
    )

    assert result.outcome is StepOutcome.ERROR
    assert result.retry_reason is None
    assert "connection reset" in (result.error or "")


async def test_tool_call_start_then_read_error_never_executes_or_retries(monkeypatch):
    executed = []

    async def execute(args, ctx):
        executed.append(args)
        return ToolResult(title="unexpected", output="unexpected")

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            return await execute_fn(args, ctx)

    monkeypatch.setattr(P, "stream_llm", fake_stream(
        events=[{"type": "tool_call_start", "index": 0, "tool": "write"}],
        raises_after=read_error(),
    ))
    result = await process_step(
        session_id="s1",
        user_id="u1",
        session=None,
        agent_def=None,
        system=[],
        llm_messages=[],
        tools={"write": SimpleNamespace(execute=execute)},
        model_id="test/model",
        ctx=Ctx(),
        hooks=Hooks(),
        assistant_info=Info(),
        sandbox=None,
        abort=NotAborted(),
        doom_loop_history=[],
    )

    assert result.outcome is StepOutcome.ERROR
    assert result.retry_reason is None
    assert executed == []


async def test_fatal_error_ends_the_run(monkeypatch):
    result = await run(monkeypatch, raises=ValueError("malformed request"))
    assert result.outcome is StepOutcome.ERROR
    assert "malformed request" in (result.error or "")


async def test_fatal_error_is_recorded_on_the_message(monkeypatch):
    monkeypatch.setattr(P, "stream_llm", fake_stream(raises=ValueError("boom")))
    info = Info()
    await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[], tools={}, model_id="test/model",
        ctx=Ctx(), hooks=None, assistant_info=info, sandbox=None,
        abort=NotAborted(), doom_loop_history=[],
    )
    assert info.error and "boom" in info.error["message"]


# ── the step owns no shared state ──

async def test_doom_loop_history_is_not_mutated(monkeypatch):
    history = []
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        {"type": "finish", "reason": "stop", "usage": {}},
    ]))
    result = await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[], tools={}, model_id="test/model",
        ctx=Ctx(), hooks=None, assistant_info=Info(), sandbox=None,
        abort=NotAborted(), doom_loop_history=history,
    )
    assert history == [], "the caller stays the only writer of doom-loop history"
    assert result.completed_tool_parts == []


async def test_identical_retry_after_actionable_validation_failure_is_blocked(monkeypatch):
    args = {
        "action": "set_segments",
        "production_id": "production_1",
        "visual_anchor": "same presenter",
        "segments": [{"ordinal": 1, "prompt": "unchanged"}],
    }
    history = [
        SimpleNamespace(
            tool="video_project",
            input=args,
            metadata={
                "validation_failed": True,
                "retry_requires_changed_args": True,
                "failure_code": "prompt_lint_failed",
            },
        )
    ]
    saved = []
    executed = False

    async def capture(part, *_capture_args, **_capture_kwargs):
        saved.append(part)

    async def should_not_execute(_args, _ctx):
        nonlocal executed
        executed = True
        return ToolResult(title="unexpected", output="unexpected")

    monkeypatch.setattr(P, "save_part", capture)
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        {
            "type": "tool_call",
            "tool": "video_project",
            "args": args,
            "call_id": "call_same_validation_failure",
            "invalid": False,
        },
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]))

    await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[],
        tools={"video_project": SimpleNamespace(execute=should_not_execute)},
        model_id="test/model", ctx=Ctx(), hooks=None,
        assistant_info=Info(), sandbox=None, abort=NotAborted(),
        doom_loop_history=history,
    )

    assert executed is False
    blocked = next(
        part for part in reversed(saved)
        if getattr(part, "tool", "") == "video_project"
    )
    assert blocked.status.value == "error"
    assert blocked.title == "Unchanged validation retry blocked"
    assert "corrected_prompt_template" in blocked.error


async def test_duration_is_reported(monkeypatch):
    result = await run(monkeypatch, events=[{"type": "finish", "reason": "stop", "usage": {}}])
    assert result.duration >= 0


async def test_conflicting_call_ids_block_the_entire_batch_before_execution(monkeypatch):
    executed = []
    saved = []

    async def capture(part, *args, **kwargs):
        saved.append(part)

    async def execute(args, ctx):
        executed.append(args)
        return ToolResult(title="unexpected", output="unexpected")

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            return await execute_fn(args, ctx)

    monkeypatch.setattr(P, "save_part", capture)
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        {"type": "tool_call", "tool": "read", "args": {"path": "a"}, "call_id": "call_reused"},
        {"type": "tool_call", "tool": "read", "args": {"path": "b"}, "call_id": "call_reused"},
        # Even a well-formed unrelated call must not run after the batch is ambiguous.
        {"type": "tool_call", "tool": "read", "args": {"path": "c"}, "call_id": "call_unique"},
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]))

    await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[],
        tools={"read": SimpleNamespace(execute=execute)},
        model_id="test/model", ctx=Ctx(), hooks=Hooks(),
        assistant_info=Info(), sandbox=None, abort=NotAborted(),
        doom_loop_history=[],
    )

    assert executed == []
    blocked = [part for part in saved if getattr(part, "title", "") == "Conflicting tool call ids blocked"]
    assert len(blocked) == 3
    assert len({part.call_id for part in blocked}) == 3
    assert all(part.status is P.ToolStatus.ERROR for part in blocked)


async def test_identical_duplicate_tool_event_executes_once(monkeypatch):
    executed = []

    async def execute(args, ctx):
        executed.append(args)
        return ToolResult(title="ok", output="ok")

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            return await execute_fn(args, ctx)

    event = {"type": "tool_call", "tool": "read", "args": {"path": "a"}, "call_id": "call_same"}
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        event,
        dict(event),
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]))

    await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[],
        tools={"read": SimpleNamespace(execute=execute)},
        model_id="test/model", ctx=Ctx(), hooks=Hooks(),
        assistant_info=Info(), sandbox=None, abort=NotAborted(),
        doom_loop_history=[],
    )

    assert executed == [{"path": "a"}]


async def test_parallel_safe_calls_overlap_and_unsafe_calls_are_barriers(monkeypatch):
    active: set[str] = set()
    timeline: list[str] = []
    max_active = 0

    async def execute(args, ctx):
        nonlocal max_active
        value = args["value"]
        if value == "unsafe":
            assert active == set()
        else:
            if value.startswith("after"):
                assert "unsafe:end" in timeline
            active.add(value)
            max_active = max(max_active, len(active))

        bound_part_id = ctx.part_id
        timeline.append(f"{value}:start")
        await asyncio.sleep(0.02)
        assert ctx.part_id == bound_part_id
        timeline.append(f"{value}:end")
        active.discard(value)
        return ToolResult(title=value, output=value)

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            ctx.part_id = part_id
            return await execute_fn(args, ctx)

    events = [
        {
            "type": "tool_call",
            "tool": tool,
            "args": {"value": value},
            "call_id": f"call_{index}",
        }
        for index, (tool, value) in enumerate(
            [
                ("safe", "before_a"),
                ("safe", "before_b"),
                ("unsafe", "unsafe"),
                ("safe", "after_a"),
                ("safe", "after_b"),
            ]
        )
    ]
    events.append({"type": "finish", "reason": "tool_calls", "usage": {}})
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=events))

    result = await process_step(
        session_id="s1",
        user_id="u1",
        session=None,
        agent_def=None,
        system=[],
        llm_messages=[],
        tools={
            "safe": SimpleNamespace(execute=execute, parallel_safe=True),
            "unsafe": SimpleNamespace(execute=execute, parallel_safe=False),
        },
        model_id="test/model",
        ctx=Ctx(),
        hooks=Hooks(),
        assistant_info=Info(),
        sandbox=None,
        abort=NotAborted(),
        doom_loop_history=[],
    )

    assert max_active == 2
    assert timeline.index("before_a:end") < timeline.index("unsafe:start")
    assert timeline.index("before_b:end") < timeline.index("unsafe:start")
    assert timeline.index("unsafe:end") < timeline.index("after_a:start")
    assert timeline.index("unsafe:end") < timeline.index("after_b:start")
    assert [part.input["value"] for part in result.completed_tool_parts] == [
        "before_a",
        "before_b",
        "unsafe",
        "after_a",
        "after_b",
    ]


async def test_hidden_exact_tool_name_cannot_execute_from_full_lookup(monkeypatch):
    executed = []
    saved = []

    async def capture(part, *args, **kwargs):
        saved.append(part)

    async def execute(args, ctx):
        executed.append(args)
        return ToolResult(title="unexpected", output="unexpected")

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            return await execute_fn(args, ctx)

    monkeypatch.setattr(P, "save_part", capture)
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        {"type": "tool_call", "tool": "image_gen", "args": {"prompt": "x"}, "call_id": "call_hidden"},
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]))

    await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[], tools={"read": SimpleNamespace(execute=execute)},
        execution_lookup={
            "read": SimpleNamespace(execute=execute),
            "image_gen": SimpleNamespace(execute=execute),
            "other_hidden_secret": SimpleNamespace(execute=execute),
        },
        step_executable_ids=frozenset({"read"}),
        provider_to_canonical={
            "read": "read",
            "image_gen": "image_gen",
            "other_hidden_secret": "other_hidden_secret",
        },
        model_id="test/model", ctx=Ctx(), hooks=Hooks(), assistant_info=Info(),
        sandbox=None, abort=NotAborted(), doom_loop_history=[],
    )

    assert executed == []
    blocked = next(part for part in reversed(saved) if getattr(part, "tool", "") == "image_gen")
    assert blocked.status is P.ToolStatus.ERROR
    assert "other_hidden_secret" not in blocked.error
    assert "Available: read" in blocked.error


async def test_native_unsafe_same_response_call_keeps_call_id_and_blocks_executor(monkeypatch):
    executed = []
    saved = []

    async def capture(part, *args, **kwargs):
        saved.append(part)

    async def execute(args, ctx):
        executed.append(args)
        return ToolResult(title="unexpected", output="unexpected")

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            return await execute_fn(args, ctx)

    monkeypatch.setattr(P, "save_part", capture)
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        {
            "type": "tool_call",
            "tool": "video_generate",
            "wire_tool": "video_generate",
            "args": {"prompt": "x"},
            "call_id": "call_native_unsafe",
            "stream_seq": 3,
            "native_same_response_executable": False,
            "native_error_code": "deferred_until_next_step",
        },
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]))

    await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[], tools={},
        execution_lookup={"video_generate": SimpleNamespace(execute=execute)},
        step_executable_ids=frozenset({"video_generate"}),
        provider_to_canonical={"video_generate": "video_generate"},
        model_id="test/model", ctx=Ctx(), hooks=Hooks(), assistant_info=Info(),
        sandbox=None, abort=NotAborted(), doom_loop_history=[],
    )

    assert executed == []
    blocked = next(
        part for part in reversed(saved)
        if getattr(part, "tool", "") == "video_generate"
    )
    assert blocked.call_id == "call_native_unsafe"
    assert blocked.stream_seq == 3
    assert blocked.metadata["failure_code"] == "deferred_until_next_step"
    assert "No executor was entered" in blocked.error


@pytest.mark.parametrize(
    (
        "tool_id",
        "same_response",
        "native_error",
        "expected_executions",
        "expect_rejection",
    ),
    [
        ("read", True, None, 1, False),
        ("video_generate", False, "deferred_until_next_step", 0, False),
        # A provider event cannot promote an unaudited catalogue entry.
        ("video_generate", True, None, 0, True),
    ],
)
async def test_native_reveal_is_committed_in_order_before_response_local_execution(
    monkeypatch,
    tool_id,
    same_response,
    native_error,
    expected_executions,
    expect_rejection,
):
    from agent.native_tool_search import (
        NativeCapabilityKey,
        build_openai_responses_native_plan,
    )
    from agent.tool_exposure import ExposurePlan, build_eligible_catalog
    from session.internal_parts import ProviderCapabilityBinding
    from tool.tool import ToolContext, ToolInfo
    import session.internal_parts as internal

    async def execute(args, ctx):
        executions.append(args)
        return ToolResult(title="ok", output="ok")

    info = ToolInfo(
        id=tool_id,
        description=f"deferred {tool_id}",
        parameters=SimpleNamespace,
        execute=execute,
        canonical_id=tool_id,
        provider_name=tool_id,
        source="builtin",
        plane="platform",
    )
    # build_eligible_catalog only needs a pydantic-like parameters class for
    # schema normalization; reuse the small model from a real built-in shape.
    from pydantic import BaseModel

    class Args(BaseModel):
        value: str = "x"

    info.parameters = Args
    search = ToolInfo(
        id="capability_search",
        description="search",
        parameters=Args,
        execute=execute,
        canonical_id="capability_search",
        provider_name="capability_search",
        source="builtin",
        plane="platform",
    )
    catalog = build_eligible_catalog({"capability_search": search, tool_id: info})
    plan = build_openai_responses_native_plan(
        catalog,
        ExposurePlan(
            direct_ids=("capability_search",),
            deferred_ids=(tool_id,),
            discovery_ids=(tool_id,),
            reasons={"capability_search": "resident"},
            strategy="portable",
            schema_chars=0,
        ),
    )
    binding = ProviderCapabilityBinding(
        provider="openai",
        endpoint="https://api.openai.com/path:test",
        account_id="account-config:test",
        api_version="2025-03-01-preview",
        model="openai/gpt-5.4",
        dialect="responses",
    )
    ctx = ToolContext(
        session_id="s1",
        user_id="u1",
        agent_id="build",
        _capability_catalog=catalog,
        _native_tool_plan=plan,
        _native_binding=binding,
        _native_capability_key=NativeCapabilityKey(
            "openai_responses_tool_search_v1", binding.digest(), "cfg"
        ),
    )
    executions = []
    committed = []
    internal_rows = []
    public_parts = []

    async def save_internal(**kwargs):
        internal_rows.append(kwargs)
        return SimpleNamespace(id=f"internal-{len(internal_rows)}")

    async def commit(event, **kwargs):
        committed.append((event, kwargs))

    async def save_public(part, *args, **kwargs):
        public_parts.append(part)

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            return await execute_fn(args, ctx)

    monkeypatch.setattr(internal, "save_internal_part", save_internal)
    monkeypatch.setattr(internal, "commit_tool_reveal", commit)
    monkeypatch.setattr(P, "save_part", save_public)
    monkeypatch.setattr(P, "update_message_info", save_public)
    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        {
            "type": "native_search_started",
            "raw_item": {"type": "tool_search_call", "execution": "server"},
            "response_chain_id": "resp-1",
            "stream_seq": 0,
        },
        {
            "type": "native_search_result",
            "raw_item": {"type": "tool_search_output", "execution": "server", "status": "completed", "tools": []},
            "response_chain_id": "resp-1",
            "stream_seq": 1,
        },
        {
            "type": "native_tool_revealed",
            "raw_item": {"type": "tool_revealed", "tool": tool_id},
            "canonical_tool_id": tool_id,
            "wire_tool_name": tool_id,
            "same_response_executable": same_response,
            "response_chain_id": "resp-1",
            "stream_seq": 2,
        },
        {
            "type": "tool_call",
            "tool": tool_id,
            "wire_tool": tool_id,
            "args": {"value": "ok"},
            "call_id": "call-1",
            "stream_seq": 3,
            "native_same_response_executable": same_response,
            "native_error_code": native_error,
        },
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]))

    result = await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[], tools={}, model_id="openai/gpt-5.4",
        ctx=ctx, hooks=Hooks(), assistant_info=Info(), sandbox=None,
        abort=NotAborted(), doom_loop_history=[],
        execution_lookup={tool_id: info},
        step_executable_ids=frozenset(),
        provider_to_canonical={tool_id: tool_id},
        provider_binding_digest=binding.digest(),
        provider_dialect="responses",
    )

    if expect_rejection:
        assert result.outcome is P.StepOutcome.ERROR
        assert "unauthorized same-response" in result.error
        assert [row["stream_seq"] for row in internal_rows] == [0, 1]
        assert committed == []
        assert executions == []
        return

    assert [row["stream_seq"] for row in internal_rows] == [0, 1, 2]
    assert len(committed) == 1
    assert committed[0][0].canonical_tool_id == tool_id
    assert len(executions) == expected_executions
    tool_part = next(part for part in reversed(public_parts) if getattr(part, "tool", "") == tool_id)
    assert tool_part.call_id == "call-1"
    assert tool_part.stream_seq == 3
    if not same_response:
        assert tool_part.metadata["failure_code"] == "deferred_until_next_step"


async def test_wire_name_executes_under_canonical_permission_identity(monkeypatch):
    authorized = []
    executed = []

    async def execute(args, ctx):
        executed.append(args)
        return ToolResult(title="ok", output="ok")

    class Hooks:
        async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
            authorized.append(tool_name)
            return await execute_fn(args, ctx)

    monkeypatch.setattr(P, "stream_llm", fake_stream(events=[
        {"type": "tool_call", "tool": "mcp_wire", "args": {"value": 1}, "call_id": "call_mcp"},
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]))
    canonical = "mcp:v2:" + "a" * 52
    await process_step(
        session_id="s1", user_id="u1", session=None, agent_def=None,
        system=[], llm_messages=[], tools={"mcp_wire": SimpleNamespace(execute=execute)},
        execution_lookup={canonical: SimpleNamespace(execute=execute)},
        step_executable_ids=frozenset({canonical}),
        provider_to_canonical={"mcp_wire": canonical},
        model_id="test/model", ctx=Ctx(), hooks=Hooks(), assistant_info=Info(),
        sandbox=None, abort=NotAborted(), doom_loop_history=[],
    )

    assert authorized == [canonical]
    assert executed == [{"value": 1}]
