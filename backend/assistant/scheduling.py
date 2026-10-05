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


async def held_task_locked(db, session, *, lock=False, resume_command_id=None, replacing_continuation=False):
    """Inspect persisted lineage; a supplied ToolContext cannot erase a hold."""
    return await _held_task(db, session, lock=lock, resume_command_id=resume_command_id,
                            replacing_continuation=replacing_continuation)


async def _held_task(db, session, *, lock=False, resume_command_id=None,
                     replacing_continuation=False, snapshot_checks=None):
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
            from assistant.commands import _authority, _project
            from db.models.user import User
            try:
                main = await _authority(db, user_id=task.user_id, workspace_id=task.workspace_id,
                                 main_id=task.assistant_session_id)
                await _project(db, task.project_id, task.user_id, task.workspace_id)
                from assistant.schedule_runs import validate_task_schedule_locked
                await validate_task_schedule_locked(db, task, snapshot_checks=snapshot_checks)
                from assistant.command_sources import validate_task_command_sources
                await validate_task_command_sources(db, task, snapshot_checks=snapshot_checks)
                from assistant.continuation import validate_execution_authority
                if not replacing_continuation:
                    await validate_execution_authority(db, main, task, snapshot_checks=snapshot_checks)
            except AssistantError:
                return TaskHold(task.id, "unavailable", task.control_revision)
            if (current.project_id != task.project_id or current.visibility != "private"
                    or current.memory_policy != "assistant_isolated" or current.kind != "normal"
                    or not await db.scalar(select(User.id).where(User.id == task.user_id,
                        User.is_active.is_(True), User.is_deleted.is_(False)))):
                return TaskHold(task.id, "unavailable", task.control_revision)
            if task.desired_state != "running":
                return TaskHold(task.id, task.desired_state, task.control_revision)
            if task.observed_state == "resuming":
                from assistant.control import pending_resume_locked
                pending = await pending_resume_locked(db, task.id)
                if pending is not None and pending.id != resume_command_id:
                    return TaskHold(task.id, "resuming", task.control_revision)
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


async def require_runnable_locked(db, session, *, lock=False, resume_command_id=None, replacing_continuation=False):
    hold = await held_task_locked(db, session, lock=lock, resume_command_id=resume_command_id,
                                  replacing_continuation=replacing_continuation)
    if hold is not None:
        raise TaskSchedulingHeld(hold)


async def task_hold(session_id, user_id):
    if not session_id or not user_id:
        return None
    async with get_db_session() as db:
        session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id))
        return await held_task_locked(db, session) if session is not None else None


async def observe_task_hold(session_id, user_id):
    """Observe a monitor hold in one new read-only snapshot per poll.

    This only requests cancellation; it cannot admit a run or authorize any
    provider/tool dispatch. Those boundaries continue to use task_hold or
    require_runnable_locked and independently revalidate current authority.
    """
    if not session_id or not user_id:
        return None
    from assistant.transactions import source_snapshot
    async with source_snapshot() as (db, checks):
        session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id))
        return await _held_task(db, session, snapshot_checks=checks) if session is not None else None


async def require_runnable(session_id, user_id, *, abort=None):
    hold = await task_hold(session_id, user_id)
    if hold is not None:
        if abort is not None:
            abort.set()
        raise TaskSchedulingHeld(hold)
