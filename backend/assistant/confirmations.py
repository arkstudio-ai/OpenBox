"""Confirmation for consequential actions, and compatibility with old send cards.

An authorized task input goes directly to the user's own conversation, whether
private or workspace-visible. Visibility alone must not create a second
"confirm sending" decision. Existing send-card continuations remain bound to
their exact input and can consume their receipt once. High-risk actions still
use a fresh card explaining the action and its impact.
"""
from datetime import timedelta
import json

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
SEND_TOOLS = frozenset({"tasks.followup", "assets.attach"})


async def confirmed_sends(db, ctx, *, scope_only=False, inputs=None):
    """Bind a card-only continuation to its persisted call, never its display text.

    A new human input removes this binding. Consumed and expired receipts still
    restrict the old continuation's scope; they do not grant execution consent.
    """
    from db.models.agent_inbox import AgentInboxItem
    from db.models.part import Part
    from question import runtime
    if inputs is None:
        inputs = list(await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == ctx.session_id, AgentInboxItem.user_id == ctx.user_id,
            AgentInboxItem.run_id == ctx.run_id, AgentInboxItem.generation == ctx.run_generation,
            AgentInboxItem.state == "claimed")))
    if not inputs or any(item.origin != "system_recovery" or
            (item.origin_ref or {}).get("entrypoint") != "question_answer" for item in inputs):
        return []
    generations = {(item.origin_ref or {}).get("generation") for item in inputs}
    rows = await db.execute(select(QuestionCheckpoint, Part).join(
        Part, Part.id == QuestionCheckpoint.part_id).where(
        QuestionCheckpoint.session_id == ctx.session_id, QuestionCheckpoint.user_id == ctx.user_id,
        QuestionCheckpoint.generation.in_(generations), QuestionCheckpoint.detached.is_(False),
        QuestionCheckpoint.status == "answered", Part.session_id == ctx.session_id,
        Part.user_id == ctx.user_id, Part.type == "tool"))
    result = []
    for row, part in rows:
        card = (row.continuation or {}).get(KIND) or {}
        tool = part.canonical_tool_id or (part.data or {}).get("tool")
        saved = (part.data or {}).get("input") or {}
        if (tool not in SEND_TOOLS or not card.get("task_id") or
                (row.answers or [[]])[0] != [card.get("confirm", CONFIRM)] or
                card.get("digest") != input_digest(saved.get("task_id"), saved.get("text"),
                                                    saved.get("attachment_ids", []))):
            continue
        usable = not card.get("consumed") and runtime.utc(row.updated_at) >= runtime.now() - ANSWER_VALID_FOR
        if usable or scope_only:
            result.append({"confirmation_id": row.id, "tool": tool, "arguments": saved,
                           "usable": usable})
    return result


async def check_send_replay(ctx, operation, arguments, parameters):
    """Reject drift before asking a second card; do not substitute model input."""
    from assistant.policy import AssistantError
    async with get_db_session() as db:
        confirmed = await confirmed_sends(db, ctx, scope_only=True)
    if not confirmed:
        return
    matching = [item for item in confirmed if item["tool"] == operation]
    for item in matching:
        expected = parameters.model_validate(item["arguments"]).model_dump()
        if expected == arguments:
            if item["usable"]:
                return
            raise AssistantError(409, "ASSISTANT_CONFIRMATION_UNAVAILABLE",
                "The original send confirmation was already used or expired. Do not ask again in this "
                "card continuation or claim a new action. Report the existing action result, or that "
                "the confirmation expired.")
    raise AssistantError(409, "ASSISTANT_CONFIRMATION_INPUT_CHANGED",
        "This continuation is bound to the exact send the user already confirmed. No new card was created. "
        "Call the original tool with the original arguments below unchanged; the card's display labels "
        "and attachment footer are not message text. Do not change the target, files or source IDs. "
        + json.dumps(confirmed, ensure_ascii=False))


def input_digest(task_id: str, text: str, attachments=()) -> str:
    return command_digest({"task_id": task_id, "text": text, "attachments": sorted(attachments or ())})


