"""Authenticated personal-memory fixtures with real workspace membership."""
from datetime import datetime, timezone

from sqlalchemy import update
from db.base import get_db_session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from db.models.project import Project


async def create_memory_user(user_id: str, username: str, project_ids=()) -> str:
    workspace_id = 'ws_' + user_id
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=username, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=workspace_id, name=workspace_id, owner_user_id=user_id, created_at=now, updated_at=now))
        await db.flush()
        db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role='owner', status='active', created_at=now, updated_at=now))
        await db.execute(update(User).where(User.id == user_id).values(default_workspace_id=workspace_id))
        db.add_all([Project(id=pid, user_id=user_id, workspace_id=workspace_id, name=pid, slug=pid, created_at=now, updated_at=now)
                    for pid in project_ids])
    return workspace_id
