"""V2 P4 on real SQL: questions across the user's conversations, answers by the assistant (D6), status reads.

docs/PERSONAL_ASSISTANT_DESIGN_V2.md 10 and 12: the assistant sees questions
waiting in all the user's own conversations; it may answer an ordinary one for
the user, shown there as "由个人助理代答"; decisions stay with the user, who
gets a link; a workspace-visible conversation asks the user on a card first.
"""
import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from db.base import get_db_session
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from models.message import ToolPartData
from question import question as q, runtime
from question.continuation import QuestionContinuationWorker, apply_answers
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import (MAIN_MESSAGES, assistant, card, conversation, finish_turn,
                                                   main_turn, tool_call)

COLORS = [{"label": "深色"}, {"label": "浅色"}]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-requests-v2-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


async def ask_in(session, owner, *, tool="question", question="页面用哪种配色？", options=COLORS, custom=False,
                 allow_attachments=False):
    """An agent in `session` asks the user one question and waits."""
    await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup", prompt="做个页面",
                                  origin="human", origin_ref={"actor_user_id": owner})
    lease = await reserve_run(session.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    ticket = await runtime.start_run(session.id, owner, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    try:
        fence = (session.id, lease.run_id, lease.generation)
        message = await create_assistant_message(session.id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=owner, run_fence=fence)
        part = ToolPartData(session_id=session.id, message_id=message.id, tool=tool, status="running")
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        with pytest.raises(q.QuestionSuspended) as suspended:
            await q.ask(session.id, [q.Question(question=question, custom=custom, options=options,
                                                allow_attachments=allow_attachments)],
                        {"messageID": message.id, "callID": part.id}, owner)
        message.finish = "waiting_input"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input")
    return suspended.value.request_id, part.id


async def question_part(part_id):
    async with get_db_session() as db:
        return await db.get(Part, part_id)


async def test_questions_in_other_conversations_are_listed_with_who_may_answer():
    owner, other, workspace, main = await assistant()
    mine = await conversation(owner, workspace, main, "配色讨论", visibility="private")
    takeover = await conversation(owner, workspace, main, "登录抖音", visibility="private")
    theirs = await conversation(other, workspace, main, "成员的会话")
    ordinary, _ = await ask_in(mine, owner)
    user_only, _ = await ask_in(takeover, owner, tool="desktop_takeover", question="请完成滑块验证")
    await ask_in(theirs, other)
    ctx, lease, _ = await main_turn(owner, workspace, main, "我有哪些会话在等我回答？")
    try:
        value, part = await tool_call(ctx, "requests.list", {"kind": "question"})
    finally:
        await lease.release(session_status="idle")
    others = {item["id"]: item for item in value["other_conversations"]}
    assert set(others) == {ordinary, user_only}  # never another member's conversation
    assert others[ordinary]["assistant_may_answer"] is True and others[ordinary]["link"] == f"/app/s/{mine.id}"
    assert others[ordinary]["questions"][0]["options"] == ["深色", "浅色"]
    assert others[user_only]["assistant_may_answer"] is False and others[user_only]["user_only_reason"]


async def test_the_assistant_answers_an_ordinary_question_and_the_conversation_shows_it():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "配色讨论", visibility="private")
    request_id, part_id = await ask_in(session, owner)
    ctx, lease, _ = await main_turn(owner, workspace, main, "配色讨论那个会话，帮我选深色。")
    try:
        bad, part = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [["紫色"]]})
        assert bad["error"] == "ASSISTANT_ANSWER_INVALID"
        value, part = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [["深色"]]})
    finally:
        await lease.release(session_status="idle")
    assert value["state"] == "answered" and value["answers"] == [["深色"]]
    row = await card(request_id)
    assert row.status == "answered" and row.answers == [["深色"]]
    assert row.continuation["answered_by"]["kind"] == "assistant"
    assert await apply_answers(session.id, owner) is not None
    recorded = await question_part(part_id)
    assert recorded.data["status"] == "completed"
    assert recorded.data["metadata"]["answered_by"] == "assistant"
    assert recorded.data["output"].startswith("The user's personal assistant answered for the user")


