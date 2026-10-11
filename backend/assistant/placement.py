"""Choose where new work belongs before creating any project or conversation.

The card is bound to the complete task input and original human sources. Its
answer is applied inside task admission's transaction, using the card as the
idempotency key even when the model retries with a different tool call.
"""
import json
from uuid import uuid4

from sqlalchemy import select

from assistant.commands import _authority, _project, _tool_source_locked, command_digest, tool_command_key
from assistant.confirmations import ANSWER_VALID_FOR, CANCEL, EXPIRES_AFTER
from assistant.policy import AssistantError
from assistant.project_tools import _clean, existing_project
from core.identifier import generate_id
from db.base import get_db_session
from db.models.assistant import AssistantCommand
from db.models.project import Project
from db.models.question import QuestionCheckpoint
from question import runtime

KIND = "assistant_placement"


def task_input(*, project_id, title, prompt, attachments=(), model=None, continuation=None, source):
    return {"project_id": project_id, "title": title, "instructions": prompt,
            "attachment_ids": list(attachments), "model": model, "continuation": continuation,
            "source_message_ids": list(source.source_message_ids)}


async def require_placement(ctx, source, values: dict, *, destination_confirmed: bool = False) -> str | None:
    """None for an explicit destination; otherwise suspend or return the answered card ID."""
    digest = command_digest(values)
    async with get_db_session() as db:
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        project = await _project(db, values["project_id"], ctx.user_id, ctx.workspace_id)
        # An executor retry of an already accepted explicit call keeps the ordinary receipt path.
        prior = await db.scalar(select(AssistantCommand.id).where(
            AssistantCommand.assistant_session_id == main.id, AssistantCommand.actor_user_id == ctx.user_id,
            AssistantCommand.idempotency_key == tool_command_key(main.id, source.part_id)))
        if prior:
            return None
        rows = (await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == main.id, QuestionCheckpoint.user_id == ctx.user_id,
            QuestionCheckpoint.status == "answered")
            .order_by(QuestionCheckpoint.updated_at.desc()).limit(50))).all()
        for row in rows:
            card = (row.continuation or {}).get(KIND) or {}
            if card.get("digest") == digest and (card.get("receipt") or
                    runtime.utc(row.updated_at) >= runtime.now() - ANSWER_VALID_FOR):
                return row.id
        # The model resolves destination intent from conversation, while the server
        # checks authentic human provenance and the owned project. No text patterns.
        await _tool_source_locked(db, main, source, "task_create")
        if destination_confirmed:
            return None
        project_name = project.name
    from question.question import Question, QuestionOption, ask
    name = _clean(values["title"])
    options = [f"在「{project_name}」里新建对话", f"新建项目「{name}」", CANCEL]
    await ask(ctx.session_id, [Question(header="安排这件事", question=f"这件事放在哪里？\n任务：{values['title']}",
        custom=False, options=[QuestionOption(label=options[0], description="使用这个项目的文件和背景资料"),
            QuestionOption(label=options[1], description="新建项目，并在其中开一个对话"),
            QuestionOption(label=CANCEL, description="暂不创建项目或对话")])],
        tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
        continuation={"kind": "question", KIND: {"digest": digest, "input": values,
            "project_name": name, "options": options}}, expires_at=runtime.now() + EXPIRES_AFTER)
    return None


async def apply_locked(db, main, question_id: str, values: dict, source_ref: dict):
    """Resolve and create in the same transaction as the task; failure rolls everything back."""
    row = await db.scalar(select(QuestionCheckpoint).where(QuestionCheckpoint.id == question_id,
        QuestionCheckpoint.session_id == main.id, QuestionCheckpoint.user_id == main.user_id,
        QuestionCheckpoint.status == "answered").with_for_update())
    card = ((row.continuation or {}).get(KIND) or {}) if row else {}
    if (not card or card.get("digest") != command_digest(values)
            or runtime.utc(row.updated_at) < runtime.now() - ANSWER_VALID_FOR):
        raise AssistantError(409, "ASSISTANT_PLACEMENT_CHANGED", "Choose where this exact task belongs again")
    options = card.get("options") or []
    answers = row.answers or []
    if len(options) != 3 or len(answers) != 1 or len(answers[0]) != 1 or answers[0][0] not in options:
        raise AssistantError(409, "ASSISTANT_PLACEMENT_CHANGED", "The placement choice is unavailable")
    choice = answers[0][0]
    if choice == CANCEL:
        return None, row, {"state": "cancelled"}
    source_ref["placement"] = {"question_id": row.id, "choice": choice, "digest": card["digest"]}
    project_id = values["project_id"]
    if choice == options[1]:
        name = card["project_name"]
        project = await existing_project(db, user_id=main.user_id, workspace_id=main.workspace_id, name=name)
        if project is None:
            # Lazy directory creation is the normal project behavior. Use a
            # generated safe slug so renamed/reserved directories never collide.
            now = runtime.now()
            project = Project(id=generate_id(), user_id=main.user_id, workspace_id=main.workspace_id,
                name=name, slug=f"project-{uuid4().hex}", created_at=now, updated_at=now)
            db.add(project)
            await db.flush()
        project_id = project.id
    await _project(db, project_id, main.user_id, main.workspace_id)
    return project_id, row, None


def save_receipt(row, receipt: dict) -> None:
    card = row.continuation[KIND]
    row.continuation = {**row.continuation, KIND: {**card, "receipt": receipt}}


def result(card: dict, answers: list) -> dict:
    choice = answers[0][0] if answers and answers[0] else ""
    cancelled = choice == CANCEL
    return {"title": "已取消" if cancelled else "已选择安排位置",
        "output": ("The user cancelled. No project or conversation was created. Do not submit this task." if cancelled else
            "The user chose " + choice + (". Project name: " + card["project_name"] if choice == card["options"][1] else "") + ". Nothing has been created yet. Call tasks.submit again with exactly "
            "these original arguments; the server applies the choice atomically: " + json.dumps(card["input"], ensure_ascii=False)),
        "metadata": {"placement": "cancelled" if cancelled else "selected"}}
