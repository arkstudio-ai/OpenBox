"""Reauthorize queued realtime payloads at egress, after any queueing delay."""
from sqlalchemy import select

from db.base import get_db_session
from db.models.session import Session
from session.policy import readable_session


async def public_event(user_id: str, event: dict) -> dict | None:
    data = event.get("data") or {}
    session_id = data.get("sessionId") or data.get("session_id")
    if not session_id:
        return event
    async with get_db_session() as db:
        session = await db.scalar(select(Session).where(Session.id == session_id,
            readable_session(user_id, Session.workspace_id)))
        if session is None:
            return None
        if session.kind != "assistant":
            return event
        identity = {"sessionId": session.id}
        if type(data.get("generation")) is int:
            identity["generation"] = data["generation"]
        kind = event.get("type")
        if kind in {"session.status", "session.finalizing"}:
            return {"type": kind, "data": {**identity, **{key: data[key] for key in
                ("status", "attempt", "maxAttempts") if key in data}}}
        if kind == "session.error":
            return {"type": kind, "data": {**identity, "error": {"code": "ASSISTANT_RUN_FAILED"}}}
        # Main transcript bytes come only from the current-source SQL view.
        # Replaying an old queued delta would bypass source invalidation, and
        # a live uncommitted answer has no final evidence manifest yet.
        if kind in {"message.created", "message.updated", "tool.completed", "tool.error",
                    "session.updated", "session.compaction.complete", "assistant.history.changed"}:
            return {"type": "assistant.history.changed", "data": identity}
        return None
