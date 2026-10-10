"""Request-scoped dependencies for ``/v1``: identity, scopes, rate limit, ownership."""
from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import select

from api.v1.errors import ApiError
from api.v1.ids import internal_id
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.session import Session as SessionRow


async def api_identity(
    request: Request,
    current_user: dict = Depends(get_current_user),
    _workspace: dict = Depends(get_workspace),
) -> dict:
    """The caller, with its workspace resolved and its request counted.

    API keys are rate limited per key; a JWT (debugging from the web app)
    is not, because the web app has its own limits.
    """
    if current_user.get("auth_kind") == "api_key":
        from api.v1 import ratelimit
        from cache import get_cache
        from core.config import get_config

        spec = current_user.get("rate_limit") or get_config().rate_limit_api
        info = await ratelimit.check(get_cache(), current_user["api_key_id"], spec)
        if info is not None:
            request.state.rate_limit = info
    return current_user


def require_scope(scope: str):
    """A dependency that refuses API keys lacking ``scope``; JWTs pass."""

    async def dependency(identity: dict = Depends(api_identity)) -> dict:
        if identity.get("auth_kind") == "api_key" and scope not in (identity.get("scopes") or []):
            raise ApiError(
                403, "SCOPE_REQUIRED", f"This API key lacks the {scope} scope",
                details={"scope": scope},
            )
        return identity

    return dependency


async def load_session_row(public_session_id: str, identity: dict, *, write: bool) -> SessionRow:
    """The session named in the path, or 404 when it is not this workspace's.

    Reads are open to every workspace member; writes need the owner, the
    same rule the internal routes apply.
    """
    session_id = internal_id(public_session_id, "session")
    async with get_db_session() as db:
        row = await db.scalar(select(SessionRow).where(
            SessionRow.id == session_id,
            SessionRow.workspace_id == identity["workspace_id"],
            SessionRow.is_deleted.is_(False),
        ))
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Session not found")
    if write and row.user_id != identity["user_id"]:
        raise ApiError(403, "SESSION_READ_ONLY", "Only the session owner can change this session")
    return row
