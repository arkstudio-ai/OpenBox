"""Which Sessions may use the shared workspace runtime, and which files they get.

Only the assistant's own main conversation (Session.kind == "assistant") is
private for runtime purposes; it never runs on a sandbox or cloud desktop.
Every other Session, including the private-visibility, memory-isolated
Sessions the assistant delegates work to, their children, forks and
subagents, runs on the shared workspace runtime like an ordinary project
Session. "private" visibility only decides who can see a conversation;
memory isolation and task control are enforced elsewhere.

Files the user gave the assistant remain that user's own files. A runtime may
receive them for the same user in the same workspace while they are current,
ready and not deleted; ownership, not the source conversation, decides.

The dormant private actor runtime (sandbox.private_runtime) still serves only
private Sessions and keeps its own guest identity checks.
"""
from contextlib import asynccontextmanager

from sqlalchemy import select

from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.session import Session

# Fields that identify an asset's object and owner. A rename is progress, not
# a different file, so the name is deliberately not part of this identity.
_ASSET_IDENTITY = ("session_id", "user_id", "workspace_id", "oss_key", "size", "mime")


class PrivateRuntimeUnavailable(AssistantError):
    def __init__(self, detail: str | None = None):
        detail = detail or ("当前桌面由工作区共享，尚未提供私有执行隔离。私有会话可以继续文字交流，"
                  "但不能在此环境准备、读写文件或操作应用。需要私有执行环境后才能继续这些操作。")
        super().__init__(409, "PRIVATE_SANDBOX_UNAVAILABLE", detail)
        self.payload = {"code": self.code, "state": "private_runtime_unavailable", "detail": detail}


def runtime_private(session) -> bool:
    """Only the assistant's own main conversation is private for runtime purposes."""
    return session is not None and session.kind == "assistant"


def _private_source():
    """SQL form of runtime_private()."""
    return Session.kind == "assistant"


@asynccontextmanager
async def _reader(db):
    if db is not None:
        yield db
    else:
        async with get_db_session() as current:
            yield current


async def session_requires_private_runtime(session_id: str) -> bool:
    """A denial signal only; resolving a private route performs full authority checks."""
    async with get_db_session() as db:
        return bool(await db.scalar(select(Session.id).where(
            Session.id == session_id, _private_source())))


async def require_client_runtime(client, session_id: str | None = None) -> None:
    """Validate the original private binding; traces cannot replace its Session."""
    route = getattr(client, "private_runtime_route", None)
    if route is None:
        await require_shared_runtime(session_id)
        return
    from agent.driver import _current_lease
    from sandbox.private_runtime import validate_private_runtime
    bound_session = getattr(client, "private_session_id", None)
    lease = _current_lease.get()
    if (not bound_session or session_id and session_id != bound_session
            or lease is not None and (lease.session_id, lease.user_id)
                != (bound_session, route.actor_user_id)):
        raise PrivateRuntimeUnavailable("私有执行环境与当前会话不匹配。")
    await validate_private_runtime(route, session_id=bound_session,
        user_id=route.actor_user_id, workspace_id=route.workspace_id)


async def require_session_asset_sources(assets, *, session_id: str, user_id: str, db) -> None:
    """A destination Session receives only its actor's current, same-workspace files."""
    session = await db.get(Session, session_id)
    if not runtime_private(session):
        await require_shared_runtime(session_id)
        await require_shared_asset_sources(assets, db=db, user_id=user_id,
            workspace_id=session.workspace_id if session is not None else None)
        return
    from sandbox.private_runtime import private_session_scope
    scope = await private_session_scope(session_id=session_id, user_id=user_id,
        workspace_id=session.workspace_id)
    if scope is None:
        raise PrivateRuntimeUnavailable()
    ids = {asset.id for asset in assets}
    current = list((await db.scalars(select(FileAsset).where(FileAsset.id.in_(ids)))).all())
    expected = {asset.id: asset for asset in assets}
    if len(current) != len(ids):
        raise PrivateRuntimeUnavailable("私有附件来源已不可用。")
    for asset in current:
        original = expected[asset.id]
        if (asset.user_id != user_id or asset.workspace_id != session.workspace_id
                or asset.is_deleted or asset.status != "ready"
                or any(getattr(asset, field) != getattr(original, field) for field in
                       ("session_id", "user_id", "workspace_id", "name", "oss_key", "size", "mime"))):
            raise PrivateRuntimeUnavailable("私有附件来源已改变。")
        if asset.session_id:
            source = await db.get(Session, asset.session_id)
            if (source is None or source.is_deleted or source.user_id != user_id
                    or source.workspace_id != session.workspace_id):
                raise PrivateRuntimeUnavailable("私有附件来源已不可用。")


