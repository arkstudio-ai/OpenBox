"""One confirmation-card protocol for every high-risk assistant action, on real SQL.

docs/ASSISTANT_VOICE_FIX_PLAN.md 1.2: a card in the main session says what will
be done and, on its second line, the impact; 确认 lets exactly that input through
once; an unanswered card expires after ten minutes and the next call asks
again. 1.3: a call lists the waiting cards (pending_cards) and answers them
through question.question.reply with a voice source.
"""
from dataclasses import replace
from datetime import timedelta
from itertools import count

import pytest
from sqlalchemy import select

from assistant.confirmations import (ANSWER_VALID_FOR, CANCEL, CONFIRM, CONFIRM_ACTION, CONFIRM_KIND, EXPIRES_AFTER,
                                     KIND, pending_cards, release_confirmation, require_card)
from db.base import get_db_session
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from models.message import ToolPartData, ToolStatus
from question import question as q, runtime
from question.continuation import QuestionContinuationWorker, expire_questions
from session.session import save_part
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import (MAIN_MESSAGES, assistant, card, finish_turn, main_turn,
                                                   tool_call, watched_conversation)

CALLS = count(1)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-confirmations-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


async def call_part(ctx, tool="projects.delete"):
    """A persisted running call of a card-asking tool, as the processor saves it."""
    index = next(CALLS)
    part = ToolPartData(tool=tool, canonical_tool_id=tool, call_id=f"confirm-call-{index}",
        wire_tool_name=tool.replace(".", "_"), provider_binding_digest="d" * 64, provider_dialect="openai",
        stream_seq=index, status=ToolStatus.RUNNING, input={}, session_id=ctx.session_id, message_id=ctx.message_id)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    return replace(ctx, part_id=part.id)


async def ask_confirm(ctx, digest="digest-1", **extra):
    with pytest.raises(q.QuestionSuspended) as asked:
        await require_card(ctx, kind=CONFIRM_KIND, action="project_delete", digest=digest,
                           prompt="删除项目「贪吃蛇」。", impact="其中 3 个会话会一起删除。", header="确认删除",
                           description="由个人助理删除这个项目", target={"project_id": "p1"}, **extra)
    return asked.value.request_id


async def test_confirm_card_states_action_and_impact_and_passes_exactly_once():
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉。")
    try:
        first = await call_part(ctx)
        request_id = await ask_confirm(first)
        row = await card(request_id)
        question = row.questions[0]
        assert question["question"] == "删除项目「贪吃蛇」。\n影响：其中 3 个会话会一起删除。"
        assert [option["label"] for option in question["options"]] == [CONFIRM_ACTION, CANCEL] == ["确认", "取消"]
        assert question["header"] == "确认删除" and question["custom"] is False
        assert row.continuation["kind"] == "question" and KIND not in row.continuation
        assert row.continuation[CONFIRM_KIND] == {"project_id": "p1", "digest": "digest-1", "confirm": "确认",
            "action": "project_delete", "impact": "其中 3 个会话会一起删除。", "high_risk": True}
        assert timedelta(minutes=9) < runtime.utc(row.expires_at) - runtime.utc(row.created_at) <= EXPIRES_AFTER
        await q.reply(request_id, [["确认"]], owner)
        second = await call_part(ctx)
        assert await require_card(second, kind=CONFIRM_KIND, action="project_delete", digest="digest-1",
            prompt="删除项目「贪吃蛇」。", impact="…", header="确认删除", description="…") == request_id
        assert (await card(request_id)).continuation[CONFIRM_KIND]["consumed"] is True
        # Consumed: the same input needs a new card, never a second pass.
        third = await call_part(ctx)
        again = await ask_confirm(third)
        assert again != request_id and (await card(again)).status == "pending"
    finally:
        await lease.release(session_status="idle")


async def test_cancel_or_another_input_never_passes():
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "删掉两个项目。")
    try:
        cancelled = await ask_confirm(await call_part(ctx))
        await q.reply(cancelled, [[CANCEL]], owner)
        retried = await ask_confirm(await call_part(ctx))
        assert retried != cancelled
        await q.reply(retried, [["确认"]], owner)
        # A confirmed card covers only its own digest, and only its own kind.
        other = await ask_confirm(await call_part(ctx), digest="digest-2")
        assert other != retried
        with pytest.raises(q.QuestionSuspended):
            await require_card(await call_part(ctx), digest="digest-1", prompt="…", header="…", description="…")
        assert (await card(retried)).continuation[CONFIRM_KIND].get("consumed") is None
    finally:
        await lease.release(session_status="idle")


async def test_an_expired_card_is_asked_again_and_a_stale_answer_is_no_consent():
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉。")
    try:
        first = await call_part(ctx)
        expired = await ask_confirm(first)
        async with get_db_session() as db:
            row = await db.get(QuestionCheckpoint, expired)
            row.expires_at = runtime.now() - timedelta(seconds=1)
        assert await pending_cards(owner, workspace, main.id) == []
        await expire_questions()
        row = await card(expired)
        assert row.status == "expired"
        async with get_db_session() as db:
            part = await db.get(Part, first.part_id)
            assert part.data["status"] == "error" and part.data["metadata"]["question_status"] == "expired"
        with pytest.raises(q.QuestionGone):
            await q.reply(expired, [["确认"]], owner)
        asked_again = await ask_confirm(await call_part(ctx))
        assert asked_again != expired and (await card(asked_again)).status == "pending"
        # An answer older than the window does not authorize a call now.
        await q.reply(asked_again, [["确认"]], owner)
        async with get_db_session() as db:
            row = await db.get(QuestionCheckpoint, asked_again)
            row.updated_at = runtime.now() - ANSWER_VALID_FOR - timedelta(seconds=1)
        stale = await ask_confirm(await call_part(ctx))
        assert stale not in {expired, asked_again}
        assert (await card(asked_again)).continuation[CONFIRM_KIND].get("consumed") is None
    finally:
        await lease.release(session_status="idle")


