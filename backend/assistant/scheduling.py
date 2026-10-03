"""Persistent Task holds shared by Agent admission and dispatch boundaries.

Maintenance may still settle work that already happened. A hold prevents a
new run, input claim, provider request or tool body, including descendants of
the original linked execution. It never changes an input's recorded origin.
"""
from dataclasses import dataclass

from sqlalchemy import select

from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.assistant import AssistantTask
from db.models.session import Session


@dataclass(frozen=True)
class TaskHold:
    task_id: str | None
    state: str
    revision: int | None


class TaskSchedulingHeld(AssistantError):
    def __init__(self, hold):
        super().__init__(409, "ASSISTANT_TASK_HELD", "This task is paused, canceled or unavailable; automatic execution is held")
        self.hold = hold


async def held_task_locked(db, session, *, lock=False):
    """Inspect persisted lineage; a supplied ToolContext cannot erase a hold."""
    current, seen = session, set()
    while current is not None:
        if current.is_deleted or current.id in seen or len(seen) >= 64:
            return TaskHold(None, "unavailable", None)
        seen.add(current.id)
        query = select(AssistantTask).where(AssistantTask.execution_session_id == current.id)
        task = await db.scalar(query.with_for_update() if lock else query)
        if task is not None:
            if task.user_id != session.user_id or task.workspace_id != session.workspace_id:
                return TaskHold(None, "unavailable", None)
            if task.desired_state != "running":
                return TaskHold(task.id, task.desired_state, task.control_revision)
        if not current.parent_id:
            return None
        parent = await db.scalar(select(Session).where(Session.id == current.parent_id,
            Session.user_id == session.user_id, Session.workspace_id == session.workspace_id,
            Session.is_deleted.is_(False)))
        if parent is None:
            # Isolated descendants cannot escape a missing or reassigned
            # parent. Ordinary legacy Sessions keep their existing behavior.
            return TaskHold(None, "unavailable", None) if session.memory_policy == "assistant_isolated" else None
        current = parent
    return None


async def require_runnable_locked(db, session, *, lock=False):
    hold = await held_task_locked(db, session, lock=lock)
    if hold is not None:
        raise TaskSchedulingHeld(hold)


async def task_hold(session_id, user_id):
    if not session_id or not user_id:
        return None
    async with get_db_session() as db:
        session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id))
        return await held_task_locked(db, session) if session is not None else None


async def require_runnable(session_id, user_id, *, abort=None):
    hold = await task_hold(session_id, user_id)
    if hold is not None:
        if abort is not None:
            abort.set()
        raise TaskSchedulingHeld(hold)
