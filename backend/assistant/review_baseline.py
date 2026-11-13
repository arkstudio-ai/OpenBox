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


def is_review(card, data) -> bool:
    """Whether a displayed card proposed something for approval (``purpose=review``).

    New cards carry it in ``detail.purpose``. Older ones only in the tool call's raw input, which is what
    the model sent, not what the tool validated: gw2 2026-11-13, a ``questions`` array sent as a JSON string
    was repaired for execution but stored as sent, and every later followup to that conversation died on
    ``'str' object has no attribute 'get'``.
    """
    for item in card.questions or []:
        if isinstance(item, dict) and ((item.get("detail") or {}).get("purpose") == "review"):
            return True
    raw = data.get("input") if isinstance(data, dict) else None
    if isinstance(raw, str):
        raw = _decoded(raw)
    questions = raw.get("questions") if isinstance(raw, dict) else None
    if isinstance(questions, str):
        questions = _decoded(questions)
    return any(isinstance(q, dict) and q.get("purpose") == "review" and q.get("review_content")
               for q in (questions if isinstance(questions, list) else []))


def _decoded(text: str):
    import json
    try:
        return json.loads(text)
    except ValueError:
        return None


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
        if is_review(card, data):
            # A settled proposal is historical context, not the baseline of
            # every subsequent operation (e.g. publishing a completed video).
            # Stop at the newest review instead of falling back to an older
            # unanswered card. Explicit task revisions bind their own card.
            if card.status not in {"pending", "superseded"}:
                return ref
            return {**ref, "review_baseline": {"request_id": card.id, "content_hash": digest(card)}}
    return ref
