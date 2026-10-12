"""Focused SQL coverage for conversation setup and voice video decisions."""
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import select

from assistant.model_choices import KIND
from assistant.request_answers import list_waiting
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from question import question as q
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, card, conversation, main_turn, tool_call
from tests.unit.test_assistant_request_answers import ask, asset
from tests.unit.test_assistant_placement import counts
from tests.unit.test_voice_cards import Heard
from voice.cards import CardDesk
from voice.tools import CallScope, run


CATALOGUE = {"chat": [{"id": "test/model", "name": "Task AI", "vision": True}],
    "video": [{"id": "MiniMax-H3-Max-Turbo", "name": "MiniMax Turbo", "resolutions": ["768p", "480p"]},
              {"id": "wan3.0", "name": "Wan 3.0", "resolutions": ["720p", "1080p"]}]}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "voice-video-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr("assistant.model_choices.catalogue", lambda: CATALOGUE)
    MAIN_MESSAGES.clear()


@pytest.mark.parametrize("video_index", [0, 1])
async def test_models_are_selected_before_creation_and_spoken_skill_is_preserved(video_index):
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "在默认空间新开对话，帮我制作一段视频")
    args = {"project_id": main.project_id, "title": "口播样片", "instructions": "做 5 秒咖啡口播",
            "source_message_ids": [human], "destination_confirmed": True, "work_type": "video"}
    try:
        before = await counts(owner)
        for field, choice_index in (("model", 0), ("work_type", 0), ("video_model", video_index)):
            result, _ = await tool_call(ctx, "tasks.submit", args)
            row = await card(result["suspended"])
            assert row.continuation[KIND]["field"] == field
            assert await counts(owner) == before
            await q.reply(row.id, [[row.questions[0]["options"][choice_index]["label"]]], owner)
        receipt, _ = await tool_call(ctx, "tasks.submit", args)
        assert receipt["state"] == "accepted"
        async with get_db_session() as db:
            session = await db.get(Session, receipt["execution_session_id"])
            item = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == session.id))
            assert session.model == "test/model"
            expected = CATALOGUE["video"][video_index]
            assert session.video_model == expected["id"]
            assert session.video_resolution == expected["resolutions"][0]
            assert item.origin_ref["workflow"] == "video-production"
            assert "video-production" in item.prompt
        after = await counts(owner)
        assert after == (before[0], before[1] + 1, before[2] + 1)
        assert (await tool_call(ctx, "tasks.submit", args))[0] == receipt
        assert await counts(owner) == after
    finally:
        await lease.release(session_status="idle")


async def test_setup_cancel_and_changed_work_never_create_or_reuse_selection():
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "做视频")
    args = {"project_id": main.project_id, "title": "视频", "instructions": "咖啡视频", "source_message_ids": [human]}
    try:
        result, _ = await tool_call(ctx, "tasks.submit", args)
        await q.reply(result["suspended"], [["取消"]], owner)
        assert (await tool_call(ctx, "tasks.submit", args))[0] == {"state": "cancelled"}
        other, _ = await tool_call(ctx, "tasks.submit", {**args, "instructions": "制作茶叶介绍"})
        assert other["suspended"] != result["suspended"]
        assert (await counts(owner))[2] == 0
    finally:
        await lease.release(session_status="idle")


async def voice_form():
    owner, other, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "口播制作", visibility="private")
    request_id = await ask(session, owner, questions=[
        q.Question(question="使用哪些图片？", options=[{"label": "直接使用"}, {"label": "参考生成首尾帧"}],
                   allow_attachments=True, custom=False),
        q.Question(question="字幕怎么处理？", options=[{"label": "中文"}, {"label": "无字幕"}], custom=False)])
    heard = Heard()
    desk = CardDesk(heard)
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id, call_id="voice-test", desk=desk)
    [item] = await list_waiting(user_id=owner, workspace_id=workspace, main_id=main.id)
    desk.show_question(item, "zh")
    return owner, other, workspace, main, request_id, heard, scope, item


async def test_voice_answers_full_form_with_chosen_owned_picture_without_duplicate_confirmation():
    owner, _, workspace, main, request_id, heard, scope, item = await voice_form()
    image = await asset(owner, workspace, main)
    args = {"request_id": request_id, "version": item["version"],
            "answers": [["直接使用"], ["中文"]], "attachments": [[image], []]}
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "need_user_answer"
    heard.say("用刚才那张图片直接做，字幕用中文")
    partial = {**args, "answers": [["直接使用"]]}
    assert (await run("task_question_answer", scope, json.dumps(partial)))["status"] == "invalid_arguments"
    result = await run("task_question_answer", scope, json.dumps(args))
    assert result["status"] == "decision_recorded" and result["execution_confirmed"] is False
    saved = await card(request_id)
    assert saved.answers == args["answers"]
    assert saved.continuation["answer_attachments"] == args["attachments"]
    assert saved.continuation["answered_by"]["voice_call_id"] == "voice-test"
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "conflict"


async def test_voice_cannot_use_another_users_picture_or_a_stale_card(monkeypatch):
    owner, other, workspace, main, request_id, heard, scope, item = await voice_form()
    foreign = await asset(owner, workspace, main, user=other)
    args = {"request_id": request_id, "version": item["version"],
            "answers": [["直接使用"], ["中文"]], "attachments": [[foreign], []]}
    heard.say("就按这个做")
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "invalid_arguments"
    from assistant import requests
    original = requests.assistant_answer
    async def changed_before_lock(row, answers, **kwargs):
        async with get_db_session() as db:
            saved = await db.get(QuestionCheckpoint, row.id)
            saved.questions = [{**saved.questions[0], "question": "方案已改为生成 10 张图片"}, saved.questions[1]]
        return await original(row, answers, **kwargs)
    monkeypatch.setattr(requests, "assistant_answer", changed_before_lock)
    args["attachments"] = [[], []]
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "conflict"
    assert (await card(request_id)).status == "pending"


