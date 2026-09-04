"""Persistence helpers for workspaces, members, and invitations."""
from datetime import datetime, timezone

from sqlalchemy import select

from db.base import get_db_session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceInvitation, WorkspaceMember


def _workspace_dict(row: Workspace, role: str | None = None) -> dict:
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    if role is not None:
        data["role"] = role
    return data


class PgWorkspaceRepo:
    async def list_for_user(self, user_id: str) -> list[dict]:
        async with get_db_session() as db:
            rows = (
                await db.execute(
                    select(Workspace, WorkspaceMember.role)
                    .join(
                        WorkspaceMember,
                        WorkspaceMember.workspace_id == Workspace.id,
                    )
                    .where(
                        WorkspaceMember.user_id == user_id,
                        WorkspaceMember.status == "active",
                        Workspace.is_deleted.is_(False),
                    )
                    .order_by(Workspace.created_at.asc())
                )
            ).all()
            return [_workspace_dict(workspace, role) for workspace, role in rows]

    async def get(self, workspace_id: str) -> dict | None:
        async with get_db_session() as db:
            row = (
                await db.execute(
                    select(Workspace).where(
                        Workspace.id == workspace_id,
                        Workspace.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            return _workspace_dict(row) if row else None

    async def get_member(self, workspace_id: str, user_id: str) -> dict | None:
        async with get_db_session() as db:
            row = (
                await db.execute(
                    select(WorkspaceMember).where(
                        WorkspaceMember.workspace_id == workspace_id,
                        WorkspaceMember.user_id == user_id,
                        WorkspaceMember.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if not row:
                return None
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}

    async def list_members(self, workspace_id: str) -> list[dict]:
        async with get_db_session() as db:
            rows = (
                await db.execute(
                    select(WorkspaceMember, User)
                    .join(User, User.id == WorkspaceMember.user_id)
                    .where(WorkspaceMember.workspace_id == workspace_id)
                    .order_by(WorkspaceMember.created_at.asc())
                )
            ).all()
            return [
                {
                    "user_id": member.user_id,
                    "username": user.username,
                    "email": user.email,
                    "role": member.role,
                    "status": member.status,
                    "created_at": member.created_at,
                    "updated_at": member.updated_at,
                }
                for member, user in rows
            ]

    async def list_invitations(self, workspace_id: str) -> list[dict]:
        async with get_db_session() as db:
            rows = (
                await db.execute(
                    select(WorkspaceInvitation)
                    .where(WorkspaceInvitation.workspace_id == workspace_id)
                    .order_by(WorkspaceInvitation.created_at.desc())
                )
            ).scalars()
            return [
                {
                    "id": row.id,
                    "workspace_id": row.workspace_id,
                    "target": row.target,
                    "role": row.role,
                    "expires_at": row.expires_at,
                    "created_at": row.created_at,
                    "accepted_at": row.accepted_at,
                }
                for row in rows
            ]

    async def list_pending_for_user(self, user_id: str) -> list[dict]:
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            user = await db.get(User, user_id)
            if user is None:
                return []
            targets = [user.username.lower()]
            if user.email:
                targets.append(user.email.lower())
            rows = (
                await db.execute(
                    select(WorkspaceInvitation, Workspace.name)
                    .join(Workspace, Workspace.id == WorkspaceInvitation.workspace_id)
                    .where(
                        WorkspaceInvitation.target.in_(targets),
                        WorkspaceInvitation.accepted_at.is_(None),
                        WorkspaceInvitation.expires_at > now,
                        Workspace.is_deleted.is_(False),
                    )
                    .order_by(WorkspaceInvitation.created_at.desc())
                )
            ).all()
            return [
                {
                    "id": invitation.id,
                    "workspace_id": invitation.workspace_id,
                    "workspace_name": workspace_name,
                    "target": invitation.target,
                    "role": invitation.role,
                    "expires_at": invitation.expires_at,
                    "created_at": invitation.created_at,
                }
                for invitation, workspace_name in rows
            ]
