"""Workspace membership and invitation API."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from audit import record
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.identifier import generate_id
from db.base import get_db_session
from db.models.user import User
from db.models.workspace import WorkspaceInvitation, WorkspaceMember
from db.repository.user_repo import PgUserRepo
from db.repository.workspace_repo import PgWorkspaceRepo


router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])
_repo = PgWorkspaceRepo()
_user_repo = PgUserRepo()


class InviteBody(BaseModel):
    target: str
    role: str = "member"

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        value = value.strip().lower()
        if not value or len(value) > 255:
            raise ValueError("A username or email is required")
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in {"admin", "member"}:
            raise ValueError("Role must be admin or member")
        return value


class ChangeRoleBody(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in {"admin", "member"}:
            raise ValueError("Role must be admin or member")
        return value


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
        headers={"X-Error-Code": code},
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _assert_selected(workspace_id: str, workspace: dict) -> None:
    if workspace_id != workspace["id"]:
        raise _error(403, "WORKSPACE_FORBIDDEN", "Select this workspace first")


def _assert_manager(workspace: dict) -> None:
    if workspace["role"] not in {"owner", "admin"}:
        raise _error(403, "WORKSPACE_ROLE_REQUIRED", "Owner or admin role required")


@router.get("")
async def list_workspaces(current_user: dict = Depends(get_current_user)):
    user = await _user_repo.get(current_user["user_id"])
    return {
        "items": await _repo.list_for_user(current_user["user_id"]),
        "default_workspace_id": (user or {}).get("default_workspace_id"),
    }


@router.get("/current")
async def current_workspace(
    workspace: dict = Depends(get_workspace),
):
    item = await _repo.get(workspace["id"])
    if not item:
        raise _error(404, "WORKSPACE_NOT_FOUND", "Workspace not found")
    return {
        **item,
        "role": workspace["role"],
        "members": await _repo.list_members(workspace["id"]),
        "invitations": await _repo.list_invitations(workspace["id"]),
    }


@router.get("/invitations/pending")
async def pending_invitations(current_user: dict = Depends(get_current_user)):
    return {"items": await _repo.list_pending_for_user(current_user["user_id"])}


@router.post("/{workspace_id}/invitations", status_code=201)
async def invite_member(
    workspace_id: str,
    body: InviteBody,
    request: Request,
    current_user: dict = Depends(get_current_user),
    workspace: dict = Depends(get_workspace),
):
    _assert_selected(workspace_id, workspace)
    _assert_manager(workspace)
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    invitation = WorkspaceInvitation(
        id=generate_id(),
        workspace_id=workspace_id,
        target=body.target,
        role=body.role,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=now + timedelta(days=7),
        created_by=current_user["user_id"],
        created_at=now,
    )
    async with get_db_session() as db:
        db.add(invitation)
    await record(
        current_user["user_id"],
        workspace_id,
        "workspace.invite",
        "workspace_invitation",
        invitation.id,
        {"target": body.target, "role": body.role},
        request,
    )
    return {
        "id": invitation.id,
        "workspace_id": workspace_id,
        "target": body.target,
        "role": body.role,
        "token": token,
        "expires_at": invitation.expires_at,
    }


@router.post("/invitations/{token}/accept")
async def accept_invitation(
    token: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        invitation = (
            await db.execute(
                select(WorkspaceInvitation).where(
                    WorkspaceInvitation.token_hash == token_hash
                )
            )
        ).scalar_one_or_none()
        if invitation is None:
            raise _error(404, "INVITATION_NOT_FOUND", "Invitation not found")
        if invitation.accepted_at is not None:
            raise _error(409, "INVITATION_ALREADY_ACCEPTED", "Invitation was already accepted")
        if _as_utc(invitation.expires_at) <= now:
            raise _error(410, "INVITATION_EXPIRED", "Invitation has expired")
        user = await db.get(User, current_user["user_id"])
        targets = {str(user.username).lower()}
        if user.email:
            targets.add(str(user.email).lower())
        if invitation.target.lower() not in targets:
            raise _error(403, "INVITATION_TARGET_MISMATCH", "Invitation belongs to another user")

        member = await db.get(
            WorkspaceMember,
            (invitation.workspace_id, current_user["user_id"]),
        )
        if member is None:
            member = WorkspaceMember(
                workspace_id=invitation.workspace_id,
                user_id=current_user["user_id"],
                role=invitation.role,
                status="active",
                invited_by=invitation.created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(member)
        else:
            member.role = invitation.role
            member.status = "active"
            member.invited_by = invitation.created_by
            member.updated_at = now
        invitation.accepted_by = current_user["user_id"]
        invitation.accepted_at = now
        workspace_id = invitation.workspace_id
        invitation_id = invitation.id

    await record(
        current_user["user_id"],
        workspace_id,
        "workspace.accept",
        "workspace_invitation",
        invitation_id,
        None,
        request,
    )
    return {"ok": True, "workspace_id": workspace_id}


@router.patch("/{workspace_id}/members/{user_id}")
async def change_member_role(
    workspace_id: str,
    user_id: str,
    body: ChangeRoleBody,
    request: Request,
    current_user: dict = Depends(get_current_user),
    workspace: dict = Depends(get_workspace),
):
    _assert_selected(workspace_id, workspace)
    _assert_manager(workspace)
    async with get_db_session() as db:
        member = await db.get(WorkspaceMember, (workspace_id, user_id))
        if member is None or member.status != "active":
            raise _error(404, "WORKSPACE_MEMBER_NOT_FOUND", "Member not found")
        if member.role == "owner":
            raise _error(409, "WORKSPACE_OWNER_IMMUTABLE", "Owner role cannot be changed")
        member.role = body.role
        member.updated_at = datetime.now(timezone.utc)
    await record(
        current_user["user_id"], workspace_id, "workspace.change_role",
        "workspace_member", user_id, {"role": body.role}, request,
    )
    return {"ok": True, "user_id": user_id, "role": body.role}


@router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    workspace_id: str,
    user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    workspace: dict = Depends(get_workspace),
):
    _assert_selected(workspace_id, workspace)
    _assert_manager(workspace)
    async with get_db_session() as db:
        member = await db.get(WorkspaceMember, (workspace_id, user_id))
        if member is None or member.status != "active":
            raise _error(404, "WORKSPACE_MEMBER_NOT_FOUND", "Member not found")
        if member.role == "owner":
            raise _error(409, "WORKSPACE_OWNER_IMMUTABLE", "Owner cannot be removed")
        member.status = "removed"
        member.updated_at = datetime.now(timezone.utc)
    await record(
        current_user["user_id"], workspace_id, "workspace.remove_member",
        "workspace_member", user_id, None, request,
    )
    return {"ok": True}
