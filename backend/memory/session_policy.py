"""Persistent lineage isolation for every memory entry point."""
from sqlalchemy import select

from db.base import get_db_session
from db.models.session import Session
from memory.policy import MemoryAccessDenied

MEMORY_CAPABILITIES = frozenset({
    "creator_context", "memory_search", "memory_read_sources", "current_task_state", "memory_forget",
})


def memory_isolated(session) -> bool:
    return getattr(session, "memory_policy", "standard") != "standard" or session.kind == "assistant"


async def require_session_memory(db, *, session_id: str, user_id: str, workspace_id: str | None = None):
    query = select(Session).where(Session.id == session_id, Session.user_id == user_id,
                                  Session.is_deleted.is_(False))
    if workspace_id:
        query = query.where(Session.workspace_id == workspace_id)
    session = await db.scalar(query)
    if session is None or memory_isolated(session):
        raise MemoryAccessDenied("Session memory is unavailable under the persistent isolation policy")
    return session


async def require_context_memory(ctx):
    async with get_db_session() as db:
        return await require_session_memory(db, session_id=ctx.session_id, user_id=ctx.user_id,
                                            workspace_id=ctx.workspace_id)
