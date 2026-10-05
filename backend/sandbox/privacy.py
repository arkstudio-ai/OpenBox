"""Keep private inputs on a verified actor execution identity in Wuying.

Workspace ownership, a per-request scope header and a directory name do not
provide a private filesystem or desktop. Admission additionally requires the
original guest's UID/mount proof; it never repairs or relabels legacy files.
"""
from sqlalchemy import or_, select

from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.session import Session


class PrivateRuntimeUnavailable(AssistantError):
    def __init__(self, detail: str | None = None):
        detail = detail or ("当前桌面由工作区共享，尚未提供私有执行隔离。私有会话可以继续文字交流，"
                  "但不能在此环境准备、读写文件或操作应用。需要私有执行环境后才能继续这些操作。")
        super().__init__(409, "PRIVATE_SANDBOX_UNAVAILABLE", detail)
        self.payload = {"code": self.code, "state": "private_runtime_unavailable", "detail": detail}


def _private_source():
    return or_(Session.kind == "assistant", Session.visibility == "private",
               Session.memory_policy == "assistant_isolated")


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
    """Private destinations may receive this actor's current, same-workspace assets."""
    session = await db.get(Session, session_id)
    private = session is not None and (session.kind == "assistant"
        or session.visibility == "private" or session.memory_policy == "assistant_isolated")
    if not private:
        await require_shared_runtime(session_id)
        await require_shared_asset_sources(assets, db=db)
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


async def require_client_asset_sources(client, assets) -> None:
    await require_client_runtime(client)
    route = getattr(client, "private_runtime_route", None)
    if route is None:
        await require_shared_asset_sources(assets)
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


async def require_shared_asset_sources(assets, *, db=None) -> None:
    """A shared target must not launder an attachment from a private Session.

    Consult current SQL even when a caller holds older FileAsset objects. Also
    retain any original Session identity on those objects as a denial signal.
    """
    asset_ids = {row.id for row in assets if isinstance(getattr(row, "id", None), str)}
    source_ids = {row.session_id for row in assets
                  if isinstance(getattr(row, "session_id", None), str) and row.session_id}
    if not asset_ids and not source_ids:
        return
    source = Session.id.in_(source_ids)
    if asset_ids:
        source = or_(source, Session.id.in_(select(FileAsset.session_id).where(FileAsset.id.in_(asset_ids))))
    query = select(Session.id).where(source, _private_source()).limit(1)
    if db is not None:
        denied = await db.scalar(query)
    else:
        async with get_db_session() as current:
            denied = await current.scalar(query)
    if denied:
        raise PrivateRuntimeUnavailable()
