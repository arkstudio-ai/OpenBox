"""Saved task choices survive other topics without replaying an old tool."""
import pytest
from sqlalchemy import func, select

from agent import inbox
from assistant.confirmations import pending_cards
from assistant.requests import question_version
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from question import question as q
from session.session import update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_placement import quiet, prepare  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, card, main_turn, tool_call


async def pause(ctx, lease):
    message = MAIN_MESSAGES.pop(ctx.message_id)
    message.finish = "waiting_input"
    await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    await lease.release(session_status="waiting_input")


async def saved_request():
    owner, workspace, main, ctx, lease, args = await prepare()
    asked, part = await tool_call(ctx, "tasks.submit", args)
    await pause(ctx, lease)
    return owner, workspace, main, args, asked["suspended"], part.id


async def test_new_topic_keeps_choice_and_answer_resumes_exactly_once():
    owner, workspace, main, args, request_id, part_id = await saved_request()
    ctx, lease, human = await main_turn(owner, workspace, main, "刚才那个发布计划放在默认空间里")
    try:
        row = await card(request_id)
        assert row.detached and row.status == "pending"
        assert request_id in {r.id for r in await q.list_pending(owner)}
        assert len(await pending_cards(owner, workspace, main.id, message_id=row.message_id)) == 1
        assert await pending_cards(owner, workspace, main.id, message_id=ctx.message_id) == []
        listed, _ = await tool_call(ctx, "cards.list", {})
        assert listed["items"][0]["original_call"]["arguments"] == args
        choice = row.questions[0]["options"][0]["label"]
        answer = dict(request_id=request_id, version=question_version(row), answers=[[choice]],
                      source_message_ids=[human])
        recorded, _ = await tool_call(ctx, "cards.answer", answer)
        assert recorded.get("state") == "decision_recorded", recorded
        assert not recorded["execution_confirmed"]
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AssistantTask)) == 0
            assert (await db.get(Part, part_id)).data["status"] == "completed"
        created, _ = await tool_call(ctx, "tasks.submit", recorded["original_call"]["arguments"])
        assert created["state"] == "accepted"
        assert (await tool_call(ctx, "tasks.submit", args))[0] == created
        assert (await card(request_id)).applied
    finally:
        await lease.release(session_status="idle")


async def test_duplicate_preparation_uses_existing_pending_request():
    owner, workspace, main, args, request_id, _ = await saved_request()
    ctx, lease, _ = await main_turn(owner, workspace, main, "先看看我有什么素材")
    try:
        duplicate, _ = await tool_call(ctx, "tasks.submit", args)
        assert duplicate["error"] == "ASSISTANT_PLACEMENT_PENDING"
        assert request_id in duplicate["message"]
        assert len(await pending_cards(owner, workspace, main.id)) == 1
    finally:
        await lease.release(session_status="idle")


async def test_ui_answer_after_topic_change_is_durable_and_wakes_once(monkeypatch):
    owner, workspace, main, _, request_id, _ = await saved_request()
    ctx, lease, _ = await main_turn(owner, workspace, main, "先说别的")
    await pause(ctx, lease)
    wakes = []
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *args: wakes.append(args))
    row = await card(request_id)
    answers = [[row.questions[0]["options"][0]["label"]]]
    assert (await q.reply(request_id, answers, owner))["applied"]
    assert not (await q.reply(request_id, answers, owner))["applied"]
    assert wakes == [(main.id, owner)]
    async with get_db_session() as db:
        items = (await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == main.id, AgentInboxItem.origin == "system_recovery"))).all()
        assert len(items) == 1 and items[0].origin_ref["question_id"] == request_id
        assert await db.scalar(select(func.count()).select_from(AssistantTask)) == 0


@pytest.mark.parametrize("other_kind", ["preparation", "approval"])
async def test_saved_card_answer_waits_only_for_execution_approvals(other_kind):
    from question import runtime
    from tests.unit.test_assistant_confirmations import ask_confirm, call_part

    owner, workspace, main, args, request_id, _ = await saved_request()
    ctx, lease, human = await main_turn(owner, workspace, main, "另外帮我准备第二件事")
    if other_kind == "preparation":
        result, _ = await tool_call(ctx, "tasks.submit", {**args, "title": "第二个计划",
            "instructions": "准备另一份计划", "source_message_ids": [human]})
        second_id = result["suspended"]
    else:
        second_id = await ask_confirm(await call_part(ctx))
    await pause(ctx, lease)

    assert (await q.reply(request_id, [["取消"]], owner))["applied"]
    second = await card(second_id)
    assert second.status == "pending"
    assert second.detached is (other_kind == "preparation")
    async with get_db_session() as db:
        assert await runtime.admission_blocked_locked(db, main.id) is (other_kind == "approval")
        assert await db.scalar(select(func.count()).select_from(AssistantTask)) == 0
    assert await inbox._has_waking_input(main.id, owner) is (other_kind == "preparation")


@pytest.mark.parametrize("bad_source", ["before_card", "assistant"])
async def test_old_or_model_authored_answer_is_rejected(bad_source):
    owner, workspace, main, args, request_id, _ = await saved_request()
    ctx, lease, _ = await main_turn(owner, workspace, main, "这几个选项有什么区别")
    try:
        row = await card(request_id)
        result, _ = await tool_call(ctx, "cards.answer", dict(request_id=request_id,
            version=question_version(row), answers=[[row.questions[0]["options"][0]["label"]]],
            source_message_ids=args["source_message_ids"] if bad_source == "before_card" else [ctx.message_id]))
        assert result["error"] in {"ASSISTANT_CARD_ANSWER_REQUIRED", "ASSISTANT_SOURCE_UNVERIFIED"}
        assert (await card(request_id)).status == "pending"
    finally:
        await lease.release(session_status="idle")
