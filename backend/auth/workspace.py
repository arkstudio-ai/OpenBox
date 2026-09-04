"""Resolve and authorize the workspace selected by one HTTP request."""
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request

from auth.middleware import get_current_user
from db.repository.user_repo import PgUserRepo
from db.repository.workspace_repo import PgWorkspaceRepo


_user_repo = PgUserRepo()
_workspace_repo = PgWorkspaceRepo()


def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": code, "message": message},
        headers={"X-Error-Code": code},
    )


async def get_workspace(
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    """Return the active workspace membership for the current request."""
    cached = getattr(request.state, "workspace", None)
    if cached is not None:
        return cached

    workspace_id = request.headers.get("X-Workspace-Id", "").strip()
    if not workspace_id:
        user_row = await _user_repo.get(user["user_id"])
        workspace_id = str((user_row or {}).get("default_workspace_id") or "")
    if not workspace_id:
        raise _forbidden("WORKSPACE_FORBIDDEN", "No default workspace is available")

    member = await _workspace_repo.get_member(workspace_id, user["user_id"])
    if member is None:
        raise _forbidden("WORKSPACE_FORBIDDEN", "Workspace membership is required")
    resolved = {"id": workspace_id, "role": member["role"]}
    # FastAPI caches dependency results per request. Mutating this shared
    # identity lets existing endpoint signatures keep receiving current_user
    # while gaining the selected workspace without a second database lookup.
    user["workspace_id"] = workspace_id
    user["workspace_role"] = member["role"]
    request.state.workspace = resolved
    return resolved


def require_workspace_role(*roles: str) -> Callable:
    allowed = frozenset(roles)

    async def dependency(workspace: dict = Depends(get_workspace)) -> dict:
        if workspace["role"] not in allowed:
            raise _forbidden(
                "WORKSPACE_ROLE_REQUIRED",
                f"Workspace role must be one of: {', '.join(sorted(allowed))}",
            )
        return workspace

    return dependency
