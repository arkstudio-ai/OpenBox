"""One personal-memory authorization policy for tools, HTTP and workers.

Workspace membership never grants access to another user's personal memory.
Project-less retrieval means personal workspace background only. A management
view may explicitly request all the actor's current, owned projects.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import and_, or_, select

from db.models.project import Project
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember

POLICY_VERSION = "personal-v1"
# Facts about the person themselves (profile, preferences) carry this key
# prefix; project decisions and constraints use "project.".
PERSONAL_FACT_PREFIX = "personal."


class MemoryAccessDenied(ValueError):
    pass


def is_personal_fact(fact_key) -> bool:
    """A fact about the person, the same in every project they work in."""
    return isinstance(fact_key, str) and fact_key.startswith(PERSONAL_FACT_PREFIX)


@dataclass(frozen=True)
class MemoryAccessScope:
    actor_user_id: str
    workspace_id: str
    project_id: str | None = None
    include_all_projects: bool = False
    project_ids: tuple[str, ...] = ()
    acl_epoch: int = 1

    @property
    def user_id(self) -> str:
        return self.actor_user_id

    def predicates(self, model, *, project: bool = True, personal_visibility: bool = True) -> tuple:
        clauses = [model.user_id == self.actor_user_id, model.workspace_id == self.workspace_id]
        if personal_visibility and hasattr(model, "visibility"):
            clauses.append(model.visibility == "PERSONAL")
        if project and hasattr(model, "project_id"):
            if self.include_all_projects:
                clauses.append(or_(model.project_id.is_(None), model.project_id.in_(self.project_ids)))
            elif self.project_id:
                clauses.append(or_(model.project_id.is_(None), model.project_id == self.project_id))
            else:
                clauses.append(model.project_id.is_(None))
        return tuple(clauses)

    def personal(self) -> "MemoryAccessScope":
        """The same actor's personal (project-less) scope, for storing a personal fact.

        Keeps the resolved project set, so evidence in those projects is
        recognized as the actor's own without another lookup.
        """
        return MemoryAccessScope(self.actor_user_id, self.workspace_id, None, False, self.project_ids, self.acl_epoch)

    def covers_project(self, project_id: str | None) -> bool:
        """Whether this scope reads records stored in ``project_id`` (as ``predicates`` does)."""
        if project_id is None:
            return True
        return project_id in self.project_ids if self.include_all_projects else project_id == self.project_id


async def resolve_access_scope(db, *, user_id: str, workspace_id: str | None = None,
                               project_id: str | None = None,
                               include_all_projects: bool = False,
                               reading_session_id: str | None = None) -> MemoryAccessScope:
    scope, _ = await read_access_snapshot(db, user_id=user_id, workspace_id=workspace_id,
        project_id=project_id, include_all_projects=include_all_projects,
        reading_session_id=reading_session_id)
    return scope


async def read_access_snapshot(db, *, user_id: str, workspace_id: str | None = None,
                               project_id: str | None = None,
                               include_all_projects: bool = False,
                               reading_session_id: str | None = None, metadata=()):
    """Read authority and optional scalar metadata in the same live statement.

    Metadata is supplied by server adapters, never by a caller's remembered
    scope. No row is returned unless all current authority predicates pass.
    """
    # Identity and membership must describe the same live snapshot. Joining
    # them also avoids an extra SQL round trip in each post-provider recheck.
    target_workspace = workspace_id or User.default_workspace_id
    statement = select(WorkspaceMember)
    if reading_session_id:
        from db.models.session import Session
        # Fresh SQL on every provider boundary; an identity-map Session from
        # an earlier read must not conceal deletion or isolation changes.
        statement = statement.where(select(Session.id).where(
            Session.id == reading_session_id, Session.user_id == user_id,
            Session.workspace_id == WorkspaceMember.workspace_id,
            Session.is_deleted.is_(False),
            or_(Session.kind == "assistant", and_(Session.kind != "assistant",
                Session.memory_policy == "standard", Session.project_id == project_id)),
        ).exists())
    statement = (statement
        .join(User, User.id == WorkspaceMember.user_id)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id).where(
        User.id == user_id, User.is_active.is_(True), User.is_deleted.is_(False),
        WorkspaceMember.user_id == user_id, WorkspaceMember.workspace_id == target_workspace,
        WorkspaceMember.status == "active", Workspace.is_deleted.is_(False),
    ))
    projects = ()
    if project_id or include_all_projects:
        project_match = [Project.workspace_id == WorkspaceMember.workspace_id,
                         Project.user_id == user_id, Project.is_deleted.is_(False)]
        if project_id:
            project_match.append(Project.id == project_id)
        # Keep the membership even when no project matches, so an unavailable
        # project cannot be mistaken for a personal-scope read.
        statement = statement.add_columns(Project.id).outerjoin(Project, and_(*project_match)).order_by(Project.id)
    rows = (await db.execute(statement.add_columns(*metadata) if metadata else statement)).all()
    member = rows[0][0] if rows else None
    if project_id or include_all_projects:
        projects = tuple(row[1] for row in rows if row[1] is not None)
    if not member:
        raise MemoryAccessDenied("Memory identity and active workspace membership are required")
    workspace_id = member.workspace_id
    if project_id and project_id not in projects:
        raise MemoryAccessDenied("Memory project is not available in the authorized scope")
    stamp = member.updated_at
    if stamp and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    epoch_key = f"{workspace_id}:{user_id}:{member.role}:{stamp.isoformat() if stamp else ''}"
    epoch = max(1, int.from_bytes(sha256(epoch_key.encode()).digest()[:4], "big") & 0x7FFFFFFF)
    scope = MemoryAccessScope(user_id, workspace_id, project_id, include_all_projects, projects, epoch)
    return scope, tuple(rows[0][2 if project_id or include_all_projects else 1:])


def active_memory_predicates(now: datetime | None = None) -> tuple:
    from db.models.memory import UserMemory
    from db.models.memory_v2 import MemoryTombstone
    now = now or datetime.now(timezone.utc)
    return (
        UserMemory.status == "ACTIVE", UserMemory.confirmation_status == "CONFIRMED",
        UserMemory.deleted_at.is_(None), UserMemory.type != "PENDING_NOTE",
        or_(UserMemory.ttl.is_(None), UserMemory.ttl > now),
        or_(UserMemory.valid_from.is_(None), UserMemory.valid_from <= now),
        or_(UserMemory.valid_to.is_(None), UserMemory.valid_to > now),
        ~select(MemoryTombstone.id).where(MemoryTombstone.object_kind == "memory", MemoryTombstone.object_id == UserMemory.id).exists(),
    )
