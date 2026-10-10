"""Answering for the user, relaxed for file choices and gated by risk (docs/ASSISTANT_VOICE_FIX_PLAN.md 3.1).

A file-choice question is the assistant's to answer: with an option that needs
no file, or with the user's own files. Such an answer, one about money,
publishing, authorization or deletion, and any answer in a workspace-visible
conversation waits for the user's confirmation card. Requests for the user's
own action stay with the user. The user's words asking for the answer are kept.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.confirmations import CONFIRM_KIND, pending_cards
from assistant.request_answers import HIGH_RISK, list_waiting
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.part import Part
from models.message import ToolPartData
from question import question as q, runtime
from question.continuation import apply_answers
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, card, conversation, main_turn, tool_call

PORTRAIT = [{"label": "上传人物照片", "description": "用你的照片生成人物画面"},
            {"label": "不需要，直接生成", "description": "由模型生成人物"}]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-request-answers-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


async def ask(session, owner, question="人物画面怎么准备？", options=PORTRAIT, questions=None, **fields):
    """An agent in `session` asks the user one question through the question tool and waits."""
    await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup", prompt="做个口播视频",
                                  origin="human", origin_ref={"actor_user_id": owner})
    lease = await reserve_run(session.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    ticket = await runtime.start_run(session.id, owner, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    try:
        fence = (session.id, lease.run_id, lease.generation)
        message = await create_assistant_message(session.id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=owner, run_fence=fence)
        part = ToolPartData(session_id=session.id, message_id=message.id, tool="question", status="running")
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        with pytest.raises(q.QuestionSuspended) as suspended:
            await q.ask(session.id, questions or [q.Question(question=question, options=options, **{"custom": False, **fields})],
                        {"messageID": message.id, "callID": part.id}, owner)
        message.finish = "waiting_input"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input")
    return suspended.value.request_id


async def asset(owner, workspace, main, name="主播.png", user=None):
    asset_id = "pa-asset-" + uuid4().hex
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=user or owner, workspace_id=workspace, session_id=main.id,
            project_id=main.project_id, name=name, oss_key=f"test/{asset_id}", mime="image/png", size=10,
            status="ready", created_at=datetime.now(timezone.utc)))
    return asset_id


async def test_multifield_spoken_choices_and_dictation_fill_the_exact_form_with_human_attribution():
    from tests.unit.test_voice_cards import FORM
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "50秒搞笑视频", visibility="private")
    questions = [q.Question(**{**field, "options": [{"label": label} for label in field["options"]]})
                 for field in FORM["questions"]]
    request_id = await ask(session, owner, questions=questions)
    ctx, lease, human = await main_turn(owner, workspace, main,
        "第一题选第一个，保留50秒。字幕中文和英文都要。片名叫打工人的离谱日常。")
    try:
        partial, _ = await tool_call(ctx, "requests.answer", {"request_id": request_id,
            "answers": [["保留50秒"]], "source_message_ids": [human]})
        assert partial["error"] == "ASSISTANT_ANSWER_INVALID" and (await card(request_id)).status == "pending"
        answers = [["保留50秒"], ["中文", "英文"], ["打工人的离谱日常"]]
        value, _ = await tool_call(ctx, "requests.answer", {"request_id": request_id,
            "answers": answers, "source_message_ids": [human]})
        assert value["state"] == "answered" and value["request_id"] == request_id and value["answers"] == answers
        saved = await card(request_id)
        assert saved.answers == answers and saved.status == "answered"
        assert [ref["message_id"] for ref in saved.continuation["answered_by"]["source_refs"]] == [human]
    finally:
        await lease.release(session_status="idle")


async def test_a_file_choice_without_files_is_answered_after_the_users_card():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "iPhone 18 口播视频", visibility="private")
    request_id = await ask(session, owner, allow_attachments=True)
    ctx, lease, human = await main_turn(owner, workspace, main, "你帮我选不需要，直接生成视频。")
    args = {"request_id": request_id, "answers": [["不需要，直接生成"]], "source_message_ids": [human]}
    try:
        asked, _ = await tool_call(ctx, "requests.answer", args)
        confirmation = await card(asked["suspended"])
        text = confirmation.questions[0]["question"]
        assert text == ("以你的名义回答「iPhone 18 口播视频」里的问题：人物画面怎么准备？ → 不需要，直接生成\n"
                        "影响：「iPhone 18 口播视频」会按这个回答继续执行；不提供文件，它会按所选方式直接做。")
        assert [option["label"] for option in confirmation.questions[0]["options"]] == ["确认代答", "取消"]
        assert confirmation.continuation[CONFIRM_KIND]["action"] == "request_answer"
        assert (await card(request_id)).status == "pending"
        [listed] = await pending_cards(owner, workspace, main.id)
        assert listed["card_id"] == confirmation.id and listed["action"] == "request_answer" and listed["high_risk"]
        await q.reply(confirmation.id, [["确认代答"]], owner, source_ref={"kind": "voice", "call_id": "call-1"})
        value, _ = await tool_call(ctx, "requests.answer", args)
    finally:
        await lease.release(session_status="idle")
    assert value["state"] == "answered" and value["answers"] == [["不需要，直接生成"]]
    row = await card(request_id)
    assert row.status == "answered" and row.answers == [["不需要，直接生成"]]
    answered_by = row.continuation["answered_by"]
    assert answered_by["kind"] == "assistant" and [ref["message_id"] for ref in answered_by["source_refs"]] == [human]
    assert await apply_answers(session.id, owner) is not None
    async with get_db_session() as db:
        part = await db.get(Part, row.part_id)
    assert part.data["title"] == "由个人助理代答" and part.data["metadata"]["answered_by"] == "assistant"


async def test_a_file_choice_with_the_users_own_files_names_them_and_delivers_them():
    owner, other, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "iPhone 18 口播视频", visibility="private")
    request_id = await ask(session, owner, allow_attachments=True)
    mine = await asset(owner, workspace, main)
    theirs = await asset(owner, workspace, main, name="成员的照片.png", user=other)
    ctx, lease, human = await main_turn(owner, workspace, main, "用我上传的主播照片。")
    args = {"request_id": request_id, "answers": [["上传人物照片"]], "attachments": [[mine]],
            "source_message_ids": [human]}
    try:
        for bad in ([[theirs]], [[mine], [mine]], [["missing-asset"]]):
            refused, _ = await tool_call(ctx, "requests.answer", {**args, "attachments": bad})
            assert refused["error"] == "ASSISTANT_ANSWER_INVALID", refused
        asked, _ = await tool_call(ctx, "requests.answer", args)
        text = (await card(asked["suspended"])).questions[0]["question"]
        assert "人物画面怎么准备？ → 上传人物照片、文件「主播.png」" in text and "会把 1 个文件交给它使用" in text
        await q.reply(asked["suspended"], [["确认代答"]], owner)
        # The confirmation covers exactly these files: the same answer without them asks again.
        other_files, _ = await tool_call(ctx, "requests.answer", {**args, "attachments": None})
        assert "suspended" in other_files and other_files["suspended"] != asked["suspended"]
        value, _ = await tool_call(ctx, "requests.answer", args)
    finally:
        await lease.release(session_status="idle")
    assert value["state"] == "answered" and value["attachments"] == [[{"asset_id": mine, "name": "主播.png"}]]
    row = await card(request_id)
    assert row.answers == [["上传人物照片"]] and row.continuation["answer_attachments"] == [[mine]]
    assert await apply_answers(session.id, owner) is not None
    async with get_db_session() as db:
        delivered = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == session.id,
            AgentInboxItem.origin == "system_recovery"))).all())
    assert [item.attachments for item in delivered] == [[mine]]


async def test_a_watched_tasks_file_choice_goes_through_the_versioned_command_with_its_files():
    from db.models.session import Session
    from tests.unit.test_assistant_steering import running
    from tool.question_tool import QuestionArgs, execute
    from tool.tool import ToolContext
    args, created, lease, batch = await running()
    owner, workspace = args["user_id"], args["workspace_id"]
    ticket = await runtime.start_run(lease.session_id, owner, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    try:
        fence = (lease.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(lease.session_id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=owner, run_fence=fence)
        part = ToolPartData(session_id=lease.session_id, message_id=message.id, tool="question", status="running")
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        with pytest.raises(q.QuestionSuspended) as suspended:
            await execute(QuestionArgs(questions=[{"question": "人物画面怎么准备？", "custom": False,
                "options": PORTRAIT, "allow_attachments": True}]), ToolContext(session_id=lease.session_id,
                user_id=owner, workspace_id=workspace, message_id=message.id, part_id=part.id))
        message.finish = "waiting_input"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input")
    request_id = suspended.value.request_id
    async with get_db_session() as db:
        main = await db.get(Session, args["main_id"])
    mine = await asset(owner, workspace, main)
    ctx, main_lease, human = await main_turn(owner, workspace, main, "用我的主播照片。")
    answer = {"request_id": request_id, "answers": [["上传人物照片"]], "attachments": [[mine]],
              "source_message_ids": [human]}
    try:
        asked, _ = await tool_call(ctx, "requests.answer", answer)
        await q.reply(asked["suspended"], [["确认代答"]], owner)
        value, _ = await tool_call(ctx, "requests.answer", answer)
    finally:
        await main_lease.release(session_status="idle")
    assert value["state"] == "answered" and value.get("command_id")
    from assistant.requests import decision_for
    async with get_db_session() as db:
        command = await decision_for(db, request_id)
    assert command.source_ref["kind"] == "assistant_answer"
    assert command.source_ref["decision"] == {"answers": [["上传人物照片"]], "attachments": [[mine]]}
    assert [ref["message_id"] for ref in command.source_ref["source_refs"]] == [human]
    assert await apply_answers(lease.session_id, owner) is not None
    row = await card(request_id)
    assert row.applied and row.continuation["answer_attachments"] == [[mine]]


async def test_high_risk_words_ask_first_and_requests_for_the_users_own_action_stay_refused():
    owner, _, workspace, main = await assistant()
    plain = await conversation(owner, workspace, main, "配色讨论", visibility="private")
    colors = await ask(plain, owner, question="页面用哪种配色？", options=[{"label": "深色"}, {"label": "浅色"}])
    publish = await conversation(owner, workspace, main, "抖音视频", visibility="private")
    release = await ask(publish, owner, question="现在发布到抖音吗？", options=[{"label": "发布"}, {"label": "先不发"}])
    channel = await conversation(owner, workspace, main, "发布抖音视频", visibility="private")
    cover = await ask(channel, owner, question="封面用哪张？", options=[{"label": "第一张"}, {"label": "第二张"}])
    login = await conversation(owner, workspace, main, "绑定账号", visibility="private")
    action = await ask(login, owner, question="请在手机上扫码", options=[{"label": "已完成"}],
                       detail={"kind": "user_action", "hint": "扫码登录"})
    waiting = {item["id"]: item for item in await list_waiting(user_id=owner, workspace_id=workspace, main_id=main.id)}
    assert waiting[colors]["assistant_may_answer"] and waiting[colors]["high_risk"] is False
    assert waiting[release]["high_risk"] is True and waiting[cover]["high_risk"] is True
    assert waiting[action]["assistant_may_answer"] is False and "high_risk" not in waiting[action]
    ctx, lease, human = await main_turn(owner, workspace, main, "这些都帮我选一下。")
    try:
        answered, _ = await tool_call(ctx, "requests.answer", {"request_id": colors, "answers": [["深色"]]})
        assert answered["state"] == "answered"
        for request_id, choice in ((release, "发布"), (cover, "第一张")):
            asked, _ = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [[choice]],
                                                                "source_message_ids": [human]})
            confirmation = await card(asked["suspended"])
            assert "这个问题涉及「发布」" in confirmation.questions[0]["question"]
            assert (await card(request_id)).status == "pending"
        refused, _ = await tool_call(ctx, "requests.answer", {"request_id": action, "answers": [["已完成"]]})
        assert refused["error"] == "ASSISTANT_ANSWER_HUMAN_ONLY"
        forged, _ = await tool_call(ctx, "requests.answer", {"request_id": action, "answers": [["已完成"]],
                                                             "source_message_ids": [ctx.message_id]})
        assert forged["error"] == "ASSISTANT_ANSWER_HUMAN_ONLY"
    finally:
        await lease.release(session_status="idle")
    assert (await card(action)).status == "pending"


@pytest.mark.parametrize("text,risky", [
    ("确认支付 39 元", True), ("要删除旧的草稿吗？", True), ("授权读取你的相册", True), ("Publish now?", True),
    ("Pay $5 to continue", True), ("清空购物车", True), ("预算 2000 块钱够吗", True), ("Total: ¥ 99", True),
    ("页面用深色还是浅色？", False), ("要不要加个标题？", False), ("Use the blue palette?", False),
    ("Which payload format, JSON or $FORMAT?", False)])
def test_high_risk_words(text, risky):
    assert bool(HIGH_RISK.search(text)) is risky
