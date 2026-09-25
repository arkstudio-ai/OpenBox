"""Compaction follows the conversation model and never commits partial summaries."""
import asyncio
import json

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import update

from agent import compaction
from db.base import get_db_session
from db.models.message import Message
from models.message import TokenUsage
from session.agent_event_log import load_canonical_model_surface
from session.session import get_messages
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_full_history import _seed
from tests.unit.test_long_history_context import long_chat, _send  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401


@pytest.mark.parametrize("trigger", ["automatic", "provider-overflow", "manual"])
async def test_compaction_and_continuation_use_the_effective_agent_model(
    state, long_chat, monkeypatch, trigger,
):
    from agent.agent import AgentDef
    from api import sessions as api
    from session.session import get_session
    from tests.unit.test_long_history_context import USER, _drain_tasks

    effective_model = "test/agent-model"
    monkeypatch.setattr(long_chat.loop, "get_agent", lambda name: AgentDef(
        name=name, description="Regression agent", model=effective_model,
    ))
    # This model is smaller than the session's GPT-4o. The pre-request check
    # must resolve the agent override before choosing the threshold.
    monkeypatch.setattr(compaction, "get_model_context_limit", lambda model: (
        60000 if model == effective_model else 128000
    ))
    ids = await _seed("s1", "u1", turns=5)
    async with get_db_session() as db:
        await db.execute(update(Message).where(Message.id == ids[-1]).values(
            tokens={"input": 50000 if trigger == "automatic" else 500, "output": 1},
        ))
    calls = []

    async def provider(**kwargs):
        calls.append(("model", kwargs["model_id"]))
        if trigger == "provider-overflow" and len(calls) == 1:
            yield {"type": "error", "error": "maximum context length exceeded"}
        else:
            yield {"type": "text_delta", "text": "Done."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 500, "output": 10}}

    async def summary(**kwargs):
        calls.append(("summary", kwargs["model_id"]))
        yield {"type": "text_delta", "text": "Earlier work summarized."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 500, "output": 10}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    monkeypatch.setattr("agent.llm.stream_llm", summary)
    if trigger == "manual":
        tasks = BackgroundTasks()
        await api.summarize_session("s1", tasks, current_user=USER)
        await asyncio.wait_for(tasks(), timeout=20)
        await _drain_tasks(long_chat)
        expected = ["summary"]
    else:
        await _send(long_chat, "Continue the current task")
        expected = (["summary", "model"] if trigger == "automatic"
                    else ["model", "summary", "model"])
    assert calls == [(kind, effective_model) for kind in expected]
    # An agent override remains temporary, even across compaction.
    assert (await get_session("s1", user_id="u1")).model != effective_model


@pytest.mark.parametrize("previous_input", [0, 12000, 127000])
async def test_automatic_compaction_uses_fresh_usage_and_does_not_repeat_after_success(
    state, long_chat, monkeypatch, previous_input,
):
    ids = await _seed("s1", "u1", turns=5)
    async with get_db_session() as db:
        await db.execute(update(Message).where(Message.id == ids[-1]).values(
            tokens={"input": previous_input, "output": 1},
        ))
    calls = []

    async def summary(**kwargs):
        calls.append("summary")
        yield {"type": "text_delta", "text": "Earlier work summarized."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 500, "output": 10}}

    async def provider(**kwargs):
        calls.append("model")
        if previous_input < 108000 and len(calls) == 1:
            yield {"type": "tool_call", "tool": "read", "args": {}, "call_id": "grow-context"}
            yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 127000, "output": 10}}
        else:
            yield {"type": "text_delta", "text": "Task completed."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 20}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    monkeypatch.setattr("agent.llm.stream_llm", summary)
    await _send(long_chat, "Continue with the current request")
    assert calls == (["summary", "model"] if previous_input >= 108000
                     else ["model", "summary", "model"])


@pytest.mark.parametrize("raised", [False, True], ids=["error-event", "exception"])
async def test_provider_overflow_compacts_and_resumes(state, long_chat, monkeypatch, raised):
    from agent.retry import ContextOverflowError

    await _seed("s1", "u1", turns=5)
    calls = []

    async def provider(**kwargs):
        calls.append("model")
        if len(calls) == 1:
            if raised:
                raise ContextOverflowError("maximum context length exceeded")
            yield {"type": "error", "error": "maximum context length exceeded"}
        else:
            yield {"type": "text_delta", "text": "Recovered and completed."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 10}}

    async def summary(**kwargs):
        calls.append("summary")
        yield {"type": "text_delta", "text": "A compact summary of earlier work."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 500, "output": 10}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    monkeypatch.setattr("agent.llm.stream_llm", summary)
    await _send(long_chat, "Continue the task")
    assert calls == ["model", "summary", "model"]