async def require_client_asset_sources(client, assets, *, user_id: str | None = None,
                                       workspace_id: str | None = None) -> None:
    await require_client_runtime(client)
    route = getattr(client, "private_runtime_route", None)
    if route is None:
        await require_shared_asset_sources(assets, user_id=user_id, workspace_id=workspace_id)
        return
    async with get_db_session() as db:
        await require_session_asset_sources(assets, session_id=client.private_session_id,
            user_id=route.actor_user_id, db=db)


async def require_shared_runtime(session_id: str | None = None) -> None:
    """Check both the actual Driver and an explicit persisted target afresh.

    Synthetic management IDs and unbound ordinary maintenance remain valid.
    A caller-supplied ID cannot grant access or replace a private Driver's ID.
    """
    from agent.driver import _current_lease
    lease = _current_lease.get()
    ids = {value for value in (session_id, lease.session_id if lease else None)
           if isinstance(value, str) and value}
    if not ids:
        return
    async with get_db_session() as db:
        if await db.scalar(select(Session.id).where(Session.id.in_(ids), _private_source()).limit(1)):
            raise PrivateRuntimeUnavailable()


async def _driver_actor(db):
    """The current Driver Session's owner and workspace, if a run is bound."""
    from agent.driver import _current_lease
    lease = _current_lease.get()
    if lease is None:
        return None
    session = await db.get(Session, lease.session_id)
    if session is None or session.is_deleted or session.user_id != lease.user_id:
        return None
    return session.user_id, session.workspace_id


async def require_shared_asset_sources(assets, *, db=None, user_id: str | None = None,
                                       workspace_id: str | None = None) -> None:
    """A shared target receives a file from the assistant conversation only as its owner's file.

    The destination actor is the explicit user and workspace (both, or neither)
    or else the current Driver Session's owner; an explicit actor must agree
    with a bound Driver, and without any actor such a file is refused. It must
    be that actor's current, ready, undeleted file in the same workspace, and
    its source conversation must belong to the same actor. Consult current SQL
    even when a caller holds older FileAsset objects, and keep each object's
    original Session as a source signal. Other files keep their callers' own
    ownership rules.
    """
    asset_ids = {row.id for row in assets if isinstance(getattr(row, "id", None), str)}
    original_sources = {row.session_id for row in assets
                        if isinstance(getattr(row, "session_id", None), str) and row.session_id}
    if not asset_ids and not original_sources:
        return
    async with _reader(db) as current:
        rows = {row.id: row for row in (await current.scalars(select(FileAsset).where(
            FileAsset.id.in_(asset_ids)))).all()} if asset_ids else {}
        sources = original_sources | {row.session_id for row in rows.values() if row.session_id}
        private = {row.id: row for row in (await current.scalars(select(Session).where(
            Session.id.in_(sources), _private_source()))).all()} if sources else {}
        if not private:
            return
        actor = (user_id, workspace_id) if user_id and workspace_id else None
        driver = await _driver_actor(current)
        if actor is None:
            actor = driver
        elif driver is not None and driver != actor:
            actor = None
        if actor is None:
            raise PrivateRuntimeUnavailable("助手对话里的文件只能交给其所有者在同一工作空间的会话。")
        user_id, workspace_id = actor
        if any(source.is_deleted or source.user_id != user_id or source.workspace_id != workspace_id
               for source in private.values()):
            raise PrivateRuntimeUnavailable("私有附件来源已不可用。")
        for held in assets:
            row = rows.get(getattr(held, "id", None))
            if getattr(held, "session_id", None) not in private and (row is None or row.session_id not in private):
                continue
            if (row is None or row.user_id != user_id or row.workspace_id != workspace_id
                    or row.is_deleted or row.status != "ready"
                    or any(hasattr(held, field) and getattr(held, field) != getattr(row, field)
                           for field in _ASSET_IDENTITY)):
                raise PrivateRuntimeUnavailable("私有附件来源已改变。")
