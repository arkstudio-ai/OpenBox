"""Session operations the assistant performs with exactly the user's rights.

Only top-level ordinary conversations the user owns in this workspace are in
scope (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 6.2). Deleting one is
sessions.delete (assistant.delete_tools): only on the user's explicit request
naming it, after they confirm its impact on a card.
"""
from sqlalchemy import select

from assistant.commands import _authority, _project, _tool_source_locked
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.session import Session
from memory.redaction import redact_credentials
from session.internal_parts import begin_session_write


async def owned_session(db, *, user_id, workspace_id, session_id):
    session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id,
        Session.workspace_id == workspace_id, Session.kind == "normal", Session.parent_id.is_(None),
        Session.is_deleted.is_(False)))
    if session is None:
        raise AssistantError(404, "ASSISTANT_SESSION_UNAVAILABLE", "Owned conversation is unavailable")
    await _project(db, session.project_id, user_id, workspace_id)
    return session


async def rename_session(*, user_id, workspace_id, main_id, session_id, title, source=None) -> dict:
    title = redact_credentials((title or "").strip())
    if not 1 <= len(title) <= 128:
        raise ValueError("A title of 1 to 128 characters is required")
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if source is not None:
            await _tool_source_locked(db, main, source, "session_rename")
        await owned_session(db, user_id=user_id, workspace_id=workspace_id, session_id=session_id)
    from session.session import set_session_title
    await set_session_title(session_id, title, user_id=user_id)
    return {"session_id": session_id, "title": title, "state": "renamed"}