@pytest.mark.parametrize("tokens,auto,expected", [(102399, True, False), (102400, True, True),
                                                (127000, False, False)])
async def test_configured_auto_threshold_and_disable_switch(long_chat, tokens, auto, expected):
    from core.config import get_config

    get_config().compaction.auto = auto
    assert await compaction.is_overflow(TokenUsage(input=tokens), "openai/gpt-4o") is expected


async def test_chunks_use_the_conversation_model_and_its_budget(monkeypatch):
    from core.config import get_config

    config = get_config().model_copy(deep=True)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(compaction, "get_model_context_limit", lambda _model: 30000)
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs)
        yield {"type": "text_delta", "text": "A brief chunk summary."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    messages = [{"role": "user", "content": f"Message {n}: " + "x" * 12000} for n in range(10)]
    await compaction._chunked_summarize(messages, "test/large-main", 25700, "s", "u")
    assert len(requests) > 1
    for request in requests:
        assert request["model_id"] == "test/large-main"
        assert compaction._compaction_input_tokens(request["messages"]) <= 25700
    assert json.loads("".join(request["messages"][0]["content"] for request in requests)) == messages


def test_oversized_tool_message_is_split_without_losing_arguments_or_results():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"quoted": '"中文\\' * 1000})}}]},
        {"role": "tool", "tool_call_id": "c", "content": "Large tool result " * 1000},
    ]
    requests = compaction._chunk_requests(messages, 500)
    assert len(requests) > 1
    assert all(compaction._compaction_input_tokens(request) <= 500 for request in requests)
    assert all(message["role"] == "user" for request in requests for message in request)
    assert json.loads("".join(request[0]["content"] for request in requests)) == messages


async def test_all_chunks_and_final_summary_use_the_same_model(state, monkeypatch):
    from core.config import get_config

    config = get_config().model_copy(deep=True)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    ids = await _seed("s1", "u1", turns=10, tool_output="Important source data " * 400)
    await compaction.create_compaction("s1", auto=False, user_id="u1", model_id="test/main")
    monkeypatch.setattr(compaction, "get_model_context_limit", lambda _model: 6000)
    calls = []

    async def provider(**kwargs):
        calls.append((kwargs["billing_kind"], kwargs["model_id"]))
        yield {"type": "text_delta", "text": "Earlier work summarized."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 500, "output": 10}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    await compaction.process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "test/main", auto=False, user_id="u1",
    )
    assert len(calls) > 2
    assert calls[:-1] == [("compaction_chunk", "test/main")] * (len(calls) - 1)
    assert calls[-1] == ("compaction", "test/main")
    surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert ids[0] not in {message.id for message in surface.messages}
    assert any(message.summary and message.finish for message in surface.messages)
    assert set(ids) <= {message.id for message in await get_messages("s1", user_id="u1")}


@pytest.mark.parametrize("failure", ["error-event", "exception", "empty", "truncated", "interrupted"])
async def test_failed_chunk_never_replaces_original_history(state, monkeypatch, failure):
    ids = await _seed("s1", "u1", turns=10, tool_output="Important source data " * 400)
    await compaction.create_compaction("s1", auto=False, user_id="u1")
    monkeypatch.setattr(compaction, "get_model_context_limit", lambda _model: 6000)
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs["billing_kind"])
        if kwargs["billing_kind"] == "compaction_chunk":
            if failure == "error-event":
                yield {"type": "error", "error": "Summary service unavailable"}
            elif failure == "exception":
                raise RuntimeError("Summary service unavailable")
            elif failure in {"truncated", "interrupted"}:
                yield {"type": "text_delta", "text": "Only a partial chunk summary"}
                if failure == "truncated":
                    yield {"type": "finish", "reason": "length", "usage": {}}
            else:
                yield {"type": "finish", "reason": "stop", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "A plausible but incomplete final summary."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    assert await compaction.process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "test/main", auto=False, user_id="u1",
    ) == "stop"
    assert calls == ["compaction_chunk"]
    surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert set(ids) <= {message.id for message in surface.messages}
    assert not any(message.summary and message.finish for message in surface.messages)


