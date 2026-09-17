"""Real desktop ownership fixtures, including PostgreSQL foreign keys."""
from datetime import datetime, timezone

from db.base import get_db_session
from db.models.user import User
from db.models.workspace import Workspace


async def desktop_workspace(workspace_id, *, owner_id=None):
    owner_id = owner_id or workspace_id
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        if await db.get(User, owner_id) is None:
            db.add(User(id=owner_id, username=owner_id, created_at=now, updated_at=now))
            await db.flush()
        if await db.get(Workspace, workspace_id) is None:
            db.add(Workspace(id=workspace_id, name=workspace_id, owner_user_id=owner_id,
                             created_at=now, updated_at=now))
    return workspace_id
