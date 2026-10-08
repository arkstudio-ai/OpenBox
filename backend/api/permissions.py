"""Permission routes."""
from fastapi import APIRouter, Depends, HTTPException
from auth.middleware import get_current_user
from pydantic import Field
from api.questions import QuestionReplyBinding
from assistant.policy import AssistantError

from permission import permission as perm_mod

router = APIRouter(dependencies=[Depends(get_current_user)])


class PermissionReplyBody(QuestionReplyBinding):
    action: str  # "once", "always", "reject"
    message: str | None = Field(default=None, max_length=4000)


@router.get("/permission")
async def list_permissions(current_user: dict = Depends(get_current_user)):
    """List all pending permission requests."""
    user_id = current_user["user_id"]
    from assistant.permission_requests import legacy_pending_for_user, pending_for_user
    durable = await pending_for_user(user_id)
    return await legacy_pending_for_user(user_id) + durable


@router.get("/permission/{request_id}")
async def get_permission(request_id: str, current_user: dict = Depends(get_current_user)):
    from assistant.permission_requests import event_for, fresh, scope_for
    from db.base import get_db_session
    try:
        async with get_db_session() as db:
            event = await event_for(db, request_id, current_user["user_id"])
            if event is None:
                raise HTTPException(404, "Permission request not found")
            task, _ = await scope_for(db, event)
            return await fresh(db, event, task)
    except AssistantError as error:
        raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from error


@router.post("/permission/{request_id}")
async def reply_permission(
    request_id: str,
    body: PermissionReplyBody,
    current_user: dict = Depends(get_current_user),
):
    """Reply to a permission request."""
    if body.action not in ("once", "always", "reject"):
        raise HTTPException(400, "Invalid action. Must be 'once', 'always', or 'reject'.")

    user_id = current_user["user_id"]
    try:
        receipt = await perm_mod.reply(request_id, body.action, body.message, user_id=user_id, **body.binding())
    except KeyError:
        raise HTTPException(404, "Permission request not found")
    except PermissionError:
        raise HTTPException(403, "Permission request does not belong to current user")
    except AssistantError as error:
        raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from error
    except ValueError as error:
        raise HTTPException(400, "Invalid permission reply") from error
    return receipt or {"ok": True}
