"""The assistant's own conversation gets no sandbox; files keep their owner.

Only the assistant's main conversation (Session.kind == "assistant") is
refused the workspace runtime: it never runs on a sandbox or cloud desktop.
Every other Session, including the private-visibility, memory-isolated
Sessions the assistant delegates work to, their children, forks and
subagents, runs on the shared workspace runtime like an ordinary project
Session. "private" visibility only decides who can see a conversation;
memory isolation and task control are enforced elsewhere.

Files the user gave the assistant remain that user's own files. A runtime may
receive them for the same user in the same workspace while they are current,
ready and not deleted; ownership, not the source conversation, decides.
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
    """The assistant's own conversation asked for the workspace runtime.

    The code (and the payload state) are stable: clients map them.
    """

    def __init__(self, detail: str | None = None):
        detail = detail or ("助手对话本身不使用工作区云电脑：可以继续文字交流，"
                  "但不能在这里准备、读写文件或操作应用。需要时请交给一个任务去做。")
        super().__init__(409, "PRIVATE_SANDBOX_UNAVAILABLE", detail)
        self.payload = {"code": self.code, "state": "private_runtime_unavailable", "detail": detail}


def _assistant_conversation():
    """SQL test for the assistant's own (main) conversation."""
    return Session.kind == "assistant"


@asynccontextmanager
async def _reader(db):
    if db is not None:
        yield db
    else:
        async with get_db_session() as current:
            yield current


async def require_shared_runtime(session_id: str | None = None) -> None:
    """Refuse the runtime to the assistant's own conversation, checked afresh.

    Both the actual Driver and an explicit persisted target are checked.
    Synthetic management IDs and unbound ordinary maintenance remain valid.
    A caller-supplied ID cannot grant access or replace the Driver's ID.
    """
    from agent.driver import _current_lease
    lease = _current_lease.get()
    ids = {value for value in (session_id, lease.session_id if lease else None)
           if isinstance(value, str) and value}
    if not ids:
        return
    async with get_db_session() as db:
        if await db.scalar(select(Session.id).where(Session.id.in_(ids), _assistant_conversation()).limit(1)):
            raise PrivateRuntimeUnavailable()


async def require_session_asset_sources(assets, *, session_id: str, user_id: str, db) -> None:
    """A destination Session receives only its actor's current, same-workspace files."""
    await require_shared_runtime(session_id)
    session = await db.get(Session, session_id)
    await require_shared_asset_sources(assets, db=db, user_id=user_id,
        workspace_id=session.workspace_id if session is not None else None)


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
            Session.id.in_(sources), _assistant_conversation()))).all()} if sources else {}
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
