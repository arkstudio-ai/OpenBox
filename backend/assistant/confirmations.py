"""The user confirms on a card before the assistant takes a high-risk action.

V2 decision D4 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 6.3): members can read a
workspace-visible conversation, so text the assistant sends there is shown to
the user first as a question card in the main session (``assistant_send``).
docs/ASSISTANT_VOICE_FIX_PLAN.md 1 uses the same card for every other
high-risk action (``assistant_confirm``): deleting a project, a conversation
or a task, and a high-risk answer for the user. Its first line says what will
be done, the second line the impact; the user answers on the card, or by voice
in a call (pending_cards, then question.question.reply).

Only an answered card for exactly this input lets the same input through,
once, and only while it is fresh: a card unanswered for ten minutes expires,
and the next call asks again; a confirmation not used within half an hour
(the assistant resumes right after the answer) is no consent any more. A
private conversation needs no send card.
"""
from datetime import timedelta

from sqlalchemy import select

from assistant.commands import _authority, command_digest, task_locked
from db.base import get_db_session
from db.models.question import QuestionCheckpoint, SessionExecution

KIND = "assistant_send"
CONFIRM_KIND = "assistant_confirm"
#: The continuation sub-keys of the main session's confirmation cards.
KINDS = (KIND, CONFIRM_KIND)
CONFIRM = "确认发送"
#: The confirm option of an assistant_confirm card.
CONFIRM_ACTION = "确认"
CANCEL = "取消"
#: An unanswered card expires after this.
EXPIRES_AFTER = timedelta(minutes=10)
#: A confirmation passes only this soon after the answer. Every other card of
#: the same turn expires first, so the resumed call always falls within it.
ANSWER_VALID_FOR = timedelta(minutes=30)


def input_digest(task_id: str, text: str, attachments=()) -> str:
    return command_digest({"task_id": task_id, "text": text, "attachments": sorted(attachments or ())})


def card_text(prompt: str, impact: str | None) -> str:
    """What will be done, then its impact on the second line (the same words a call reads aloud)."""
    return prompt if not impact else f"{prompt}\n影响：{impact}"


async def require_card(ctx, *, digest: str, prompt: str, header: str, description: str,
                       confirm: str | None = None, target: dict | None = None, kind: str = KIND,
                       action: str | None = None, impact: str | None = None,
                       high_risk: bool = True) -> str | None:
    """Return the consumed confirmed card for exactly this input, or ask for one.

    Asking suspends this tool call (QuestionSuspended). The user's answer
    becomes the call's output; calling again with the same input then passes,
    once. A confirmation older than ANSWER_VALID_FOR is no consent for now:
    the call asks again.
    """
    if kind not in KINDS:
        raise ValueError("Unknown confirmation card")
    confirm = confirm or (CONFIRM if kind == KIND else CONFIRM_ACTION)
    from question import runtime
    fresh_since = runtime.now() - ANSWER_VALID_FOR
    async with get_db_session() as db:
        rows = list((await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == ctx.session_id, QuestionCheckpoint.user_id == ctx.user_id,
            QuestionCheckpoint.status == "answered")
            .order_by(QuestionCheckpoint.updated_at.desc(), QuestionCheckpoint.id.desc())
            .limit(20).with_for_update())).all())
        for row in rows:
            card = (row.continuation or {}).get(kind) or {}
            if card.get("digest") != digest or card.get("consumed"):
                continue
            if runtime.utc(row.updated_at) < fresh_since:
                continue
            if (row.answers or [[]])[0] == [card.get("confirm", CONFIRM)]:
                row.continuation = {**row.continuation, kind: {**card, "consumed": True}}
                return row.id
    from question.question import Question, QuestionOption, ask
    card = {**(target or {}), "digest": digest, "confirm": confirm}
    if kind == CONFIRM_KIND:
        card.update(action=action, impact=impact, high_risk=high_risk)
    # The whole input is shown: confirming text the user has not seen is no confirmation.
    await ask(ctx.session_id, [Question(question=card_text(prompt, impact), header=header, custom=False,
        options=[QuestionOption(label=confirm, description=description),
                 QuestionOption(label=CANCEL, description="不进行")])],
        tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
        continuation={"kind": "question", kind: card}, expires_at=runtime.now() + EXPIRES_AFTER)
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
    """An action that failed after consuming its card leaves the card usable again."""
    if not question_id:
        return
    async with get_db_session() as db:
        row = await db.scalar(select(QuestionCheckpoint).where(QuestionCheckpoint.id == question_id,
            QuestionCheckpoint.session_id == ctx.session_id, QuestionCheckpoint.user_id == ctx.user_id)
            .with_for_update())
        for kind in KINDS:
            card = (row.continuation or {}).get(kind) if row is not None else None
            if card and card.get("consumed"):
                row.continuation = {**row.continuation, kind: {**card, "consumed": False}}


def confirmation_card(continuation: dict | None) -> dict | None:
    """The confirmation card a question checkpoint carries, if it is one."""
    continuation = continuation or {}
    if continuation.get("kind") != "question":
        return None
    return next((continuation[kind] for kind in KINDS if continuation.get(kind)), None)


async def pending_cards(user_id: str, workspace_id: str, main_id: str) -> list[dict]:
    """Cards in the main session waiting for the user, oldest first.

    For a call to read them aloud (docs/ASSISTANT_VOICE_FIX_PLAN.md 1.3): the
    action, the card text (what will be done, then "影响：…"), the exact option
    labels and whether the action is high-risk. A card is answered with
    question.question.reply(card_id, [[label]], user_id); the suspended tool
    call then continues as it does after an answer on the card.
    """
    from question import runtime
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        rows = list((await db.scalars(select(QuestionCheckpoint).join(
            SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id).where(
            QuestionCheckpoint.session_id == main_id, QuestionCheckpoint.user_id == user_id,
            QuestionCheckpoint.status == "pending", QuestionCheckpoint.generation == SessionExecution.generation)
            .order_by(QuestionCheckpoint.created_at, QuestionCheckpoint.id))).all())
    now, cards = runtime.now(), []
    for row in rows:
        if not row.questions or (row.expires_at and runtime.utc(row.expires_at) <= now):
            continue
        continuation = row.continuation or {}
        card = confirmation_card(continuation)
        if card is not None:
            kind = next(kind for kind in KINDS if continuation.get(kind))
            action = card.get("action") or ("request_answer" if card.get("request_id") else "task_send")
            high_risk = bool(card.get("high_risk", True))
        elif continuation.get("kind") == "memory_proposal":
            kind, card, action, high_risk = "memory_proposal", {}, "memory_remember", False
        else:
            continue
        question = row.questions[0]
        cards.append({"card_id": row.id, "kind": kind, "action": action,
                      "header": question.get("header", ""), "prompt": question.get("question", ""),
                      "impact": card.get("impact"),
                      "options": [option.get("label") for option in question.get("options") or []],
                      "created_at": runtime.utc(row.created_at).isoformat(),
                      "expires_at": runtime.utc(row.expires_at).isoformat() if row.expires_at else None,
                      "high_risk": high_risk})
    return cards
