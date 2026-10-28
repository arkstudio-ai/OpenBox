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


@pytest.fixture
def tiers(monkeypatch):
    value = {**CATALOGUE, "video_tiers": [{"id": "fast", "name": "快速档", "model": "MiniMax-H3-Max-Turbo",
        "resolution": "768p", "resolutions": ["480p", "768p"], "credits_per_second": {"768p": "0.77"},
        "description": "支持第一张和结束图片"}]}
    monkeypatch.setattr("assistant.model_choices.catalogue", lambda: value)
    return value


@pytest.mark.parametrize("question_field", ["video_model_question", "video_tier_question"])
@pytest.mark.parametrize("work_type", ["spoken_video", "other_video"])
async def test_tier_question_is_not_creation_and_answer_binds_exact_model_once(tiers, question_field, work_type):
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "默认空间里用Task AI做咖啡口播，先解释档次")
    args = {"project_id": main.project_id, "title": "档次样片", "instructions": "咖啡口播",
            "source_message_ids": [human], "destination_confirmed": True, "work_type": work_type,
            "model": "test/model", "model_confirmed": True,
            question_field: "这条咖啡口播视频想用哪个档次？"}
    try:
        before = await counts(owner)
        result, _ = await tool_call(ctx, "tasks.submit", args)
        pending = await card(result["suspended"])
        assert pending.questions[0]["header"] == "视频档次"
        assert pending.questions[0]["question"] == "这条咖啡口播视频想用哪个档次？"
        assert "video_tier_question" not in pending.continuation[KIND]["input"]
        option = pending.questions[0]["options"][0]
        assert option["label"] == "快速档" and "0.77" not in option["description"] and "积分" not in option["description"]
        assert "MiniMax" not in option["description"] and "768p" in option["description"]
        assert await counts(owner) == before
        await q.reply(pending.id, [["快速档"]], owner)
        receipt, _ = await tool_call(ctx, "tasks.submit", args)
        async with get_db_session() as db:
            saved = await db.get(Session, receipt["execution_session_id"])
            assert (saved.video_model, saved.video_resolution) == ("MiniMax-H3-Max-Turbo", "768p")
            inbound = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == saved.id))
            assert inbound.origin_ref["confirmed_setup"] == {"task_model": "test/model",
                "video_model": "MiniMax-H3-Max-Turbo", "video_resolution": "768p"}
            assert (inbound.origin_ref.get("workflow") == "video-production") == (work_type == "spoken_video")
            from agent.input_origin import provider_text
            rendered = provider_text(inbound.prompt, origin=inbound.origin, reference=inbound.origin_ref)
            assert "supersede older draft wording" in rendered and "neither paid" in rendered
        assert (await tool_call(ctx, "tasks.submit", args))[0] == receipt
    finally:
        await lease.release(session_status="idle")


async def test_retired_chat_model_keeps_answered_fast_tier_and_setup_identity(tiers):
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "做开业短片，快速档，其他按原来的来")
    args = {"project_id": main.project_id, "title": "开业短片", "instructions": "做开业短片",
        "source_message_ids": [human], "destination_confirmed": True, "work_type": "other_video",
        "model": "test/model", "model_confirmed": True}
    try:
        asked, _ = await tool_call(ctx, "tasks.submit", args)
        await q.reply(asked["suspended"], [["快速档"]], owner)
        tiers["chat"] = [{"id": "test/replacement", "name": "Current task AI"}]
        unavailable, _ = await tool_call(ctx, "tasks.submit", args)
        assert unavailable["error"] == "ASSISTANT_MODEL_UNAVAILABLE"
        replacement = {**args, "model": "test/replacement"}
        receipt, _ = await tool_call(ctx, "tasks.submit", replacement)
        assert receipt["state"] == "accepted"
        assert (await tool_call(ctx, "tasks.submit", replacement))[0] == receipt
        async with get_db_session() as db:
            session = await db.get(Session, receipt["execution_session_id"])
            assert session.model == "test/replacement"
            assert (session.video_model, session.video_resolution) == ("MiniMax-H3-Max-Turbo", "768p")
        assert (await counts(owner))[2] == 1
    finally:
        await lease.release(session_status="idle")


