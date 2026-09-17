"""Exercise main's 200-message fix through the refactored API and real loop.

Only external model/sandbox calls are replaced. Acceptance, event replay,
tool execution, compaction, persistence and pagination remain real. The
shared state fixture also runs these cases on isolated PostgreSQL schemas.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import event

import db.base as database
from api import sessions as api
from core.config import CompactionConfig
from session.agent_event_log import load_canonical_model_surface
from session.session import get_messages, get_session
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_full_history import _seed
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401
from tool.tool import ToolResult, define_tool


USER = {"user_id": "u1", "workspace_id": "w1"}
TOOL_OUTPUT = "Result produced after the first 402 messages."


@pytest.fixture
def long_chat(loop_harness, monkeypatch):
    from core.config import get_config

    config = get_config()
    config.compaction = CompactionConfig(tail_turns=2, preserve_recent_tokens=500)
    monkeypatch.setattr(api, "get_config", lambda: config)
    monkeypatch.setattr(api, "_remember_prompt_history", lambda *args: None)
    monkeypatch.setattr(api, "_background_tasks", set())
    monkeypatch.setattr(loop_harness.loop, "_background_tasks", set())
    monkeypatch.setattr(loop_harness.loop, "_ensure_title", AsyncMock())
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=[]))

    class ReadArgs(BaseModel):
        pass

    async def read(_args, _ctx):
        return ToolResult(title="Long-history check", output=TOOL_OUTPUT)

    loop_harness.tools["read"] = define_tool(
        "read", description="Read a regression fixture", parameters=ReadArgs,
        execute=read, sandbox_required=False,
    )
    return loop_harness


def _content(request):
    return json.dumps(request["messages"], ensure_ascii=False)


async def _drain_tasks(harness):
    for tasks in (api._background_tasks, harness.loop._background_tasks):
        while tasks:
            await asyncio.wait_for(asyncio.gather(*list(tasks)), timeout=20)


async def _send(harness, text, *, asynchronous=False):
    endpoint = api.send_message_async if asynchronous else api.send_message
    response = await asyncio.wait_for(endpoint(
        "s1", api.PromptBody(text=text, model="openai/gpt-4o"), current_user=USER,
    ), timeout=20)
    await _drain_tasks(harness)
    return response


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync-api", "web-async-api"])
async def test_over_200_turns_accepts_new_input_tool_result_and_next_turn(
    state, long_chat, monkeypatch, asynchronous,
):
    ids = await _seed("s1", "u1", turns=201)
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            yield {"type": "tool_call", "tool": "read", "args": {}, "call_id": "read-after-402"}
            yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 2000, "output": 10}}
        else:
            yield {"type": "text_delta", "text": "Accepted the latest input."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 2500, "output": 20}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await _send(long_chat, "First new input after 201 turns", asynchronous=asynchronous)
    assert len(requests) == 2
    assert "问题 0" in _content(requests[0])
    assert "问题 200" in _content(requests[0])
    assert "First new input after 201 turns" in _content(requests[0])
    assert any(message["role"] == "tool" and TOOL_OUTPUT in str(message["content"])
               for message in requests[1]["messages"])

    await _send(long_chat, "Another new input after the completed run", asynchronous=asynchronous)
    assert len(requests) == 3
    assert "Another new input after the completed run" in _content(requests[-1])
    assert (await get_session("s1", user_id="u1")).status.value == "idle"

    public = await get_messages("s1", user_id="u1")
    surface = await load_canonical_model_surface("s1", user_id="u1")
    assert [message.id for message in surface.messages] == [message.id for message in public]
    assert len(public) == 407
    first_page = await api.get_messages("s1", current_user=USER)
    assert [message["id"] for message in first_page] == ids[:200]
    latest = await api.get_history("s1", turns=8, current_user=USER)
    assert latest["has_more"]
    assert latest["messages"][-1]["id"] == public[-1].id
    catchup = await api.get_history("s1", after=ids[-1], current_user=USER)
    assert [message["id"] for message in catchup["messages"]] == [message.id for message in public[401:]]


async def test_legacy_bootstrap_does_not_exhaust_the_database_bind_limit(state):
    engine = database._engine
    sqlite = engine.dialect.name == "sqlite"
    # asyncpg's protocol caps a statement at 32767 arguments. On SQLite,
    # lower the connection's real limit so the same failure is cheap in CI.
    turns = 201 if sqlite else 16384
    ids = await _seed("s1", "u1", turns=turns)

    def bound_connection(connection, _record):
        import sqlite3

        connection.run_async(lambda driver: driver._execute(
            driver._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 256,
        ))

    if sqlite:
        await engine.dispose()
        event.listen(engine.sync_engine, "connect", bound_connection)
    try:
        # main's full-history reader already avoids a growing IN list. The
        # refactor's first canonical read of an existing chat must do so too.
        public = await get_messages("s1", user_id="u1")
        assert [message.id for message in public] == ids
        surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
        assert [message.id for message in surface.messages] == ids
        assert all(len(message.parts) == 1 for message in surface.messages)
    finally:
        if sqlite:
            event.remove(engine.sync_engine, "connect", bound_connection)
            await engine.dispose()


async def test_ask_answer_after_200_turns_reaches_the_resumed_model_once(state, long_chat, monkeypatch):
    from api import questions
    from question.continuation import QuestionContinuationWorker
    from tool.question_tool import question_tool

    await _seed("s1", "u1", turns=201)
    long_chat.tools["question"] = question_tool
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            yield {"type": "tool_call", "tool": "question", "call_id": "ask-after-402", "args": {
                "questions": [{"question": "Which subtitle version?", "options": [{"label": "Version B"}]}],
            }}
            yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 2000, "output": 10}}
        else:
            yield {"type": "text_delta", "text": "Use the chosen subtitle version."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 2500, "output": 20}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await _send(long_chat, "Ask which subtitle version to use")
    assert len(requests) == 1
    assert (await get_session("s1", user_id="u1")).status.value == "waiting_input"
    pending = await questions.list_questions(current_user=USER)
    assert len(pending) == 1
    await questions.reply_question(
        pending[0].id, questions.QuestionReplyBody(answers=[["Version B"]]), current_user=USER,
    )
    worker = QuestionContinuationWorker()
    try:
        await worker.tick()
        assert "s1" in worker.runs
        await asyncio.wait_for(asyncio.gather(*worker.runs.values()), timeout=20)
        await _drain_tasks(long_chat)
        assert len(requests) == 2
        assert "Ask which subtitle version to use" in _content(requests[-1])
        assert any(message["role"] == "tool" and "Version B" in str(message["content"])
                   for message in requests[-1]["messages"])
        assert await questions.list_questions(current_user=USER) == []
        assert (await get_session("s1", user_id="u1")).status.value == "idle"
        await worker.tick()
        assert not worker.runs  # an applied answer cannot start another run
    finally:
        await worker.stop()


async def test_over_200_turns_automatically_compacts_then_accepts_input_after_manual_compaction(
    state, long_chat, monkeypatch,
):
    ids = await _seed("s1", "u1", turns=201)
    requests, summaries = [], []

    async def provider(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            yield {"type": "tool_call", "tool": "read", "args": {}, "call_id": "read-before-compaction"}
            # Exercise the real token threshold: GPT-4o's 128k context minus
            # reserved output space, independent of the number of messages.
            yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 127000, "output": 10}}
        else:
            yield {"type": "text_delta", "text": "Continued with the compressed context."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 3000, "output": 20}}

    async def summarize(**kwargs):
        summaries.append(kwargs)
        yield {"type": "text_delta", "text": f"Long-history summary {len(summaries)}."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 10000, "output": 100}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    monkeypatch.setattr("agent.llm.stream_llm", summarize)
    await _send(long_chat, "Keep this current request and the latest tool result")
    assert len(summaries) == 1
    assert "问题 0" in _content(summaries[0])
    assert "问题 150" in _content(summaries[0])  # well past message 200
    assert len(requests) == 2
    resumed = _content(requests[1])
    assert "Long-history summary 1." in resumed
    assert "问题 0" not in resumed
    assert "问题 200" in resumed  # recent turn preserved verbatim
    assert "Keep this current request and the latest tool result" in resumed
    assert TOOL_OUTPUT in resumed

    # The manual API must see the actual compressed tail and stop after the
    # summary. A subsequent ordinary prompt starts a fresh run successfully.
    tasks = BackgroundTasks()
    assert (await api.summarize_session("s1", tasks, current_user=USER))["ok"]
    await asyncio.wait_for(tasks(), timeout=20)
    await _drain_tasks(long_chat)
    assert len(summaries) == 2
    assert "Long-history summary 1." in _content(summaries[1])
    assert TOOL_OUTPUT in _content(summaries[1])
    assert len(requests) == 2
    await _send(long_chat, "New input following both compactions")
    assert len(requests) == 3
    assert "Long-history summary 2." in _content(requests[-1])
    assert "New input following both compactions" in _content(requests[-1])
    assert "问题 0" not in _content(requests[-1])

    # Compaction changes model context, not the persisted transcript. All
    # original rows remain reachable through the UI's backward pagination.
    public = await get_messages("s1", user_id="u1")
    assert [message.id for message in public[:402]] == ids
    paged, before = [], None
    while True:
        page = await api.get_history("s1", before=before, turns=20, current_user=USER)
        assert page["messages"]
        paged[0:0] = [message["id"] for message in page["messages"]]
        if not page["has_more"]:
            break
        before = page["messages"][0]["id"]
    assert paged == [message.id for message in public]
    assert len(paged) == len(set(paged))
    assert (await get_session("s1", user_id="u1")).status.value == "idle"