async def test_released_confirmation_is_usable_again():
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉。")
    try:
        request_id = await ask_confirm(await call_part(ctx))
        await q.reply(request_id, [["确认"]], owner)
        consumer = await call_part(ctx)
        args = dict(kind=CONFIRM_KIND, digest="digest-1", prompt="…", header="…", description="…")
        assert await require_card(consumer, **args) == request_id
        await release_confirmation(consumer, request_id)
        assert await require_card(await call_part(ctx), **args) == request_id
    finally:
        await lease.release(session_status="idle")


async def test_the_answered_card_tells_the_assistant_what_to_do_for_either_answer():
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉。")
    try:
        part_ctx = await call_part(ctx)
        request_id = await ask_confirm(part_ctx)
        await finish_turn(ctx, lease, "请在卡片上确认。")
    finally:
        await lease.release(session_status="waiting_input")
    await q.reply(request_id, [[CANCEL]], owner)
    await QuestionContinuationWorker().tick()
    async with get_db_session() as db:
        part = await db.get(Part, part_ctx.part_id)
    assert part.data["status"] == "completed" and part.data["metadata"]["confirmation"] == "declined"
    assert "declined on the card" in part.data["output"] and "Do not do it" in part.data["output"]


async def test_pending_cards_lists_every_main_session_card_for_a_call():
    owner, other, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉，再告诉团队换深色。")
    try:
        delete_card = await ask_confirm(await call_part(ctx))
        sent, _ = await tool_call(ctx, "tasks.followup", {"task_id": linked["task_id"], "text": "换成深色主题。",
            "expected_revision": linked["task_revision"], "source_message_ids": [human]})
        memory_ctx = await call_part(ctx, "memory.remember")
        with pytest.raises(q.QuestionSuspended) as remembered:
            await q.ask(main.id, [q.Question(question="要我记住这条吗？\n「最近在吃降压药」", header="记忆确认", custom=False,
                options=[q.QuestionOption(label="记住"), q.QuestionOption(label="不用记")],
                detail={"kind": "memory_proposal", "summary": "最近在吃降压药", "memory_id": "m1"})],
                {"callID": memory_ctx.part_id, "messageID": memory_ctx.message_id}, owner,
                continuation={"kind": "memory_proposal", "memory_id": "m1", "expected_revision": 1})
        cards = await pending_cards(owner, workspace, main.id)
    finally:
        await lease.release(session_status="idle")
    assert [item["card_id"] for item in cards] == [delete_card, sent["suspended"], remembered.value.request_id]
    removal, send, memory = cards
    assert removal["kind"] == CONFIRM_KIND and removal["action"] == "project_delete" and removal["high_risk"] is True
    assert removal["prompt"].endswith("\n影响：其中 3 个会话会一起删除。") and removal["options"] == ["确认", "取消"]
    assert removal["impact"] == "其中 3 个会话会一起删除。" and removal["expires_at"] > removal["created_at"]
    assert send["kind"] == KIND and send["action"] == "task_send" and send["options"] == [CONFIRM, CANCEL]
    assert send["high_risk"] is True and "换成深色主题。" in send["prompt"]
    assert memory["kind"] == "memory_proposal" and memory["options"] == ["记住", "不用记"]
    assert memory["high_risk"] is False and memory["expires_at"] is None
    # Another member never sees them; answered cards leave the list.
    from assistant.policy import AssistantError
    with pytest.raises(AssistantError) as foreign:
        await pending_cards(other, workspace, main.id)
    assert foreign.value.code == "ASSISTANT_UNAVAILABLE"
    await q.reply(delete_card, [["确认"]], owner)
    assert [item["card_id"] for item in await pending_cards(owner, workspace, main.id)] == [
        sent["suspended"], remembered.value.request_id]


@pytest.mark.parametrize("label", ["确认", "取消"])
async def test_a_call_answers_a_main_session_card_with_a_voice_source(label):
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "把贪吃蛇项目删掉，再告诉团队换深色。")
    try:
        delete_card = await ask_confirm(await call_part(ctx))
        args = {"task_id": linked["task_id"], "text": "换成深色主题。", "expected_revision": linked["task_revision"],
                "source_message_ids": [human]}
        sent, _ = await tool_call(ctx, "tasks.followup", args)
        voice = {"kind": "voice", "call_id": "call-1", "display_id": "spoken-1"}
        await q.reply(delete_card, [[label]], owner, source_ref=voice)
        send_label = CONFIRM if label == "确认" else CANCEL
        await q.reply(sent["suspended"], [[send_label]], owner, source_ref=voice)
        assert (await card(delete_card)).answers == [[label]]
        retried, _ = await tool_call(ctx, "tasks.followup", args)
        if label == "确认":
            assert retried["state"] == "accepted"
            passed = await require_card(await call_part(ctx), kind=CONFIRM_KIND, digest="digest-1", prompt="…",
                                        header="…", description="…")
            assert passed == delete_card
        else:
            assert "suspended" in retried and retried["suspended"] != sent["suspended"]
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        rows = list((await db.scalars(select(QuestionCheckpoint).where(QuestionCheckpoint.id.in_(
            [delete_card, sent["suspended"]])))).all())
    assert {row.status for row in rows} == {"answered"}