async def test_changed_tier_mapping_cannot_silently_replace_an_answered_choice(tiers):
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "做口播，档次我选")
    args = {"project_id": main.project_id, "title": "旧卡", "instructions": "咖啡口播",
            "source_message_ids": [human], "destination_confirmed": True, "work_type": "spoken_video",
            "model": "test/model", "model_confirmed": True}
    try:
        result, _ = await tool_call(ctx, "tasks.submit", args)
        await q.reply(result["suspended"], [["快速档"]], owner)
        tiers["video_tiers"][0]["model"] = "wan3.0"
        result, _ = await tool_call(ctx, "tasks.submit", args)
        assert result["error"] == "ASSISTANT_VIDEO_TIER_CHANGED"
        assert (await counts(owner))[2] == 0
    finally:
        await lease.release(session_status="idle")


async def test_confirmed_tier_preserves_explicit_resolution_without_another_card(tiers):
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "默认空间用快速档480p做口播，Task AI来执行")
    try:
        result, _ = await tool_call(ctx, "tasks.submit", {"project_id": main.project_id, "title": "快速档",
            "instructions": "咖啡口播", "source_message_ids": [human], "destination_confirmed": True,
            "work_type": "spoken_video", "model": "test/model", "model_confirmed": True,
            "video_tier": "fast", "video_tier_confirmed": True, "video_resolution": "480p"})
        assert result["state"] == "accepted"
        async with get_db_session() as db:
            saved = await db.get(Session, result["execution_session_id"])
            assert (saved.video_model, saved.video_resolution) == ("MiniMax-H3-Max-Turbo", "480p")
    finally:
        await lease.release(session_status="idle")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "voice-video-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr("assistant.model_choices.catalogue", lambda: CATALOGUE)
    MAIN_MESSAGES.clear()


@pytest.mark.parametrize("video_index", [0, 1])
@pytest.mark.parametrize("semantic_recall", [False, True])
async def test_models_are_selected_before_creation_and_spoken_skill_is_preserved(video_index, semantic_recall, monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config().memory, "semantic_recall", semantic_recall)
    monkeypatch.setattr(get_config().memory, "allowed_user_ids", [])
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
            if field == "model":
                from assistant.shared_context import recent_decisions
                from assistant.projection import project_main_messages
                from assistant.model_choices import result as setup_result
                async with get_db_session() as db:
                    decisions = await recent_decisions(db, owner, workspace)
                assert decisions[0]["answers"] == [["Task AI"]]
                assert decisions[0]["selected_settings"] == {"model": "test/model", "model_confirmed": True}
                assert decisions[0]["task_title"] == "口播样片" and not decisions[0]["execution_confirmed"]
                assert "Task AI" in setup_result(row.continuation[KIND], [["Task AI"]])["output"]
                projected = await project_main_messages([], ctx=ctx)
                # Semantic recall replaces legacy background. Current bound-card
                # receipts are covered separately with the resumed Driver turn.
                has_legacy_answer = any(m.id == "assistant:answered-decisions" and "test/model" in str(m.parts)
                                        for m in projected)
                assert has_legacy_answer is not semantic_recall
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
    args = {"request_id": request_id, "version": item["version"], "conversation": item["session_title"],
            "answers": [["直接使用"], ["中文"]], "attachments": [[image], []]}
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "need_user_answer"
    heard.say("用刚才那张图片直接做，字幕用中文")
    partial = {**args, "answers": [["直接使用"]]}
    assert (await run("task_question_answer", scope, json.dumps(partial)))["status"] == "invalid_arguments"
    result = await run("task_question_answer", scope, json.dumps(args))
    assert result["status"] == "decision_recorded" and result["execution_confirmed"] is False
    assert result["conversation"] == item["session_title"] and result["session_id"] == item["session_id"]
    saved = await card(request_id)
    assert saved.answers == args["answers"]
    assert saved.continuation["answer_attachments"] == args["attachments"]
    assert saved.continuation["answered_by"]["voice_call_id"] == "voice-test"
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "conflict"


