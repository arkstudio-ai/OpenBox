"""Public model choices and resumable, human-selected new-conversation setup.

The model interprets intent. This module deals only in configured model IDs and
persisted card choices; no transcript is classified with words or expressions.
"""
import json

from sqlalchemy import select

from assistant.commands import _authority, _tool_source_locked, command_digest
from assistant.confirmations import CANCEL
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.question import QuestionCheckpoint

KIND = "assistant_task_setup"


def catalogue():
    from core.config import get_config
    from api.metadata import _chat_models, _video_models
    from agent.compaction import get_model_context_limit
    from agent.vision import supports_vision
    config = get_config()
    videos = _video_models(config)
    for video in videos:
        entry = next((row for row in config.video_generation.models if row.id == video["id"]), None)
        if entry:
            video.update({key: getattr(entry, key) for key in ("duration_range", "ratios",
                "supports_generated_audio", "supports_reference_image", "supports_reference_video",
                "supports_first_last_frame")})
        resolutions = video.get("resolutions") or []
        default = config.video_generation.default_resolution
        video["default_resolution"] = default if default in resolutions else next(iter(resolutions), None)
    return {"chat": _chat_models(config, context_limit=get_model_context_limit,
                                  supports_vision=supports_vision),
            "video": videos, "default_chat": config.model,
            "default_video": config.video_generation.model,
            "note": "Defaults are recommendations, not this user's choice. Capabilities are not quality guarantees."}


async def prepare(ctx, source, arguments):
    """Resolve successive choices against the same original request; create nothing.

    Repeat the original arguments after every answer. Changed work gets fresh
    questions rather than reusing an unrelated task's approval.
    """
    original = dict(arguments)
    values = dict(arguments)
    setup_id = None
    inventory = catalogue()
    async with get_db_session() as db:
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        await _tool_source_locked(db, main, source, "task_create")
        rows = (await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == main.id, QuestionCheckpoint.user_id == main.user_id,
            QuestionCheckpoint.status == "answered").order_by(QuestionCheckpoint.updated_at.desc()).limit(100))).all()
    for field in ("model", "work_type", "video_model"):
        if field == "model" and values.get("model_confirmed") and values.get("model"):
            continue
        if field == "work_type" and values.get("work_type") != "video":
            continue
        if field == "video_model" and values.get("work_type") not in {"spoken_video", "other_video"}:
            continue
        if field == "video_model" and values.get("video_model_confirmed") and values.get("video_model"):
            continue
        digest = command_digest({"input": original, "resolved": values, "field": field})
        answered = next((row for row in rows if ((row.continuation or {}).get(KIND) or {}).get("digest") == digest), None)
        if answered:
            card = answered.continuation[KIND]
            selected = (answered.answers or [[]])[0]
            if len(selected) != 1 or selected[0] not in card["choices"]:
                raise AssistantError(409, "ASSISTANT_SETUP_CHANGED", "The model choice is no longer available")
            patch = card["choices"][selected[0]]
            if patch is None:
                return None
            values.update(patch)
            setup_id = answered.id
            continue
        from question.question import Question, QuestionOption, ask
        if field == "work_type":
            choices = {"口播视频": {"work_type": "spoken_video"}, "其他类型视频": {"work_type": "other_video"}}
            descriptions = {"口播视频": "以人物对着镜头讲解为主，也可以搭配演示画面", "其他类型视频": "例如场景展示、产品镜头或故事短片"}
            question = values.get("video_kind_question") or "这条视频你想让人物对着镜头讲解，还是主要展示画面？"
            title = "视频形式"
        else:
            items = inventory["chat" if field == "model" else "video"]
            if not items:
                raise AssistantError(503, "ASSISTANT_MODELS_UNAVAILABLE", "No models are currently configured")
            # Duplicate display names still have distinct, selectable identities.
            names = [item["name"] for item in items]
            labels = {item["id"]: item["name"] if names.count(item["name"]) == 1 else
                      f'{item["name"]} ({item["id"]})' for item in items}
            choices = {labels[item["id"]]: {field: item["id"],
                       "model_confirmed" if field == "model" else "video_model_confirmed": True} for item in items}
            descriptions = {labels[item["id"]]: (
                "支持查看图片" if item.get("vision") else "处理文字任务") if field == "model" else
                " / ".join(item.get("resolutions") or []) +
                (f'；单段最长 {item["max_duration_seconds"]} 秒' if item.get("max_duration_seconds") else "") for item in items}
            title = "任务 AI 模型" if field == "model" else "视频模型"
            question = values.get("model_question" if field == "model" else "video_model_question") or (
                "这个新对话，你想交给哪个 AI 来做？我可以解释可选模型，或根据需求给你建议。" if field == "model" else
                "这条视频你想用哪个模型制作？可以先了解它们支持的时长和画面规格，再决定。")
        choices[CANCEL] = None
        await ask(ctx.session_id, [Question(header=title, question=question, custom=False,
            options=[QuestionOption(label=label, description=descriptions.get(label, "暂不创建会话")) for label in choices])],
            tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
            continuation={"kind": "question", KIND: {"digest": digest, "input": original,
                "field": field, "choices": choices}})
    if values["model"] not in {row["id"] for row in inventory["chat"]}:
        raise AssistantError(400, "ASSISTANT_MODEL_UNAVAILABLE", "Read status.models and choose an available task model")
    if values.get("video_model") and values["video_model"] not in {row["id"] for row in inventory["video"]}:
        raise AssistantError(400, "ASSISTANT_VIDEO_MODEL_UNAVAILABLE", "Read status.models and choose an available video model")
    if values.get("video_model"):
        video = next(row for row in inventory["video"] if row["id"] == values["video_model"])
        resolutions = video.get("resolutions") or []
        if values.get("video_resolution") and values["video_resolution"] not in resolutions:
            raise AssistantError(400, "ASSISTANT_VIDEO_RESOLUTION_UNAVAILABLE", "Ask the user for a resolution supported by their selected model")
        if not values.get("video_resolution") and resolutions:
            values["video_resolution"] = video.get("default_resolution") or resolutions[0]
    # Internal identity, never a model-provided argument. Retries in a new tool
    # part still create only one conversation for the completed setup card.
    values["_setup_id"] = setup_id
    return values


def result(card, answers):
    choice = (answers or [[]])[0]
    cancelled = choice == [CANCEL]
    return {"title": "已取消" if cancelled else "已记录选择",
            "output": ("The user cancelled. Do not create work." if cancelled else
                "The user's selection was recorded. No conversation or task has been created. "
                "Call tasks.submit again with exactly these original arguments; saved choices will be applied: "
                + json.dumps(card["input"], ensure_ascii=False)),
            "metadata": {"task_setup": "cancelled" if cancelled else "selected"}}
