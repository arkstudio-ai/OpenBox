"""Which card a task's conversation is waiting on, said so the user knows where to act.

A pending card owns the conversation's next turn (question.runtime
admission_blocked_locked): delegated input waits queued behind it and runs
once the card is handled. Tasks, the watch list, delegation receipts and calls
show that card instead of reporting the task as working.
"""
from sqlalchemy import select

from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session

QUESTION_CHARS = 300


def link(session_id: str) -> str:
    return f"/app/s/{session_id}"


def card_kind(row: QuestionCheckpoint) -> str:
    """desktop_takeover, user_action, …, a plan review, or an ordinary question."""
    for question in row.questions or []:
        kind = (question.get("detail") or {}).get("kind")
        if kind:
            return str(kind)
    return str((row.continuation or {}).get("kind") or "question")


def view(row: QuestionCheckpoint, session: Session) -> dict:
    question = (row.questions or [{}])[0]
    detail = question.get("detail") or {}
    text = " ".join(str(question.get("question") or "").split())
    value = {"request_id": row.id, "kind": card_kind(row), "host": str(detail.get("host") or ""),
             "title": session.title or "", "link": link(session.id),
             "question": text[:QUESTION_CHARS] + ("…" if len(text) > QUESTION_CHARS else "")}
    if detail.get("browser"):
        # local: the page is on the cloud desktop; extension: in the user's own browser.
        value["browser"] = str(detail["browser"])
    if value["kind"] == "desktop_takeover":
        from tool.desktop_takeover import ANSWER_DONE
        value["done_option"] = ANSWER_DONE
    return value


async def blocked_views(db, sessions) -> dict[str, dict]:
    """The oldest pending card of each conversation's current turn, by session id (one read)."""
    by_id = {session.id: session for session in sessions}
    if not by_id:
        return {}
    rows = (await db.scalars(select(QuestionCheckpoint).join(
        SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id).where(
        QuestionCheckpoint.session_id.in_(list(by_id)), QuestionCheckpoint.status == "pending",
        QuestionCheckpoint.generation == SessionExecution.generation,
    ).order_by(QuestionCheckpoint.created_at, QuestionCheckpoint.id))).all()
    views: dict[str, dict] = {}
    for row in rows:
        session = by_id[row.session_id]
        if row.user_id == session.user_id and row.session_id not in views:
            views[row.session_id] = view(row, session)
    return views


async def blocked_by_locked(db, session) -> dict | None:
    """The card this conversation waits on, or None."""
    return (await blocked_views(db, [session])).get(session.id)
