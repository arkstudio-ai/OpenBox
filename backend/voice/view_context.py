"""A visible conversation is a reference hint, never task/answer authority."""
import re

from sqlalchemy import select

from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from memory.readers import authority


def session_hint(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else None


async def read(db, turn, session_id, *, include_history=False):
    if not session_hint(session_id) or turn.read_policy != "allowed":
        return None
    access = await authority(db, turn)
    row = await db.scalar(select(Session).where(Session.id == session_id,
        *access.predicates(Session, personal_visibility=False), Session.is_deleted.is_(False),
        Session.kind == "normal", Session.parent_id.is_(None), Session.memory_policy == "standard"))
    if row is None:
        return None
    pending = list((await db.scalars(select(QuestionCheckpoint.id).join(SessionExecution,
        SessionExecution.session_id == QuestionCheckpoint.session_id).where(
        QuestionCheckpoint.session_id == row.id, QuestionCheckpoint.user_id == turn.user_id,
        QuestionCheckpoint.status == "pending", QuestionCheckpoint.generation == SessionExecution.generation)
        .order_by(QuestionCheckpoint.created_at.desc()).limit(2))).all())
    value = {"session_id": row.id, "title": (row.title or "")[:200], "pending_request_ids": pending,
        "kind": "conversation", "history_status": "not_read",
        "instruction": "This is an ordinary visible conversation, not necessarily a task. Its title is not evidence of its content. "
            "Only the supplied history can support a recap; never infer pending work from a title. "
            "No pending_request_ids means no verified pending decision, not a reason to invent a confirmation. "
            "UI navigation is not execution consent. For a pending decision, read cards_pending by request_id first. "
            "For changed requirements use assistant_ask(mode=revise) with that verified question_id; do not re-ask the old question. "
            "If the task itself is ambiguous, clarify which task once."}
    if include_history:
        from memory.readers import history_window
        from memory.policy import MemoryAccessDenied
        try:
            window = await history_window(db, access, turn, row.id, limit=4,
                max_chars=1800, visible_only=True)
        except MemoryAccessDenied:
            return None
        value["history"] = {"version": window["version"], "next_offset": window["next_offset"],
            "entries": [{key: entry[key] for key in ("role", "text", "source_ref", "omitted") if key in entry}
                        for entry in window["entries"]]}
        value["history_status"] = "partial" if window["next_offset"] or any(
            entry.get("omitted") for entry in window["entries"]) else "complete"
    return value


def history_needed(packet):
    # The shared semantic decision selects this local read. No phrase matching
    # and no extra model request; a navigation hint never authorizes history.
    return packet.get("route", {}).get("needs", {}).get("history_useful", 0) >= .4


async def attach(scope, packet):
    from dataclasses import replace
    from core.config import get_config
    from db.base import get_db_session
    from memory.packet import apply_budget
    packet.pop("viewed_task", None)
    # Freeze the UI position at the start of this utterance. Navigation while
    # the answer is being prepared cannot silently retarget the human's words.
    session_id = packet.setdefault("_viewed_session_id", getattr(scope, "viewed_session_id", None))
    policy = packet.get("route", {}).get("read_policy", "uncertain")
    packet["_view_context_checked"] = True
    if session_id and policy == "allowed":
        async with get_db_session() as db:
            value = await read(db, replace(packet["_turn"], read_policy=policy), session_id,
                include_history=history_needed(packet))
        if value:
            packet["viewed_task"] = value
    apply_budget(packet, get_config().memory)
