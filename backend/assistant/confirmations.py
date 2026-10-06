"""The user confirms before the assistant writes into a workspace-visible chat.

V2 decision D4 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 6.3): members can read a
workspace-visible conversation, so text the assistant sends there is shown to
the user first as a question card in the main session. Only an answered card
for exactly this task and input lets the same input through, once. A private
conversation needs no card.
"""
from sqlalchemy import select

from assistant.commands import _authority, command_digest, task_locked
from db.base import get_db_session
from db.models.question import QuestionCheckpoint

KIND = "assistant_send"
CONFIRM = "确认发送"
CANCEL = "取消"


def input_digest(task_id: str, text: str, attachments=()) -> str:
    return command_digest({"task_id": task_id, "text": text, "attachments": sorted(attachments or ())})


async def require_card(ctx, *, digest: str, prompt: str, header: str, description: str,
                       confirm: str = CONFIRM, target: dict | None = None) -> str | None:
    """Return the consumed confirmed card for exactly this input, or ask for one.

    Asking suspends this tool call (QuestionSuspended). The user's answer
    becomes the call's output; calling again with the same input then passes,
    once.
    """
    async with get_db_session() as db:
        rows = list((await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == ctx.session_id, QuestionCheckpoint.user_id == ctx.user_id,
            QuestionCheckpoint.status == "answered")
            .order_by(QuestionCheckpoint.updated_at.desc(), QuestionCheckpoint.id.desc())
            .limit(20).with_for_update())).all())
        for row in rows:
            card = (row.continuation or {}).get(KIND) or {}
            if card.get("digest") != digest or card.get("consumed"):
                continue
            if (row.answers or [[]])[0] == [card.get("confirm", CONFIRM)]:
                row.continuation = {**row.continuation, KIND: {**card, "consumed": True}}
                return row.id
    from question.question import Question, QuestionOption, ask
    # The whole input is shown: confirming text the user has not seen is no confirmation.
    await ask(ctx.session_id, [Question(question=prompt, header=header, custom=False,
        options=[QuestionOption(label=confirm, description=description),
                 QuestionOption(label=CANCEL, description="不进行")])],
        tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
        continuation={"kind": "question", KIND: {**(target or {}), "digest": digest, "confirm": confirm}})
    return None


async def require_shared_send_confirmation(ctx, *, task_id: str, text: str, attachments=()) -> str | None:
    """A card before text goes into a workspace-visible task conversation; None for a private one."""
    async with get_db_session() as db:
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        task, execution = await task_locked(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                                            main_id=ctx.session_id, task_id=task_id)
        if execution.visibility != "workspace":
            return None
        title = execution.title or task.title
        names = []
        if attachments:
            from db.models.file_asset import FileAsset
            names = list((await db.scalars(select(FileAsset.name).where(FileAsset.id.in_(list(attachments)),
                FileAsset.user_id == ctx.user_id, FileAsset.workspace_id == ctx.workspace_id))).all())
    files = ("\n\n附带文件（成员也能看到）：" + "、".join(names)) if names else ""
    return await require_card(ctx, digest=input_digest(task_id, text, attachments),
        prompt=f"把下面这段话发送到工作区可见的会话「{title}」吗？工作区成员都能看到这条消息。\n\n{text}{files}",
        header="确认发送", description="由个人助理代你发送这段话", target={"task_id": task_id})


async def release_confirmation(ctx, question_id: str | None) -> None:
    """A send that failed after consuming its card leaves the card usable again."""
    if not question_id:
        return
    async with get_db_session() as db:
        row = await db.scalar(select(QuestionCheckpoint).where(QuestionCheckpoint.id == question_id,
            QuestionCheckpoint.session_id == ctx.session_id, QuestionCheckpoint.user_id == ctx.user_id)
            .with_for_update())
        send = (row.continuation or {}).get(KIND) if row is not None else None
        if send and send.get("consumed"):
            row.continuation = {**row.continuation, KIND: {**send, "consumed": False}}
