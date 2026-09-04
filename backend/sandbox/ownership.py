"""Resolve the workspace that owns a sandbox or cloud desktop."""

from db.repository.user_repo import PgUserRepo


_user_repo = PgUserRepo()


async def owner_for(user_id: str) -> str:
    user = await _user_repo.get(user_id)
    workspace_id = str((user or {}).get("default_workspace_id") or "")
    if not workspace_id:
        from sandbox.wuying_desktop_service import DesktopNotReady

        raise DesktopNotReady({"state": "no_workspace"})
    return workspace_id


async def owner_for_request(current_user: dict | str) -> str:
    """Use the request-selected workspace, falling back to the user's default."""
    if isinstance(current_user, str):
        # Backwards-compatible internal call shape. HTTP requests always pass
        # the identity dict enriched by get_workspace().
        return current_user
    workspace_id = str(current_user.get("workspace_id") or "")
    if workspace_id:
        return workspace_id
    return await owner_for(current_user["user_id"])


async def owner_for_session(session_id: str, user_id: str) -> str:
    """Resolve persisted session ownership, with the default workspace fallback."""
    from db.base import get_db_session
    from db.models.session import Session
    from sqlalchemy import select

    # Internal routing already holds a session id.  The row's workspace is the
    # execution owner even when the actor is not the user who created it (and
    # even immediately after the row was soft-deleted during cleanup).
    async with get_db_session() as db:
        workspace_id = (
            await db.execute(
                select(Session.workspace_id).where(Session.id == session_id)
            )
        ).scalar_one_or_none()
    if workspace_id:
        return str(workspace_id)
    return await owner_for(user_id)
