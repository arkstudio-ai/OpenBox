"""Production context counters and pagination across the canonical boundary."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from agent.compaction import create_compaction, process_compaction
from api import sessions as api
from db.base import get_db_session
from db.models.message import Message
from models.message import TokenUsage
from session.agent_event_log import load_canonical_model_surface, rebuild_sql_read_model_from_events
from session.compaction import filter_compacted
from session.session import get_messages, get_session, update_session
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_full_history import _seed


async def test_long_history_compaction_preserves_counters_pages_and_trace(state, monkeypatch):
    from core.config import OpenBoxConfig
    config = OpenBoxConfig.model_validate({"compaction": {"tail_turns": 2, "preserve_recent_tokens": 500}})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    ids = await _seed("s1", "u1", turns=121)
    async with get_db_session() as db:
        for parent, answer in zip(ids[::2], ids[1::2], strict=True):
            await db.execute(update(Message).where(Message.id == answer).values(parent_id=parent))
    await update_session("s1", user_id="u1", token_usage=TokenUsage(
        input=2000, output=300, cache=40, total=2300, cost=2.5, credits="25.00",
    ))
    messages = await get_messages("s1", user_id="u1")
    boundary = await create_compaction("s1", auto=False, user_id="u1", messages=messages)
    assert boundary is not None
    assert boundary.parts[0].synthetic is True
    seen = []

    async def provider(**kwargs):
        seen.append(kwargs)
        yield {"type": "text_delta", "text": "Summary of earlier turns."}
        yield {"type": "finish", "usage": {
            "input": 700, "output": 4, "cache": 100, "total": 704,
            "cost": 1.25, "credits": "12.50",
        }}

    projection = AsyncMock()
    monkeypatch.setattr("agent.llm.stream_llm", provider)
    monkeypatch.setattr("session.session.record_projection_in_tx", projection)
    assert await process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "openai/test", auto=False, user_id="u1",
    ) == "stop"

    public = await get_messages("s1", user_id="u1")
    summary = public[-1]
    assert summary.summary and summary.finish == "stop"
    assert seen[0]["ctx"].message_id == summary.id
    assert summary.tokens.credits == "12.50"
    usage = (await get_session("s1", user_id="u1")).token_usage
    assert (usage.input, usage.output, usage.cache, usage.total) == (2700, 304, 140, 3004)
    assert usage.cost == 3.75 and usage.credits == "37.50"
    assert usage.context > 4 and usage.limit > usage.context  # summary plus retained tail and envelope
    assert any(call.args[3] == "context.replaced" for call in projection.await_args_list)
    assert any(call.args[3] == "part.committed" and
               call.args[4]["part"].get("text") == "Summary of earlier turns."
               for call in projection.await_args_list)

    user = {"user_id": "u1", "workspace_id": "w1"}
    first = await api.get_messages("s1", current_user=user)
    rest = await api.get_messages("s1", offset=200, current_user=user)
    assert len(first) == 200
    assert [row["id"] for row in first + rest] == [message.id for message in public]
    before = None
    paged = []
    for _ in range(20):
        page = await api.get_history("s1", before=before, turns=20, current_user=user)
        paged[0:0] = [row["id"] for row in page["messages"]]
        if not page["has_more"]:
            break
        before = page["messages"][0]["id"]
    assert paged == [message.id for message in public]
    assert len(paged) == len(set(paged))

    surface = await load_canonical_model_surface("s1", user_id="u1")
    compacted = await filter_compacted(list(surface.messages))
    assert summary.id in {message.id for message in compacted}
    assert ids[-1] in {message.id for message in compacted}  # recent tail survives
    assert ids[0] not in {message.id for message in compacted}
    await rebuild_sql_read_model_from_events("s1", user_id="u1")
    assert [message.model_dump() for message in await get_messages("s1", user_id="u1")] == [
        message.model_dump() for message in public
    ]


async def test_failed_compaction_preserves_history_and_records_failure(state, monkeypatch):
    from tests.unit.test_event_range_compaction_fork import _closed_turn
    await _closed_turn("s1", "u1")
    await create_compaction("s1", auto=False, user_id="u1")
    before = await get_messages("s1", user_id="u1")
    record = AsyncMock()

    async def provider(**kwargs):
        yield {"type": "error", "error": "temporary failure"}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    monkeypatch.setattr("trajectory.record", record)
    assert await process_compaction("s1", before, "openai/test", auto=False, user_id="u1") == "stop"
    visible = await filter_compacted(await get_messages("s1", user_id="u1"))
    assert [message.id for message in visible[:len(before)]] == [message.id for message in before]
    finished = [call for call in record.await_args_list if call.args[0] == "compaction.finished"]
    assert len(finished) == 1
    assert finished[0].args[1]["status"] == "failed"
    assert finished[0].args[1]["applied"] is False


@pytest.mark.parametrize("with_tool_step", [False, True])
async def test_first_long_turn_can_compact_before_a_final_answer(state, monkeypatch, with_tool_step):
    from agent import driver
    from models.message import ToolPartData
    from session.session import create_user_message, create_assistant_message, save_part, update_message_info

    prompt = await create_user_message("s1", "Long first request " * 300, user_id="u1")
    lease = await driver.reserve_run("s1", "u1", trigger_message_id=prompt.id)
    fence = (lease.session_id, lease.run_id, lease.generation)
    try:
        if with_tool_step:
            step = await create_assistant_message("s1", prompt.id, user_id="u1", run_fence=fence)
            await save_part(ToolPartData(
                tool="read", status="completed", input={"file_path": "/workspace/data.txt"},
                output="Large completed tool result " * 300, call_id="first-read",
                message_id=step.id, session_id="s1",
            ), is_new=True, user_id="u1", run_fence=fence)
            step.finish = "tool_calls"
            await update_message_info(step, user_id="u1", run_fence=fence)
        await create_compaction("s1", auto=True, user_id="u1", run_fence=fence)

        async def provider(**kwargs):
            yield {"type": "text_delta", "text": "Continue the original request using the findings."}
            yield {"type": "finish", "usage": {"input": 900, "output": 10}}

        monkeypatch.setattr("agent.llm.stream_llm", provider)
        assert await process_compaction(
            "s1", await get_messages("s1", user_id="u1"), "openai/test",
            auto=True, user_id="u1", run_fence=fence,
        ) == "continue"
        visible = await filter_compacted(list((await load_canonical_model_surface("s1", user_id="u1")).messages))
        assert prompt.id not in {message.id for message in visible}
        assert any(message.summary and message.finish == "stop" for message in visible)
        assert visible[-1].parts[0].synthetic
        assert "Continue working" in visible[-1].parts[0].text
    finally:
        await lease.release()


async def test_compaction_keeps_pending_ask_in_the_context_tail(state):
    from models.message import ToolPartData
    from session.event_range import freeze_compaction_event_range
    from session.session import create_user_message, create_assistant_message, save_part, update_message_info

    prompt = await create_user_message("s1", "Request that needs clarification " * 100, user_id="u1")
    answer = await create_assistant_message("s1", prompt.id, user_id="u1")
    await save_part(ToolPartData(tool="question", status="waiting_input", input={},
                    call_id="pending-ask", message_id=answer.id, session_id="s1"),
                    is_new=True, user_id="u1")
    answer.finish = "waiting_input"
    await update_message_info(answer, user_id="u1")
    boundary = await create_compaction("s1", auto=False, user_id="u1")
    frozen = await freeze_compaction_event_range(
        "s1", user_id="u1", compaction_user_id=boundary.id,
        requested_tail_start_id=None, run_fence=None, allow_partial_turn=True,
    )
    assert frozen.source.covered_message_ids == (prompt.id,)
    assert frozen.tail_start_id == answer.id
