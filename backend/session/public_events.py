"""Reauthorize queued realtime payloads at egress, after any queueing delay."""
from sqlalchemy import select

from db.base import get_db_session
from db.models.session import Session
from session.policy import readable_session


async def public_event(user_id: str, event: dict) -> dict | None:
    """Drop events for sessions the reader can no longer open.

    Assistant sessions stream like ordinary chats (PERSONAL_ASSISTANT_DESIGN_V2.md
    11.2); readable_session already limits them to their owner.
    """
    data = event.get("data") or {}
    session_id = data.get("sessionId") or data.get("session_id")
    if not session_id:
        return event
    async with get_db_session() as db:
        session = await db.scalar(select(Session.id).where(Session.id == session_id,
            readable_session(user_id, Session.workspace_id)))
    return event if session is not None else None
