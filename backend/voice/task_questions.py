"""Apply a spoken human selection to the exact task question shown in this call."""
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnswerArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    conversation: str = Field(min_length=1, max_length=512,
        description="The exact conversation title of the task the user is answering, from cards_pending. "
        "Resolve the user's intended task first, independently of the latest announced card. "
        "A title/request mismatch is rejected; re-read cards_pending rather than copying another task's ID.")
    answers: list[list[str]] = Field(min_length=1, max_length=20,
        description="One list per displayed question, in question order. Preserve exact option labels; "
        "custom text only when that question permits it. Resolve the user's meaning in context, never assume defaults.")
    attachments: list[list[str]] | list[str] | None = Field(default=None, max_length=20,
        description="Resource IDs chosen by the user. With one attachment-enabled question, a flat ID list is accepted. "
        "Otherwise provide one resource-ID list per question. Only for attachment-enabled questions. "
        "Use resources_list IDs or the actual uploaded resource IDs; never filenames or invented IDs.")

    @field_validator("attachments", mode="before")
    @classmethod
    def decode_attachments(cls, value):
        # Audio 3.1 can JSON-encode an optional array a second time. Decode only
        # its transport representation; normal form/ownership checks still apply.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError("attachments must be an array of resource IDs") from None
        return value


def schema():
    value = AnswerArguments.model_json_schema()
    # Present one concrete shape to realtime models, rather than an untyped
    # optional anyOf. Compatibility decoding belongs only on the receiving side.
    value["properties"]["attachments"] = {
        "type": "array", "maxItems": 20,
        "items": {"type": "array", "items": {"type": "string"}},
        "description": 'One resource-ID array per question, e.g. [["asset_ID"], []]. Omit when no files are selected. '
                       "Copy IDs from resources_list or draft_attachments; never filenames or objects."}
    return value


async def answer(scope, arguments):
    from assistant.request_answers import list_waiting, _target, answerable
    from assistant.requests import assistant_answer
    from assistant.commands import _authority
    from assistant.policy import AssistantError
    from db.base import get_db_session
    from question import question as q
    args = AnswerArguments.model_validate(arguments)
    desk = scope.desk
    async with desk.answer_lock:
        shown = desk.questions.get(args.request_id)
        if not shown or not args.version or shown.get("version") != args.version:
            return {"status": "conflict", "this_call_applied": False, "next": "cards_pending",
                    "reason": "question_not_read" if not shown else "question_version_changed",
                    "note": "没有提交。先查询用户所指任务的当前卡片，核对ID、版本和完整选项，再处理本轮原话。"
                            "普通问题已有明确选择时不必要求用户再说一遍；高风险决定仍需先展示再确认。"}
        if args.conversation != shown.get("conversation"):
            return {"status": "target_mismatch", "this_call_applied": False, "execution_confirmed": False,
                    "next": "cards_pending", "note": "目标任务名称与卡片不一致，没有提交。重新查询用户正在回答的任务，不沿用最近播报的卡片。"}
        count, words = desk.heard()
        if scope.utterance_index is not None:
            count, words = scope.utterance_index, scope.transcript
        if not words.strip() or count < 1:
            return {"status": "need_user_answer", "this_call_applied": False}
        current = next((item for item in await list_waiting(user_id=scope.user_id,
            workspace_id=scope.workspace_id, main_id=scope.main_session_id) if item["id"] == args.request_id), None)
        if not current or current.get("version") != args.version:
            return {"status": "conflict", "this_call_applied": False, "next": "cards_pending"}
        async with get_db_session() as db:
            await _authority(db, user_id=scope.user_id, workspace_id=scope.workspace_id, main_id=scope.main_session_id)
            # _target takes a ToolContext-like identity, never a model-provided actor.
            from types import SimpleNamespace
            ctx = SimpleNamespace(user_id=scope.user_id, workspace_id=scope.workspace_id, session_id=scope.main_session_id)
            row, session = await _target(db, ctx, args.request_id)
            if args.conversation != session.title:
                return {"status": "target_mismatch", "this_call_applied": False,
                        "execution_confirmed": False, "next": "cards_pending"}
            reason, high_risk = await answerable(db, row, session)
            if reason:
                return {"status": "human_only", "this_call_applied": False, "link": current["link"]}
            items = [q.Question(**item) for item in row.questions]
            try:
                attachments = args.attachments
                # A script-only card cannot carry a newly chosen picture.
                # Do not discard the file or submit only the old yes/no fields:
                # route the complete human request for task revision instead.
                selected = list(dict.fromkeys(identity for values in attachments or []
                    for identity in ([values] if isinstance(values, str) else values)))
                if selected and not any(item.allow_attachments for item in items):
                    await q.validate_attachment_ownership(db, session, [selected])
                    return {"status": "revision_required", "this_call_applied": False,
                            "execution_confirmed": False, "request_id": row.id,
                            "attachment_ids": selected,
                            "note": "所选素材属于对原任务的修改，旧卡没有提交；交接完整原话和素材继续处理。"}
                shown_at = desk._question_shown.get(args.request_id)
                # A user can choose a routine style on screen before the call
                # reads that card. A fresh read in the same utterance need not
                # make them repeat it. Consequential choices still require a
                # subsequent answer; an older utterance never answers new data.
                if shown_at is None or count < shown_at or (high_risk and count == shown_at):
                    return {"status": "need_user_answer", "this_call_applied": False}
                if attachments and isinstance(attachments[0], str):
                    targets = [index for index, item in enumerate(items) if item.allow_attachments]
                    if len(targets) != 1:
                        raise ValueError("Multiple material questions require one attachment list per question")
                    attachments = [attachments if index == targets[0] else [] for index in range(len(items))]
                files = q.normalize_attachments(items, attachments)
                clean = q.validate_answers(items, args.answers, attachments=files)
                await q.validate_attachment_ownership(db, session, files)
            except ValueError as exc:
                return {"status": "invalid_arguments", "this_call_applied": False, "execution_confirmed": False,
                        "operation_state": "not_applied", "reason": str(exc),
                        "note": "答案或素材未通过验证，未提交。核对原卡片的完整选项和素材后再提交，不要宣称已记录。"}
        try:
            await assistant_answer(row, clean, attachments=files, expected_version=args.version, answered_by={
                "kind": "assistant", "main_session_id": scope.main_session_id,
                "voice_call_id": scope.call_id, "utterance_index": count,
                "human_words": words, "shown_version": args.version})
        except (q.QuestionGone, q.QuestionConflict, AssistantError):
            return {"status": "conflict", "this_call_applied": False, "next": "cards_pending"}
        desk.questions.pop(args.request_id, None)
        desk._question_shown.pop(args.request_id, None)
        return {"status": "decision_recorded", "this_call_applied": True, "request_id": args.request_id,
                "conversation": session.title, "session_id": session.id,
                "link": current["link"], "execution_confirmed": False,
                "note": "回答和所选素材已交回原任务；任务还需继续执行，不能说视频已生成。"}
