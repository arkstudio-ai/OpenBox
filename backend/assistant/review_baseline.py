"""Bind a followup to the proposal actually displayed in its own conversation.

The binding is captured under the input admission lock, not chosen later by a
model or by a search over similarly named tasks. It conveys no approval.
"""
from sqlalchemy import select

from db.models.part import Part
from db.models.question import QuestionCheckpoint


def digest(card):
    from assistant.commands import command_digest
    return command_digest({"id": card.id, "session": card.session_id,
        "user": card.user_id, "part": card.part_id, "questions": card.questions})


async def bind_locked(db, session, origin, ref):
    if (session.kind != "normal" or origin not in {"human", "assistant_delegation"}
            or ref.get("revision_request")):
        return ref
    # Only persisted, actually displayed review cards qualify. A failed tool
    # attempt or a clarification is never silently promoted to a proposal.
    rows = (await db.execute(select(QuestionCheckpoint, Part).join(Part,
        Part.id == QuestionCheckpoint.part_id).where(
        QuestionCheckpoint.session_id == session.id, QuestionCheckpoint.user_id == session.user_id,
        Part.session_id == session.id, Part.user_id == session.user_id,
        Part.message_id == QuestionCheckpoint.message_id, Part.type == "tool",
        QuestionCheckpoint.detached.is_(False),
    ).order_by(QuestionCheckpoint.created_at.desc(), QuestionCheckpoint.id.desc()).limit(50))).all()
    for card, part in rows:
        data = part.data or {}
        if ((part.canonical_tool_id or data.get("tool")) != "question" or data.get("ignored")):
            continue
        questions = (data.get("input") or {}).get("questions", [])
        if any(q.get("purpose") == "review" and q.get("review_content") for q in questions):
            return {**ref, "review_baseline": {"request_id": card.id, "content_hash": digest(card)}}
    return ref
