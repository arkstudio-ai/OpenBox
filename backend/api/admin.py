"""Read-only administration API."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select

from audit import record
from auth.middleware import require_admin
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.internal_task import InternalTaskState
from db.models.user import User
from db.repository.workspace_repo import PgWorkspaceRepo


router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin), Depends(get_workspace)],
)
_workspace_repo = PgWorkspaceRepo()


def _row_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@router.get("/users")
async def list_users(
    request: Request,
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(require_admin),
):
    stmt = select(User).where(User.is_deleted.is_(False))
    needle = q.strip()
    if needle:
        stmt = stmt.where(
            or_(User.username.ilike(f"%{needle}%"), User.email.ilike(f"%{needle}%"))
        )
    async with get_db_session() as db:
        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        users = (
            await db.execute(stmt.order_by(User.created_at.desc()).offset(offset).limit(limit))
        ).scalars()
        items = [
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "created_at": user.created_at,
                "default_workspace_id": user.default_workspace_id,
            }
            for user in users
        ]
    await record(admin["user_id"], admin.get("workspace_id"), "admin.view_users", "user", None,
                 {"q": q, "offset": offset, "limit": limit}, request)
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("/workspaces/{workspace_id}")
async def get_workspace_admin(
    workspace_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    workspace = await _workspace_repo.get(workspace_id)
    if workspace is None:
        from fastapi import HTTPException
        raise HTTPException(404, detail="Workspace not found")
    result = {**workspace, "members": await _workspace_repo.list_members(workspace_id)}
    await record(admin["user_id"], workspace_id, "admin.view_workspace",
                 "workspace", workspace_id, None, request)
    return result


@router.get("/audit")
async def list_audit(
    request: Request,
    workspace_id: str | None = None,
    action: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(require_admin),
):
    stmt = select(AuditLog)
    if workspace_id:
        stmt = stmt.where(AuditLog.workspace_id == workspace_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    async with get_db_session() as db:
        rows = (
            await db.execute(
                stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
            )
        ).scalars()
        items = [_row_dict(row) for row in rows]
    await record(admin["user_id"], workspace_id or admin.get("workspace_id"), "admin.view_audit",
                 "audit_log", None, {"action": action}, request)
    return {"items": items, "offset": offset, "limit": limit}


@router.get("/internal-tasks")
async def list_internal_tasks(
    request: Request,
    admin: dict = Depends(require_admin),
):
    async with get_db_session() as db:
        rows = (
            await db.execute(select(InternalTaskState).order_by(InternalTaskState.name))
        ).scalars()
        items = [_row_dict(row) for row in rows]
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.view_internal_tasks",
        "internal_task_state", None, None, request,
    )
    return {"items": items}