def card_text(prompt: str, impact: str | None) -> str:
    """What will be done, then its impact on the second line (the same words a call reads aloud)."""
    return prompt if not impact else f"{prompt}\n影响：{impact}"


async def consume_card(ctx, *, digest: str, kind: str = KIND) -> str | None:
    """Consume an existing exact, fresh receipt without creating another card."""
    if kind not in KINDS:
        raise ValueError("Unknown confirmation card")
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
    return None


async def require_card(ctx, *, digest: str, prompt: str, header: str, description: str,
                       confirm: str | None = None, target: dict | None = None, kind: str = KIND,
                       action: str | None = None, impact: str | None = None,
                       high_risk: bool = True) -> str | None:
    """Consume an exact fresh confirmation, or suspend to ask for one."""
    if consumed := await consume_card(ctx, digest=digest, kind=kind):
        return consumed
    confirm = confirm or (CONFIRM if kind == KIND else CONFIRM_ACTION)
    from question import runtime
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


async def consume_send_confirmation(ctx, *, task_id: str, text: str, attachments=()) -> str | None:
    """Validate the destination and honor old receipts; ordinary sends need no card."""
    async with get_db_session() as db:
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        await task_locked(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                          main_id=ctx.session_id, task_id=task_id)
    return await consume_card(ctx, digest=input_digest(task_id, text, attachments))


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


async def pending_cards(user_id: str, workspace_id: str, main_id: str, *, message_id: str | None = None) -> list[dict]:
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
            QuestionCheckpoint.status == "pending",
            (QuestionCheckpoint.generation == SessionExecution.generation) | QuestionCheckpoint.detached.is_(True))
            .order_by(QuestionCheckpoint.created_at, QuestionCheckpoint.id))).all())
    if message_id is not None:
        rows = [row for row in rows if row.message_id == message_id]
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
        elif continuation.get("kind") == "question" and continuation.get("assistant_placement"):
            kind, card, action, high_risk = "assistant_placement", continuation["assistant_placement"], "task_placement", False
        elif continuation.get("kind") == "question" and continuation.get("assistant_task_setup"):
            kind, card, action, high_risk = "assistant_task_setup", continuation["assistant_task_setup"], "task_setup", False
        else:
            continue
        question = row.questions[0]
        placement_fields = {}
        if kind == "assistant_placement":
            from assistant.placement import new_project_option
            placement_fields["new_project_option"] = new_project_option(card)
        from assistant.requests import question_version
        cards.append({"card_id": row.id, "kind": kind, "action": action, "question_version": question_version(row),
                      **({"task_title": card["input"].get("title", ""),
                          "task_instructions": card["input"].get("instructions", "")}
                         if kind in {"assistant_task_setup", "assistant_placement"} else {}),
                      "header": question.get("header", ""), "prompt": question.get("question", ""),
                      "impact": card.get("impact"),
                      "options": [option.get("label") for option in question.get("options") or []],
                      "option_descriptions": [option.get("description", "") for option in question.get("options") or []],
                      "generation": row.generation,
                      "created_at": runtime.utc(row.created_at).isoformat(),
                      "expires_at": runtime.utc(row.expires_at).isoformat() if row.expires_at else None,
                      "high_risk": high_risk, **placement_fields})
    return cards


async def card_status(user_id: str, workspace_id: str, main_id: str, card_id: str) -> str:
    """Explain a missing card without implying its action succeeded or revealing another owner's card."""
    from question import runtime
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        row = await db.scalar(select(QuestionCheckpoint).where(
            QuestionCheckpoint.id == card_id, QuestionCheckpoint.session_id == main_id,
            QuestionCheckpoint.user_id == user_id))
        if row is None:
            return "unavailable"
        if row.status == "answered":
            return "already_answered"
        if row.status != "pending":
            return row.status
        if row.expires_at and runtime.utc(row.expires_at) <= runtime.now():
            return "expired"
        execution = await db.get(SessionExecution, main_id)
        return "superseded" if execution is None or (row.generation != execution.generation and not row.detached) else "conflict"