async def test_voice_rejects_another_tasks_card_even_when_version_and_answers_fit():
    owner, _, workspace, main, old_id, heard, scope, old = await voice_form()
    second = await conversation(owner, workspace, main, "口播交接复测", visibility="private")
    new_id = await ask(second, owner, questions=[q.Question(question="完整讲稿可以吗？")])
    fresh = next(row for row in await list_waiting(user_id=owner, workspace_id=workspace, main_id=main.id)
                 if row["id"] == new_id)
    scope.desk.show_question(fresh, "zh")
    heard.say("口播交接复测的讲稿可以，保存后结束，不生成视频")
    args = {"request_id": old_id, "version": old["version"], "conversation": fresh["session_title"],
            "answers": [["直接使用"], ["无字幕"]]}
    result = await run("task_question_answer", scope, json.dumps(args))
    assert result["status"] == "target_mismatch" and not result["this_call_applied"]
    assert (await card(old_id)).status == (await card(new_id)).status == "pending"
    args.pop("conversation")
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "invalid_arguments"


async def test_voice_cannot_use_another_users_picture_or_a_stale_card(monkeypatch):
    owner, other, workspace, main, request_id, heard, scope, item = await voice_form()
    foreign = await asset(owner, workspace, main, user=other)
    args = {"request_id": request_id, "version": item["version"], "conversation": item["session_title"],
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
        "request_id": request_id, "version": item["version"], "conversation": item["session_title"],
        "answers": [["直接使用"], ["无字幕"]], "attachments": json.dumps([image]) if encoded else [image]}))
    assert result["status"] == "decision_recorded"
    assert (await card(request_id)).continuation["answer_attachments"] == [[image], []]


async def test_invalid_attachment_object_is_not_a_successful_decision():
    result = await run("task_question_answer", None, json.dumps({
        "request_id": "q1", "version": "v1", "conversation": "Task", "answers": [["Use"]],
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
        "request_id": request_id, "version": fresh["version"], "conversation": fresh["session_title"],
        "answers": [["直接使用"], ["无字幕"]], "attachments": [image]}))
    assert result["status"] == "invalid_arguments" and not result["this_call_applied"]
    assert (await card(request_id)).status == "pending"


async def test_new_picture_on_script_only_card_routes_revision_without_answering_old_plan():
    owner, other, workspace, main, request_id, heard, scope, _ = await voice_form()
    image = await asset(owner, workspace, main)
    foreign = await asset(owner, workspace, main, user=other)
    async with get_db_session() as db:
        saved = await db.get(QuestionCheckpoint, request_id)
        saved.questions = [{**question, "allow_attachments": False} for question in saved.questions]
    [fresh] = await list_waiting(user_id=owner, workspace_id=workspace, main_id=main.id)
    heard.say("讲稿可以，但改用这张图重新做")
    scope.desk.show_question(fresh, "zh")  # A revision may precede the card read.
    args = {"request_id": request_id, "version": fresh["version"], "conversation": fresh["session_title"],
            "answers": [["直接使用"], ["中文"]], "attachments": [[image], []]}
    result = await run("task_question_answer", scope, json.dumps(args))
    assert result["status"] == "revision_required" and not result["this_call_applied"]
    assert result["attachment_ids"] == [image]
    assert (await card(request_id)).status == "pending"
    args["attachments"] = [[foreign], []]
    assert (await run("task_question_answer", scope, json.dumps(args)))["status"] == "invalid_arguments"
    assert (await card(request_id)).status == "pending"


