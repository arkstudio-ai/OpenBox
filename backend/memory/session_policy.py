"""Persistent lineage isolation for every memory entry point.

Two separate questions are answered here:

- ``memory_isolated``: may this session use memory (tools, recall)? It also
  decides which tools are stripped, and is unchanged by personal extraction.
- ``memory_extraction_eligible`` / ``memory_source_session``: may what the
  person says in this session be learned from, and does evidence cited from it
  still stand? The person's own assistant main session is memory-isolated for
  tools, yet its human turns become personal memory.
"""
from sqlalchemy import and_, or_, select

from db.base import get_db_session
from db.models.session import Session
from memory.policy import MemoryAccessDenied

MEMORY_CAPABILITIES = frozenset({
    "creator_context", "memory_search", "memory_read_sources", "current_task_state", "memory_forget",
})


def memory_isolated(session) -> bool:
    return getattr(session, "memory_policy", "standard") != "standard" or session.kind == "assistant"


def memory_extraction_eligible(session) -> bool:
    """Whether the person's own words in this session may become memories.

    Delegated children and scheduled runs (``parent_id``) never qualify: their
    user-role turns were written by a parent or a scheduler. The assistant main
    session qualifies whatever its stored policy (it is created
    ``assistant_isolated``); old ``assistant_isolated`` execution sessions do not.
    """
    if getattr(session, "parent_id", None):
        return False
    if session.kind == "assistant":
        return True
    return getattr(session, "memory_policy", "standard") == "standard"


def memory_extraction_clause(model=Session):
    """SQL form of ``memory_extraction_eligible``."""
    return and_(model.parent_id.is_(None), or_(model.kind == "assistant", model.memory_policy == "standard"))


def memory_source_session(session) -> bool:
    """Whether evidence cited from this session is still under the memory policy.

    The standard rule (a Task child included: tools there can cite it), plus the
    person's own assistant main session.
    """
    if session.kind == "assistant":
        return getattr(session, "parent_id", None) is None
    return getattr(session, "memory_policy", "standard") == "standard"


def memory_source_session_clause(model=Session):
    """SQL form of ``memory_source_session``."""
    return or_(and_(model.kind != "assistant", model.memory_policy == "standard"),
               and_(model.kind == "assistant", model.parent_id.is_(None)))


def memory_session_clause(scope, model=Session):
    """A live session whose memory diagnostics the scope's actor may read.

    A standard session inside the scope, or the actor's own assistant main
    session, which is personal whatever container project it is stored in.
    """
    return and_(model.is_deleted.is_(False), or_(
        and_(*scope.predicates(model, personal_visibility=False),
             model.memory_policy == "standard", model.kind != "assistant"),
        and_(model.user_id == scope.actor_user_id, model.workspace_id == scope.workspace_id,
             model.kind == "assistant", model.parent_id.is_(None))))


def extraction_project_id(session) -> str | None:
    """The project that memories and evidence extracted from this session belong to.

    The assistant main session is stored in the workspace's default container
    project, which may belong to another member. What the person tells their
    assistant is personal, project-independent context.
    """
    return None if session.kind == "assistant" else session.project_id


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
