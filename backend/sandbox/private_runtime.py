"""Current private Session authority and fixed identities on the Wuying guest.

Old private Docker rows remain denial metadata only. This facade never creates,
starts, adopts, validates, or falls back to a Docker environment.

Dormant: only the assistant's own main conversation is private for runtime
purposes (sandbox.privacy), and that conversation never runs a sandbox. Every
other Session, including work the assistant delegates, uses the shared
workspace runtime, so no execution currently reaches this actor runtime.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import re

from sqlalchemy import and_, or_, select

from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember


class PrivateRuntimeError(AssistantError):
    pass


@dataclass(frozen=True)
class PrivateSessionScope:
    session_id: str
    user_id: str
    workspace_id: str


@dataclass(frozen=True)
class PrivateRuntimeRoute:
    binding_id: str
    kind: str
    provider: str
    workspace_id: str
    actor_user_id: str
    attempt_id: str
    revision: int
    container_id: str
    name: str
    image: str
    image_id: str
    created_at: datetime
    host: str
    port: int
    route_key: str
    api_key: str = field(repr=False)
    isolation_mode: str = "guest_uid_mount"
    base_url: str = ""
    scope_id: str = ""
    desktop_id: str = ""
    region_id: str = ""
    guest_binding_id: str = ""
    guest_attempt_id: str = ""
    provider_identity: dict = field(default_factory=dict, repr=False)


def _error(code, detail, status=409):
    return PrivateRuntimeError(status, "PRIVATE_RUNTIME_" + code, detail)


async def _scope(db, session_id, user_id, workspace_id=None, *, lock=False):
    statement = select(Session).join(User, User.id == Session.user_id).join(Workspace, Workspace.id == Session.workspace_id).join(
        WorkspaceMember, and_(WorkspaceMember.workspace_id == Session.workspace_id,
                              WorkspaceMember.user_id == user_id)).where(
        Session.id == session_id, Session.user_id == user_id, Session.is_deleted.is_(False),
        User.is_active.is_(True), User.is_deleted.is_(False),
        Workspace.is_deleted.is_(False), WorkspaceMember.status == "active")
    if workspace_id is not None:
        statement = statement.where(Session.workspace_id == workspace_id)
    if lock:
        statement = statement.with_for_update(of=(Session, User, Workspace, WorkspaceMember))
    session = await db.scalar(statement)
    if session is None:
        raise _error("SCOPE_INVALID", "The current Session owner or workspace membership is unavailable", 403)
    from sandbox.privacy import runtime_private
    if not runtime_private(session):
        return None
    return PrivateSessionScope(session.id, session.user_id, session.workspace_id)


async def private_session_scope(*, session_id, user_id, workspace_id=None):
    """Resolve persisted privacy, never a caller header or a resource label."""
    async with get_db_session() as db:
        return await _scope(db, session_id, user_id, workspace_id)



def _config(user_id, kind="sandbox"):
    from core.config import get_config
    settings = get_config()
    if settings.sandbox_provider != "wuying":
        raise _error("PROVIDER_UNSUPPORTED", "Private execution uses the configured Wuying desktop only", 403)
    config = settings.private_runtime
    if not config.enabled or user_id not in config.allowed_user_ids:
        raise _error("DISABLED", "Private Wuying execution is not enabled for this actor", 403)
    if kind != "sandbox":
        # Retained browser_profile rows are never resolved or validated.
        raise _error("UNAVAILABLE", "Unsupported private runtime kind")
    return config


def _snapshot(row):
    return {column.name: deepcopy(getattr(row, column.name)) for column in PrivateRuntimeBinding.__table__.columns}


async def find_private_binding(container_id):
    """Deny-only alias recognition, including non-ready and uncertain rows.

    This does not authorize an actor. Returned metadata contains no key,
    ciphertext, object contents or host volume path.
    """
    if not isinstance(container_id, str) or not container_id or len(container_id) > 160:
        return None
    reference = container_id.lstrip("/")
    filters = [PrivateRuntimeBinding.id == reference, PrivateRuntimeBinding.route_key == reference,
               PrivateRuntimeBinding.container_name == reference, PrivateRuntimeBinding.container_id == reference]
    if re.fullmatch(r"[0-9a-f]{12,64}", reference):
        filters.append(PrivateRuntimeBinding.container_id.startswith(reference, autoescape=True))
    async with get_db_session() as db:
        row = await db.scalar(select(PrivateRuntimeBinding).where(or_(*filters)).limit(1))
        return None if row is None else {key: getattr(row, key) for key in (
            "id", "kind", "provider", "status", "workspace_id", "actor_user_id", "container_id", "container_name", "route_key")}


async def _load(scope, *, binding_id=None, kind="sandbox"):
    async with get_db_session() as db:
        current = await _scope(db, scope.session_id, scope.user_id, scope.workspace_id)
        if current != scope:
            raise _error("SCOPE_INVALID", "The Session is no longer private", 403)
        statement = select(PrivateRuntimeBinding).where(PrivateRuntimeBinding.workspace_id == scope.workspace_id,
            PrivateRuntimeBinding.actor_user_id == scope.user_id, PrivateRuntimeBinding.kind == kind,
            PrivateRuntimeBinding.provider == "private_wuying_v1")
        if binding_id:
            statement = statement.where(PrivateRuntimeBinding.id == binding_id)
        row = await db.scalar(statement)
        return None if row is None else _snapshot(row)


async def resolve_private_runtime(*, session_id, user_id, workspace_id=None, kind="sandbox", create=True, **unsupported):
    _config(user_id, kind)
    if unsupported:
        raise _error("PROVIDER_UNSUPPORTED", "Caller-supplied runtime adapters are not accepted", 403)
    scope = await private_session_scope(session_id=session_id, user_id=user_id, workspace_id=workspace_id)
    if scope is None:
        raise _error("SCOPE_INVALID", "This Session does not have a private runtime audience", 403)
    from sandbox.private_wuying import resolve
    return await resolve(scope, kind=kind, create=create)


async def validate_private_runtime(route, *, session_id, user_id, workspace_id=None, kind="sandbox", **unsupported):
    _config(user_id, kind)
    if unsupported:
        raise _error("PROVIDER_UNSUPPORTED", "Caller-supplied runtime adapters are not accepted", 403)
    scope = await private_session_scope(session_id=session_id, user_id=user_id, workspace_id=workspace_id)
    if scope is None or not isinstance(route, PrivateRuntimeRoute) or route.provider != "private_wuying_v1":
        raise _error("SCOPE_INVALID", "A current private Session and fixed Wuying route are required", 403)
    from sandbox.private_wuying import validate
    return await validate(scope, route, kind=kind)