@pytest.mark.parametrize("failure", ["length", "no-finish", "whitespace"])
async def test_incomplete_final_summary_keeps_the_original_context(state, monkeypatch, failure):
    ids = await _seed("s1", "u1", turns=5)
    await compaction.create_compaction("s1", auto=False, user_id="u1")

    async def provider(**kwargs):
        yield {"type": "text_delta", "text": "   " if failure == "whitespace" else "Only a partial summary"}
        if failure != "no-finish":
            yield {"type": "finish", "reason": "length" if failure == "length" else "stop", "usage": {}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    assert await compaction.process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "openai/gpt-4o", auto=False, user_id="u1",
    ) == "stop"
    surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert set(ids) <= {message.id for message in surface.messages}
    summary_attempt = (await get_messages("s1", user_id="u1"))[-1]
    assert summary_attempt.error and not summary_attempt.finish


@pytest.mark.parametrize("phase", ["final", "chunk"])
async def test_stop_interrupts_a_silent_summary_and_preserves_history(state, monkeypatch, phase):
    ids = await _seed("s1", "u1", turns=5,
                      tool_output="Source facts " * 800 if phase == "chunk" else None)
    await compaction.create_compaction("s1", auto=True, user_id="u1")
    if phase == "chunk":
        monkeypatch.setattr(compaction, "get_model_context_limit", lambda _model: 6000)
    started, closed, abort = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def silent_provider(**kwargs):
        calls.append(kwargs["billing_kind"])
        started.set()
        try:
            await asyncio.Event().wait()  # No next token to wake an `async for`.
            yield {"type": "finish", "reason": "stop", "usage": {}}
        finally:
            closed.set()

    monkeypatch.setattr("agent.llm.stream_llm", silent_provider)
    task = asyncio.create_task(compaction.process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "openai/gpt-4o",
        user_id="u1", abort=abort,
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        abort.set()
        assert await asyncio.wait_for(task, timeout=2) == "stop"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set()
    assert calls == ["compaction_chunk" if phase == "chunk" else "compaction"]
    surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert set(ids) <= {message.id for message in surface.messages}
    attempt = (await get_messages("s1", user_id="u1"))[-1]
    assert attempt.finish == "aborted" and not attempt.summary and not attempt.error


async def test_abort_api_stops_compaction_and_next_input_can_run(state, long_chat, monkeypatch):
    from agent import llm
    from api import sessions as api
    from core.config import get_config, ModelConfig
    from tests.unit.test_long_history_context import USER, _drain_tasks
    from session.session import get_session

    cfg = get_config()
    cfg.models = [ModelConfig(id="openai/gpt-4o", context_limit=30000)]
    cfg.compaction.max_tokens = 1024
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda _model: 2048)
    # Real source size must still cross the threshold after the aborted
    # attempt, even though that attempt has no provider usage of its own.
    await _seed("s1", "u1", turns=5, tool_output="1 " * 2500)
    started, closed = asyncio.Event(), asyncio.Event()
    summaries = []

    async def summary(**kwargs):
        summaries.append(kwargs)
        if len(summaries) == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        yield {"type": "text_delta", "text": "Original facts preserved. Resume the newest task."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 500, "output": 20}}

    async def provider(**kwargs):
        yield {"type": "text_delta", "text": "Task resumed after the stop."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 500, "output": 20}}

    monkeypatch.setattr("agent.llm.stream_llm", summary)
    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await api.send_message_async("s1", api.PromptBody(text="First task"), current_user=USER)
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.wait_for(api.abort_session("s1", current_user=USER), timeout=3)
    await _drain_tasks(long_chat)
    assert closed.is_set()
    assert (await get_session("s1", user_id="u1")).status == "idle"
    await _send(long_chat, "Resume with the latest task", asynchronous=True)
    assert len(summaries) == 2
    assert (await get_messages("s1", user_id="u1"))[-1].finish == "stop"


async def test_cancelling_the_summary_consumer_closes_the_provider(monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()

    async def provider(**_kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield {"type": "text_delta", "text": "Must not escape the cancelled turn"}
        finally:
            closed.set()

    monkeypatch.setattr("agent.llm.stream_llm", provider)

    async def consume():
        async for _event in compaction._summary_stream(abort=asyncio.Event()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert closed.is_set()