@pytest.mark.parametrize("finish", [None, "length", "stop"])
async def test_picture_description_reads_owned_pixels_and_refuses_foreign_assets(monkeypatch, finish):
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
        if finish:
            yield {"type": "finish", "reason": finish}
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(get_object_chunks=chunks))
    monkeypatch.setattr("agent.vision.supports_vision", lambda _: True)
    monkeypatch.setattr("agent.llm.stream_llm", llm)
    if finish == "stop":
        assert "白色咖啡杯" in (await describe(ctx, asset_id=image, question="是什么样的图片？"))["description"]
    else:
        with pytest.raises(AssistantError):
            await describe(ctx, asset_id=image, question="是什么样的图片？")
    with pytest.raises(AssistantError):
        await describe(ctx, asset_id=foreign, question="是什么图片？")


@pytest.mark.parametrize("retired", [False, True])
async def test_frame_comparison_uses_one_request_and_the_selected_model(monkeypatch, retired):
    from assistant.asset_vision import describe
    from datetime import datetime, timezone
    from core.identifier import generate_id
    from db.models.message import Message
    selected = "openai/gpt-5.6-luna" if retired else "openai/gemini-3.8-flash-high"
    if retired:
        from core.config import OpenBoxConfig
        config = OpenBoxConfig(model="openai/qwen3.8-flash", models=[{"id": "openai/qwen3.8-flash"}])
        monkeypatch.setattr("core.config.get_config", lambda: config)
    owner, _, workspace, main = await assistant()
    first, last = await asset(owner, workspace, main), await asset(owner, workspace, main)
    message_id = generate_id()
    async with get_db_session() as db:
        (await db.get(Session, main.id)).model = "test/another-default"
        db.add(Message(id=message_id, session_id=main.id, user_id=owner, role="assistant",
            model_id=selected, created_at=datetime.now(timezone.utc)))
    calls = []
    closed = []
    async def chunks(key, **kwargs):
        yield b"actual-image"
    async def llm(*args, **kwargs):
        calls.append(args)
        assert kwargs["variant"] == ("none" if retired else "low")
        try:
            yield {"type": "text_delta", "text": "第一张双手放下，第二张举起右手。"}
            yield {"type": "finish", "reason": "stop"}
        finally:
            closed.append(True)
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(get_object_chunks=chunks))
    monkeypatch.setattr("agent.vision.supports_vision", lambda _: True)
    monkeypatch.setattr("agent.llm.stream_llm", llm)
    ctx = SimpleNamespace(user_id=owner, workspace_id=workspace, session_id=main.id, message_id=message_id)
    result = await describe(ctx, asset_ids=[first, last], question="两张有什么区别？")
    assert len(calls) == 1 and len(calls[0][2][0]["_images"]) == 2
    assert calls[0][4] == ("openai/qwen3.8-flash" if retired else selected)
    assert [x["asset_id"] for x in result["images"]] == [first, last]
    assert closed == [True]


async def test_image_timeout_is_reported_as_read_failure_without_partial_description(monkeypatch):
    from assistant.asset_vision import describe
    from assistant.policy import AssistantError
    owner, _, workspace, main = await assistant()
    image = await asset(owner, workspace, main)
    closed = []
    async def chunks(key, **kwargs):
        yield b"actual-image"
    async def llm(*args, **kwargs):
        try:
            yield {"type": "text_delta", "text": "incomplete"}
            raise TimeoutError()
        finally:
            closed.append(True)
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(get_object_chunks=chunks))
    monkeypatch.setattr("agent.vision.supports_vision", lambda _: True)
    monkeypatch.setattr("agent.llm.stream_llm", llm)
    ctx = SimpleNamespace(user_id=owner, workspace_id=workspace, session_id=main.id)
    with pytest.raises(AssistantError) as error:
        await describe(ctx, asset_id=image, question="图片是什么？")
    assert error.value.code == "ASSISTANT_VISION_TIMEOUT" and closed == [True]


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


