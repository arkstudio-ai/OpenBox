"""Refuse private inputs on the existing workspace-shared physical runtime.

Workspace ownership, a per-request scope header and a directory name do not
provide a private filesystem or desktop. These checks only deny entry; they
never authorize a private adapter or repair already delivered legacy files.
"""
from sqlalchemy import or_, select

from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.session import Session


class PrivateRuntimeUnavailable(AssistantError):
    def __init__(self):
        detail = ("当前桌面由工作区共享，尚未提供私有执行隔离。私有会话可以继续文字交流，"
                  "但不能在此环境准备、读写文件或操作应用。需要私有执行环境后才能继续这些操作。")
        super().__init__(409, "PRIVATE_SANDBOX_UNAVAILABLE", detail)
        self.payload = {"code": self.code, "state": "private_runtime_unavailable", "detail": detail}


def _private_source():
    return or_(Session.kind == "assistant", Session.visibility == "private",
               Session.memory_policy == "assistant_isolated")


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
