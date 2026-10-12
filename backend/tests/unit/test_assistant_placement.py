"""Real SQL: no creation before selection, atomic admission and stable card retries."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from assistant.confirmations import pending_cards
from assistant.placement import KIND
from db.base import get_db_session
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.project import Project
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from question import question as q, runtime
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, card, main_turn, tool_call


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "placement-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


async def counts(owner):
    async with get_db_session() as db:
        return tuple([await db.scalar(select(func.count()).select_from(model).where(model.user_id == owner))
                      for model in (Project, Session, AssistantTask)])


async def prepare(text="帮我写一份新品发布计划"):
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, text)
    args = {"project_id": main.project_id, "title": "新品发布计划", "instructions": "写一份新品发布计划",
            "source_message_ids": [human]}
    return owner, workspace, main, ctx, lease, args


@pytest.mark.parametrize("choice_index", [0, 1, 2])
async def test_choice_creates_only_the_selected_destination_and_retry_is_idempotent(choice_index):
    owner, workspace, main, ctx, lease, args = await prepare()
    try:
        before = await counts(owner)
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        question = await card(asked["suspended"])
        assert await counts(owner) == before
        assert len(question.questions[0]["options"]) == 3
        cards = await pending_cards(owner, workspace, main.id)
        assert cards[0]["kind"] == KIND and cards[0]["high_risk"] is False
        choice = question.questions[0]["options"][choice_index]["label"]
        await q.reply(question.id, [[choice]], owner)
        result, _ = await tool_call(ctx, "tasks.submit", args)
        if choice_index == 2:
            assert result == {"state": "cancelled"}
            assert await counts(owner) == before
            async with get_db_session() as db:
                assert await db.scalar(select(AssistantCommand.id)) is None
            return
        assert result["state"] == "accepted"
        after = await counts(owner)
        assert after == (before[0] + (choice_index == 1), before[1] + 1, before[2] + 1)
        repeated, _ = await tool_call(ctx, "tasks.submit", args)
        assert repeated == result and await counts(owner) == after
        async with get_db_session() as db:
            task = await db.get(AssistantTask, result["task_id"])
            project = await db.get(Project, task.project_id)
            command = await db.get(AssistantCommand, result["command_id"])
            assert (task.project_id == main.project_id) == (choice_index == 0)
            assert project.name == ("默认空间" if choice_index == 0 else args["title"])
            assert command.source_ref["placement"]["question_id"] == question.id
            assert result["placement"]["project_name"] == project.name
        assert (await card(question.id)).continuation[KIND]["receipt"] == result
    finally:
        await lease.release(session_status="idle")


async def test_a_changed_request_or_an_expired_answer_needs_a_new_choice():
    owner, _, _, ctx, lease, args = await prepare()
    try:
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        question = await card(asked["suspended"])
        await q.reply(question.id, [[question.questions[0]["options"][0]["label"]]], owner)
        changed, _ = await tool_call(ctx, "tasks.submit", {**args, "instructions": "改为整理竞争对手资料"})
        assert changed["suspended"] != question.id
        await q.reply(changed["suspended"], [["取消"]], owner)
        async with get_db_session() as db:
            row = await db.get(QuestionCheckpoint, question.id)
            row.updated_at = runtime.now() - timedelta(hours=1)
        expired, _ = await tool_call(ctx, "tasks.submit", args)
        assert expired["suspended"] not in (question.id, changed["suspended"])
        assert (await counts(owner))[2] == 0
    finally:
        await lease.release(session_status="idle")


async def test_explicit_new_conversation_in_a_named_project_skips_the_choice():
    owner, _, _, ctx, lease, args = await prepare("在默认空间里新建一个对话，写新品发布计划")
    try:
        result, _ = await tool_call(ctx, "tasks.submit", {**args, "destination_confirmed": True})
        assert result["state"] == "accepted"
        async with get_db_session() as db:
            assert await db.scalar(select(QuestionCheckpoint.id)) is None
    finally:
        await lease.release(session_status="idle")


async def test_failed_admission_leaves_no_new_project_and_the_same_card_can_retry(monkeypatch):
    from core.config import get_config
    owner, _, _, ctx, lease, args = await prepare()
    try:
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        question = await card(asked["suspended"])
        await q.reply(question.id, [[question.questions[0]["options"][1]["label"]]], owner)
        before = await counts(owner)
        original = get_config().max_sessions_per_user
        monkeypatch.setattr(get_config(), "max_sessions_per_user", 0)
        failed, _ = await tool_call(ctx, "tasks.submit", args)
        assert failed["error"] == "SESSION_QUOTA_EXCEEDED" and await counts(owner) == before
        monkeypatch.setattr(get_config(), "max_sessions_per_user", original)
        result, _ = await tool_call(ctx, "tasks.submit", args)
        assert result["state"] == "accepted"
        assert await counts(owner) == (before[0] + 1, before[1] + 1, before[2] + 1)
    finally:
        await lease.release(session_status="idle")


async def test_forged_human_source_cannot_even_open_a_choice():
    owner, _, _, ctx, lease, args = await prepare()
    try:
        denied, _ = await tool_call(ctx, "tasks.submit", {**args, "source_message_ids": [ctx.message_id]})
        assert denied["error"] == "ASSISTANT_SOURCE_UNVERIFIED"
        async with get_db_session() as db:
            assert await db.scalar(select(QuestionCheckpoint.id)) is None
        assert (await counts(owner))[2] == 0
    finally:
        await lease.release(session_status="idle")


async def test_choice_resumes_through_the_durable_inbox_with_original_arguments(monkeypatch):
    from db.models.agent_inbox import AgentInboxItem
    from db.models.part import Part
    from question.continuation import QuestionContinuationWorker
    owner, _, main, ctx, lease, args = await prepare()
    try:
        asked, part = await tool_call(ctx, "tasks.submit", args)
    finally:
        await lease.release(session_status="waiting_input")
    question = await card(asked["suspended"])
    await q.reply(question.id, [[question.questions[0]["options"][1]["label"]]], owner)
    question = await card(question.id)
    worker = QuestionContinuationWorker()
    await worker._resume_candidate(main.id, owner, question.generation)
    async with get_db_session() as db:
        resumed = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
            AgentInboxItem.origin == "system_recovery"))
        completed = await db.get(Part, part.id)
        assert resumed.origin_ref["entrypoint"] == "question_answer"
        assert completed.data["metadata"]["placement"] == "selected"
        assert args["instructions"] in completed.data["output"]
        assert args["source_message_ids"][0] in completed.data["output"]
        assert "Nothing has been created yet" in completed.data["output"]
    assert (await counts(owner))[2] == 0


async def test_spoken_project_name_is_applied_atomically_and_cannot_change_on_retry():
    owner, _, _, ctx, lease, args = await prepare()
    try:
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        question = await card(asked["suspended"])
        choice = question.questions[0]["options"][1]["label"]
        await q.reply(question.id, [[choice]], owner, placement_project_name="春季上新")
        result, _ = await tool_call(ctx, "tasks.submit", args)
        assert result["state"] == "accepted" and result["placement"]["project_name"] == "春季上新"
        async with get_db_session() as db:
            task = await db.get(AssistantTask, result["task_id"])
            assert (await db.get(Project, task.project_id)).name == "春季上新"
        repeat = await q.reply(question.id, [[choice]], owner, placement_project_name="春季上新")
        assert repeat["applied"] is False
        with pytest.raises(q.QuestionConflict):
            await q.reply(question.id, [[choice]], owner, placement_project_name="另一个项目")
        assert (await tool_call(ctx, "tasks.submit", args))[0] == result
    finally:
        await lease.release(session_status="idle")


async def test_a_name_cannot_be_attached_to_the_cancel_option():
    owner, _, _, ctx, lease, args = await prepare()
    try:
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        with pytest.raises(ValueError):
            await q.reply(asked["suspended"], [["取消"]], owner, placement_project_name="春季上新")
        assert (await card(asked["suspended"])).status == "pending"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("cancel", [False, True])
async def test_no_matching_project_offers_only_creation_or_cancel(cancel):
    owner, workspace, main, ctx, lease, args = await prepare("帮我做一个咖啡店开业方案")
    args.pop("project_id")
    args["placement"] = {"question": "要不要为这件事建一个咖啡店开业项目，在里面开个对话来做？",
                         "project_name": "咖啡店开业"}
    try:
        before = await counts(owner)
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        question = await card(asked["suspended"])
        assert await counts(owner) == before
        assert question.questions[0]["question"] == args["placement"]["question"]
        options = [option["label"] for option in question.questions[0]["options"]]
        assert options == ["新建项目「咖啡店开业」", "取消"]
        [shown] = await pending_cards(owner, workspace, main.id)
        from voice.cards import spoken_card
        spoken = spoken_card(shown, "1")
        assert spoken["options"][0]["fields"] == ["project_name"]
        assert "fields" not in spoken["options"][1]
        await q.reply(question.id, [[options[-1 if cancel else 0]]], owner,
                      **({} if cancel else {"placement_project_name": "新店筹备"}))
        receipt, _ = await tool_call(ctx, "tasks.submit", args)
        if cancel:
            assert receipt == {"state": "cancelled"} and await counts(owner) == before
        else:
            assert receipt["placement"]["project_name"] == "新店筹备"
            assert await counts(owner) == tuple(value + 1 for value in before)
            repeat, _ = await tool_call(ctx, "tasks.submit", args)
            assert repeat == receipt and await counts(owner) == tuple(value + 1 for value in before)
    finally:
        await lease.release(session_status="idle")


async def test_changed_proposal_does_not_reuse_an_answer_for_another_project():
    owner, _, _, ctx, lease, args = await prepare()
    args.update(project_id=None, placement={"question": "建一个新品发布项目来做？", "project_name": "新品发布"})
    try:
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        question = await card(asked["suspended"])
        await q.reply(question.id, [[question.questions[0]["options"][0]["label"]]], owner)
        changed = {**args, "placement": {**args["placement"], "project_name": "不同的项目"}}
        second, _ = await tool_call(ctx, "tasks.submit", changed)
        assert second["suspended"] != question.id
        assert (await counts(owner))[2] == 0
    finally:
        await lease.release(session_status="idle")


async def test_missing_project_cannot_skip_the_choice_by_claiming_confirmation():
    owner, _, _, ctx, lease, args = await prepare()
    try:
        before = await counts(owner)
        denied, _ = await tool_call(ctx, "tasks.submit", {**args, "project_id": None, "destination_confirmed": True})
        assert denied["error"] == "ASSISTANT_PLACEMENT_REQUIRED" and await counts(owner) == before
    finally:
        await lease.release(session_status="idle")