@pytest.mark.parametrize('legacy', [False, True])
async def test_confirmed_video_type_does_not_reopen_the_answered_model(legacy):
    """Production replay: the only changed argument was video -> spoken_video."""
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, '默认空间做两段15秒海鲜视频，无背景音乐')
    args = {'project_id': main.project_id, 'title': '海鲜视频', 'instructions': '两段15秒，单独砂锅分焗，中文，无背景音乐',
            'source_message_ids': [human], 'destination_confirmed': True, 'work_type': 'video',
            'model_question': '我想选择任务档位', 'video_model': 'wan3.0', 'video_model_confirmed': True}
    try:
        selected = []
        for label in ('Task AI', '口播视频'):
            result, _ = await tool_call(ctx, 'tasks.submit', args)
            selected.append(result['suspended'])
            await q.reply(result['suspended'], [[label]], owner)
        if legacy:
            async with get_db_session() as db:
                for identity in selected:
                    row = await db.get(QuestionCheckpoint, identity)
                    saved = dict(row.continuation[KIND])
                    saved.pop('resolved', None)
                    row.continuation = {**row.continuation, KIND: saved}
        changed = {**args, 'work_type': 'spoken_video'}
        receipt, _ = await tool_call(ctx, 'tasks.submit', changed)
        assert receipt['state'] == 'accepted'
        assert (await counts(owner))[2] == 1
        assert (await tool_call(ctx, 'tasks.submit', args))[0] == receipt
        assert (await tool_call(ctx, 'tasks.submit', changed))[0] == receipt
        async with get_db_session() as db:
            inbound = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == receipt['execution_session_id']))
            assert args['instructions'] in inbound.prompt
            assert inbound.origin_ref['confirmed_setup']['video_model'] == 'wan3.0'
        other, _ = await tool_call(ctx, 'tasks.submit', {**changed, 'instructions': '改成60秒旅游短片'})
        assert other['suspended'] not in selected
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('preferred,explicit,expected', [
    (None, None, 'test/default'), ('retired/model', None, 'test/default'),
    ('test/preferred', None, 'test/preferred'), ('test/preferred', 'test/model', 'test/model')])
async def test_new_task_uses_account_default_without_model_question(monkeypatch, preferred, explicit, expected):
    from db.models.preference import UserPreference
    inventory = {**CATALOGUE, 'default_chat': 'test/default', 'chat': [*CATALOGUE['chat'],
        {'id': 'test/default', 'name': 'Default'}, {'id': 'test/preferred', 'name': 'Preferred'}]}
    monkeypatch.setattr('assistant.model_choices.catalogue', lambda: inventory)
    owner, _, workspace, main = await assistant()
    async with get_db_session() as db:
        db.add(UserPreference(id='pref-' + owner, user_id=owner, default_model=preferred))
    ctx, lease, human = await main_turn(owner, workspace, main, '在默认空间按刚才的方案开始做')
    try:
        args = {'project_id': main.project_id, 'title': '已确认方案', 'instructions': '两段15秒，中文配音，不要背景音乐',
                'source_message_ids': [human], 'destination_confirmed': True, 'work_type': 'other_video',
                'video_model': 'wan3.0', 'video_model_confirmed': True}
        if explicit:
            args.update(model=explicit, model_confirmed=True)
        receipt, _ = await tool_call(ctx, 'tasks.submit', args)
        assert receipt['state'] == 'accepted'
        async with get_db_session() as db:
            assert (await db.get(Session, receipt['execution_session_id'])).model == expected
            assert await db.scalar(select(QuestionCheckpoint.id)) is None
        from assistant.status_tools import models
        assert (await models(user_id=owner, workspace_id=workspace, main_id=main.id))['default_chat'] == (
            preferred if preferred == 'test/preferred' else 'test/default')
    finally:
        await lease.release(session_status='idle')