async def test_decisions_and_actions_stay_with_the_user():
    owner, other, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "登录抖音", visibility="private")
    takeover, _ = await ask_in(session, owner, tool="desktop_takeover", question="请完成滑块验证")
    files = await conversation(owner, workspace, main, "整理素材", visibility="private")
    choose_files, _ = await ask_in(files, owner, question="用哪些图片？", allow_attachments=True, custom=True)
    theirs = await conversation(other, workspace, main, "成员的会话", visibility="private")
    foreign, _ = await ask_in(theirs, other)
    ctx, lease, _ = await main_turn(owner, workspace, main, "这些都帮我回答了吧。")
    try:
        for request_id in (takeover, choose_files):
            value, part = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [["好"]]})
            assert value["error"] == "ASSISTANT_ANSWER_HUMAN_ONLY" and "/app/s/" in value["message"]
        value, part = await tool_call(ctx, "requests.answer", {"request_id": foreign, "answers": [["深色"]]})
        assert value["error"] == "ASSISTANT_REQUEST_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")
    for request_id in (takeover, choose_files, foreign):
        assert (await card(request_id)).status == "pending"


async def test_a_workspace_visible_conversation_is_answered_only_after_the_users_card():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "团队配色", visibility="workspace")
    request_id, _ = await ask_in(session, owner)
    ctx, lease, _ = await main_turn(owner, workspace, main, "团队配色那个会话帮我选深色。")
    try:
        asked, part = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [["深色"]]})
        confirmation = await card(asked["suspended"])
        assert confirmation.session_id == main.id and "团队配色" in confirmation.questions[0]["question"]
        assert "深色" in confirmation.questions[0]["question"]
        await finish_turn(ctx, lease, "请在卡片上确认。")
    finally:
        await lease.release(session_status="idle")
    assert (await card(request_id)).status == "pending"
    await q.reply(confirmation.id, [["确认代答"]], owner)
    await QuestionContinuationWorker().tick()
    # The card itself does nothing; its result tells the assistant to call again.
    recorded = await question_part(confirmation.part_id)
    assert recorded.data["metadata"]["confirmation"] == "confirmed"
    assert "call the same tool again" in recorded.data["output"]
    assert (await card(request_id)).status == "pending"
    ctx, lease, _ = await main_turn(owner, workspace, main, "继续。")
    try:
        value, part = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [["深色"]]})
    finally:
        await lease.release(session_status="idle")
    assert value["state"] == "answered" and (await card(request_id)).status == "answered"


async def test_a_watched_task_question_is_answered_through_the_versioned_command():
    from tests.unit.test_assistant_requests import pending
    scope, created, request, _binding = await pending()
    from db.models.session import Session
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
    ctx, lease, _ = await main_turn(scope["user_id"], scope["workspace_id"], main, "那个任务的问题帮我选 Blue。")
    try:
        value, part = await tool_call(ctx, "requests.answer", {"request_id": request.id, "answers": [["Blue"]]})
    finally:
        await lease.release(session_status="idle")
    assert value["state"] == "answered" and value.get("command_id")
    row = await card(request.id)
    assert row.status == "answered" and row.continuation["answered_by"]["kind"] == "assistant"
    from assistant.requests import decision_for
    async with get_db_session() as db:
        command = await decision_for(db, request.id)
    assert command.source_ref["kind"] == "assistant_answer" and command.state == "accepted"
    assert await apply_answers(row.session_id, scope["user_id"]) is not None
    part = await question_part(row.part_id)
    assert part.data["metadata"]["answered_by"] == "assistant"


async def test_status_reads_answer_where_things_stand():
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "我的额度、云电脑、技能和发布情况怎么样？")
    try:
        credits, _ = await tool_call(ctx, "status.credits", {})
        resources, _ = await tool_call(ctx, "status.resources", {})
        skills, _ = await tool_call(ctx, "status.skills", {})
        publishing, _ = await tool_call(ctx, "status.publishing", {})
    finally:
        await lease.release(session_status="idle")
    assert "workspace_balance" in credits and "this_month" in credits, credits
    assert resources["cloud_desktop"]["state"] == "not_provisioned", resources
    assert isinstance(skills["my_skills"], list) and isinstance(skills["platform_skills"], list), skills
    assert publishing["jobs"] == [], publishing