async def test_uploaded_draft_images_are_visible_to_voice_and_changes_invalidate_the_card():
    owner, other, workspace, main, request_id, heard, scope, old = await voice_form()
    image = await asset(owner, workspace, main)
    foreign = await asset(owner, workspace, main, user=other)
    async with get_db_session() as db:
        saved = await db.get(QuestionCheckpoint, request_id)
        saved.draft = [{"attachments": [image, foreign]}, {}]
    [fresh] = await list_waiting(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert fresh["version"] != old["version"]
    shown = scope.desk.show_question(fresh, "zh")
    assert [item["asset_id"] for item in shown["questions"][0]["draft_attachments"]] == [image]
    assert shown["questions"][1]["draft_attachments"] == []
    assert (await card(request_id)).status == "pending"
    assert scope.desk._question_shown[request_id] == heard()[0]


@pytest.mark.parametrize("encoded", [False, True])
async def test_flat_voice_attachments_bind_to_the_only_material_question(encoded):
    owner, _, workspace, main, request_id, heard, scope, item = await voice_form()
    image = await asset(owner, workspace, main)
    heard.say("直接使用图片，不要字幕")
    result = await run("task_question_answer", scope, json.dumps({
        "request_id": request_id, "version": item["version"],
        "answers": [["直接使用"], ["无字幕"]], "attachments": json.dumps([image]) if encoded else [image]}))
    assert result["status"] == "decision_recorded"
    assert (await card(request_id)).continuation["answer_attachments"] == [[image], []]


async def test_invalid_attachment_object_is_not_a_successful_decision():
    result = await run("task_question_answer", None, json.dumps({
        "request_id": "q1", "version": "v1", "answers": [["Use"]],
        "attachments": [{"asset_id": "not-a-list-of-ids"}]}))
    assert result["status"] == "invalid_arguments" and result["this_call_applied"] is False
    assert result["operation_state"] == "not_applied" and result["execution_confirmed"] is False
    assert result["field"].startswith("attachments")


async def test_flat_attachments_never_guess_between_multiple_material_questions():
    owner, _, workspace, main, request_id, heard, scope, _ = await voice_form()
    image = await asset(owner, workspace, main)
    async with get_db_session() as db:
        saved = await db.get(QuestionCheckpoint, request_id)
        saved.questions = [saved.questions[0], {**saved.questions[1], "allow_attachments": True}]
    [fresh] = await list_waiting(user_id=owner, workspace_id=workspace, main_id=main.id)
    scope.desk.show_question(fresh, "zh")
    heard.say("直接使用图片，不要字幕")
    result = await run("task_question_answer", scope, json.dumps({
        "request_id": request_id, "version": fresh["version"],
        "answers": [["直接使用"], ["无字幕"]], "attachments": [image]}))
    assert result["status"] == "invalid_arguments" and not result["this_call_applied"]
    assert (await card(request_id)).status == "pending"


async def test_picture_description_reads_owned_pixels_and_refuses_foreign_assets(monkeypatch):
    from assistant.asset_vision import describe
    from assistant.policy import AssistantError
    owner, other, workspace, main = await assistant()
    image = await asset(owner, workspace, main)
    foreign = await asset(owner, workspace, main, user=other)
    ctx = SimpleNamespace(user_id=owner, workspace_id=workspace, session_id=main.id)
    async def chunks(key, **kwargs):
        yield b"actual-picture"
    async def llm(*args, **kwargs):
        assert args[2][0]["_images"] == ["data:image/png;base64,YWN0dWFsLXBpY3R1cmU="]
        yield {"type": "text_delta", "text": "画面中是一只白色咖啡杯。"}
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(get_object_chunks=chunks))
    monkeypatch.setattr("agent.vision.supports_vision", lambda _: True)
    monkeypatch.setattr("agent.llm.stream_llm", llm)
    assert "白色咖啡杯" in (await describe(ctx, asset_id=image, question="是什么样的图片？"))["description"]
    with pytest.raises(AssistantError):
        await describe(ctx, asset_id=foreign, question="是什么图片？")


async def test_image_provider_preference_is_per_user_and_preserves_other_settings(monkeypatch):
    from db.repository.preference_repo import PgPreferenceRepo
    from session.image_pref import selected
    owner, other, _, _ = await assistant()
    repo = PgPreferenceRepo()
    await repo.upsert(owner, extra={"locale": "zh-CN"})
    saved = await repo.upsert(owner, extra={"image_generation_provider": "runninghub"})
    assert saved["image_generation_provider"] == "runninghub" and saved["extra"]["locale"] == "zh-CN"
    assert await selected(owner) == "runninghub" and await selected(other) == "default"


async def test_builtin_skill_exposes_runtime_paths_without_claiming_other_host_files_are_readable():
    from pathlib import Path
    from tool import skill_tool
    root = Path(skill_tool.__file__).resolve().parents[1] / ".openbox" / "skills"
    builtin = "\n".join(skill_tool._host_bundle_note("video-production", str(root / "video-production")))
    assert "/opt/openbox/skills/video-production/" in builtin and "NOT readable" not in builtin
    other = "\n".join(skill_tool._host_bundle_note("video-production", "/tmp/user/video-production"))
    assert "NOT readable" in other and "/opt/openbox/skills" not in other
