"""Personal assistant creation. Reading the fixed entry never creates resources."""
from datetime import datetime, timezone

from sqlalchemy import select

from assistant.policy import main_session_locked, require_membership
from core.identifier import generate_id
from db.base import get_db_session
from db.models.project import Project
from db.models.workspace import Workspace
from project.workspace import DEFAULT_NAME, DEFAULT_SLUG
from session.internal_parts import begin_session_write
from session.session import _new_session_record, _orm_to_session, _publish_session_created


async def get_main_session(*, user_id: str, workspace_id: str):
    async with get_db_session() as db:
        await require_membership(db, user_id, workspace_id)
        row = await main_session_locked(db, user_id, workspace_id)
        return _orm_to_session(row) if row else None


async def ensure_main_session(*, user_id: str, workspace_id: str, model: str = "", variant: str | None = None):
    """Serialize bootstrap in SQL, including a missing default container project.

    SQLite uses its existing short write-transaction primitive; PostgreSQL
    locks the workspace. The partial unique index is the final cross-process
    guard. No sandbox, provider, task, or Inbox is started by this operation.
    """
    created = False
    async with get_db_session() as db:
        await begin_session_write(db)
        await require_membership(db, user_id, workspace_id)
        await db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
        await require_membership(db, user_id, workspace_id)
        row = await main_session_locked(db, user_id, workspace_id)
        if row is not None:
            return _orm_to_session(row)
        project = await db.scalar(select(Project).where(
            Project.workspace_id == workspace_id, Project.slug == DEFAULT_SLUG,
            Project.is_deleted.is_(False),
        ))
        now = datetime.now(timezone.utc)
        if project is None:
            project = Project(id=generate_id(), user_id=user_id, workspace_id=workspace_id,
                              name=DEFAULT_NAME, slug=DEFAULT_SLUG, created_at=now, updated_at=now)
            db.add(project)
            await db.flush()
        row, session = _new_session_record(
            model=model, variant=variant, agent="assistant", title="", parent_id=None,
            user_id=user_id, workspace_id=workspace_id, project_id=project.id,
            kind="assistant", now=now,
        )
        db.add(row)
        await db.flush()
        created = True
    if created:
        _publish_session_created(session)
    return session
