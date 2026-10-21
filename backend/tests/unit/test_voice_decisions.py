"""Speech cancellation is scoped to successful reads from its own durable turn."""
import json

import pytest
from sqlalchemy import select

from assistant.requests import question_version
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from question import question as questions
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_placement import quiet  # noqa: F401
from tests.unit.test_assistant_pending import saved_request, pause
from tests.unit.test_assistant_sessions_v2 import card, main_turn, tool_call
from voice.decisions import for_turn, observed


def receipt(tool, value, *, status="completed"):
    return {"part": {"data": {"tool": tool, "status": status, "output": json.dumps(value)}}}


def test_only_successful_decision_reads_bind_and_later_answers_retire_them():
    a = {"request_id": "a", "version": "v1", "state": "pending"}
    b = {"id": "b", "version": "v2", "kind": "question"}
    payloads = [receipt("assets.describe", {"items": [a]}),
                receipt("cards.list", {"items": [a]}, status="error")]
    assert observed(payloads) == {}
    payloads += [receipt("cards.list", {"items": [a]}),
                 receipt("requests.list", {"items": [], "other_conversations": [b]})]
    assert observed(payloads) == {"a": "v1", "b": "v2"}
    payloads += [receipt("cards.answer", {"request_id": "a", "state": "decision_recorded"}),
                 receipt("requests.get", {**b, "state": "answered"})]
    assert observed(payloads) == {}


@pytest.mark.parametrize("change", ["unchanged", "answered", "version"])
async def test_read_receipt_is_owned_run_scoped_and_checked_against_sql(change):
    owner, workspace, main, _, request_id, _ = await saved_request()
    ctx, lease, _ = await main_turn(owner, workspace, main, "刚才那张卡片问什么？")
    try:
        async with get_db_session() as db:
            inbox_id = await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.run_id == ctx.run_id))
        assert await for_turn(main.id, owner, workspace, inbox_id) == ({}, False)
        row = await card(request_id)
        version = question_version(row)
        listed, _ = await tool_call(ctx, "cards.list", {"request_id": request_id})
        assert listed["items"][0]["request_id"] == request_id
        if change == "answered":
            await questions.reply(request_id, [["取消"]], owner)
        elif change == "version":
            from db.models.question import QuestionCheckpoint
            async with get_db_session() as db:
                question = await db.get(QuestionCheckpoint, request_id)
                question.questions = [{**question.questions[0], "question": "已修改的问题"}]
        assert await for_turn(main.id, owner, workspace, inbox_id) == ({request_id: version}, change != "unchanged")
        assert await for_turn(main.id, "another-user", workspace, inbox_id) == ({}, False)
        assert await for_turn(main.id, owner, "another-workspace", inbox_id) == ({}, False)
    finally:
        await pause(ctx, lease)
    # Old read receipts cannot make a new unrelated input depend on that card.
    ctx, lease, _ = await main_turn(owner, workspace, main, "这张图片是什么颜色？")
    try:
        async with get_db_session() as db:
            inbox_id = await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.run_id == ctx.run_id))
        assert await for_turn(main.id, owner, workspace, inbox_id) == ({}, False)
    finally:
        await pause(ctx, lease)
