"""A provider stream that breaks off part-way is replayed, not surfaced."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent import processor as P
from tool.tool import ToolResult


INTERRUPTED = (
    "litellm.ServiceUnavailableError: litellm.MidStreamFallbackError: "
    "litellm.APIConnectionError: APIConnectionError: OpenAIException - "
    "Internal server error. Please retry your request."
)


class _Info:
    id = "msg_replay"
    error = None


class _Ctx:
    message_id = ""
    part_id = ""


class _NotAborted:
    @staticmethod
    def is_set():
        return False

    @staticmethod
    async def wait():
        await asyncio.Event().wait()


class _Hooks:
    async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
        return await execute_fn(args, ctx)


def _error():
    return {"type": "error", "error": RuntimeError(INTERRUPTED)}


async def _run_step(monkeypatch, attempts, *, saved, executed=None):
    """Run one step against a provider that answers with ``attempts`` in turn."""
    calls = []

    def stream_llm(**_kwargs):
        events = attempts[min(len(calls), len(attempts) - 1)]
        calls.append(events)

        async def generate():
            for event in events:
                yield event

        return generate()

    async def capture(part, *_args, **_kwargs):
        saved.append(part.model_copy(deep=True))

    async def execute(args, _ctx):
        if executed is not None:
            executed.append(args)
        return ToolResult(title="ok", output="done")

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(P, "update_message_info", noop)
    monkeypatch.setattr(P, "update_session", noop)
    monkeypatch.setattr(P, "create_compaction", noop)
    monkeypatch.setattr(P.bus, "publish", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(P, "retry_delay", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(P, "save_part", capture)
    monkeypatch.setattr(P, "stream_llm", stream_llm)
    tool = SimpleNamespace(execute=execute)
    info = _Info()
    result = await P.process_step(
        session_id="s1",
        user_id="u1",
        session=None,
        agent_def=SimpleNamespace(name="build"),
        system=[],
        llm_messages=[],
        tools={"read": tool},
        model_id="gateway/model-a",
        ctx=_Ctx(),
        hooks=_Hooks(),
        assistant_info=info,
        sandbox=None,
        abort=_NotAborted(),
        doom_loop_history=[],
        execution_lookup={"canonical:read": tool},
        step_executable_ids=frozenset({"canonical:read"}),
        provider_to_canonical={"read": "canonical:read"},
        provider_binding_digest="b" * 64,
        provider_dialect="litellm",
    )
    return result, calls, info


def _texts(saved):
    return [part.text for part in saved if part.type == "text"]


@pytest.mark.asyncio
async def test_interrupted_text_is_withdrawn_and_replayed(monkeypatch):
    saved = []
    result, calls, info = await _run_step(monkeypatch, [
        [{"type": "text_delta", "text": "half an ans"}, _error()],
        [{"type": "text_delta", "text": "the whole answer"},
         {"type": "finish", "reason": "stop", "usage": {"output": 4}}],
    ], saved=saved)

    assert len(calls) == 2
    assert result.outcome is P.StepOutcome.CONTINUE
    assert result.finish_reason == "stop"
    assert result.text == "the whole answer"
    assert info.error is None
    # One text part throughout: shown, emptied, then finished.
    assert len({part.id for part in saved if part.type == "text"}) == 1
    assert _texts(saved) == ["half an ans", "", "the whole answer"]


@pytest.mark.asyncio
async def test_replayed_tool_call_reuses_the_card_and_runs_once(monkeypatch):
    saved, executed = [], []
    call = {"type": "tool_call", "tool": "read", "wire_tool": "read",
            "args": {"path": "a"}, "call_id": "call_2"}
    result, calls, _info = await _run_step(monkeypatch, [
        [{"type": "tool_call_start", "index": 0, "tool": "read", "call_id": "call_1"},
         {"type": "tool_call_args_delta", "index": 0, "delta": '{"pa'}, _error()],
        [{"type": "tool_call_start", "index": 0, "tool": "read", "call_id": "call_2"},
         call, {"type": "finish", "reason": "tool_calls", "usage": {}}],
    ], saved=saved, executed=executed)

    assert len(calls) == 2
    assert result.outcome is P.StepOutcome.CONTINUE
    assert executed == [{"path": "a"}]
    tool_parts = [part for part in saved if part.type == "tool"]
    assert len({part.id for part in tool_parts}) == 1
    assert [part.status.value for part in tool_parts] == [
        "pending", "pending", "running", "completed",
    ]


@pytest.mark.asyncio
async def test_card_the_replay_does_not_reuse_is_closed_as_not_started(monkeypatch):
    saved, executed = [], []
    result, _calls, _info = await _run_step(monkeypatch, [
        [{"type": "tool_call_start", "index": 0, "tool": "read", "call_id": "call_1"}, _error()],
        [{"type": "text_delta", "text": "no tool after all"},
         {"type": "finish", "reason": "stop", "usage": {"output": 3}}],
    ], saved=saved, executed=executed)

    assert result.finish_reason == "stop"
    assert executed == []
    tool_parts = [part for part in saved if part.type == "tool"]
    assert [part.status.value for part in tool_parts] == ["pending", "error"]
    assert tool_parts[-1].tool == "read"
    assert tool_parts[-1].metadata["recovery_code"] == "tool_not_started"


@pytest.mark.asyncio
async def test_replays_are_bounded_and_the_user_sees_a_plain_message(monkeypatch):
    saved = []
    result, calls, info = await _run_step(monkeypatch, [
        [{"type": "text_delta", "text": "half"}, _error()],
    ], saved=saved)

    assert len(calls) == P.MAX_STREAM_REPLAYS + 1
    assert result.outcome is P.StepOutcome.ERROR
    assert result.error == P.STREAM_INTERRUPTED_MESSAGE
    assert info.error == {"message": P.STREAM_INTERRUPTED_MESSAGE}
    assert "litellm" not in result.error


@pytest.mark.asyncio
async def test_failure_before_any_output_still_uses_the_loop_retry_budget(monkeypatch):
    saved = []
    result, calls, _info = await _run_step(monkeypatch, [[_error()]], saved=saved)

    assert len(calls) == 1
    assert result.outcome is P.StepOutcome.RETRY


@pytest.mark.asyncio
async def test_non_transient_failure_is_not_replayed(monkeypatch):
    saved = []
    result, calls, _info = await _run_step(monkeypatch, [
        [{"type": "text_delta", "text": "half"},
         {"type": "error", "error": RuntimeError("模型回复未完整结束（length）。")}],
    ], saved=saved)

    assert len(calls) == 1
    assert result.outcome is P.StepOutcome.ERROR
    assert result.error == "模型回复未完整结束（length）。"
