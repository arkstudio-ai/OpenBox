"""Recheck generated task inputs when execution history becomes evidence.

The execution's private audience is necessary but does not certify the
material used to write its instructions. A transcript read, copied history
or later main answer must retain those original command dependencies.
"""
from functools import lru_cache

from sqlalchemy import and_, bindparam, select

from assistant.policy import AssistantError
from assistant.source_scope import authority_columns, require_authority
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask, TaskResult
from db.models.project import Project
from db.models.session import Session


def _unavailable():
    return AssistantError(410, "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE", "Execution sources are unavailable")


async def _lineage(db, session_id, *, user_id, workspace_id, main_id):
    tasks, visited = {}, set()

    async def visit(current_id, path):
        if current_id in path or len(path) >= 32 or len(visited) >= 200:
            raise _unavailable()
        if current_id in visited:
            return
        # Read independent constructor facts together, without caching a
        # lineage proof or refreshing a caller's pending Session changes.
        # Scalar scope fields are current even if its ORM row was held earlier.
        current = (await db.execute(_lineage_statement(), {
            "source_user_id": user_id, "source_workspace_id": workspace_id, "source_session_id": current_id,
        })).first()
        if (current is None or current.is_deleted or current.user_id != user_id
                or current.workspace_id != workspace_id or current.visibility != "private"
                or current.kind != "normal" or current.memory_policy != "assistant_isolated"):
            raise _unavailable()
        if current.project_id is None:
            raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", "The owned project is unavailable")
        if current.task_id is not None:
            if main_id is not None and current.main_id != main_id:
                raise _unavailable()
            require_authority(db, main_id=current.main_id, active_member=current.active_member,
                              authority_main_id=current.authority_main_id)
            task = current.AssistantTask
            if task.user_id != user_id or task.workspace_id != workspace_id:
                raise AssistantError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable")
            # The current execution already passed all Session audience checks
            # above. Its owned project must also be this Task's exact project,
            # retaining task_locked's execution-before-project refusal order.
            if task.project_id != current.project_id:
                raise AssistantError(409, "ASSISTANT_EXECUTION_UNAVAILABLE", "The original execution Session is unavailable")
            tasks[task.id] = task
        birth = current.birth_payload if current.birth_id is not None else None
        if birth is not None and (birth.get("version") != 1
                or birth.get("parent_id") != current.parent_id):
            raise _unavailable()
        # Old direct tasks predate isolation birth events. Unlinked copies
        # and descendants require their durable constructor evidence.
        if current.task_id is None and birth is None:
            raise _unavailable()
        parents = (current.parent_id, birth.get("source_session_id") if birth else None)
        for parent_id in dict.fromkeys(filter(None, parents)):
            await visit(parent_id, (*path, current_id))
        visited.add(current_id)

    await visit(session_id, ())
    return tuple(tasks.values())


@lru_cache(maxsize=1)
def _lineage_statement():
    """The AST is shared, not a lineage/authority result or an actor identity."""
    user_id = bindparam("source_user_id", type_=Session.user_id.type)
    workspace_id = bindparam("source_workspace_id", type_=Session.workspace_id.type)
    current_id = bindparam("source_session_id", type_=Session.id.type)
    return (select(
            Session.user_id, Session.workspace_id, Session.is_deleted, Session.visibility,
            Session.kind, Session.memory_policy, Session.parent_id,
            Project.id.label("project_id"), AssistantTask.id.label("task_id"),
            AssistantTask.assistant_session_id.label("main_id"),
            AssistantTask,
            *authority_columns(user_id=user_id, workspace_id=workspace_id,
                               main_id=AssistantTask.assistant_session_id),
            AgentEvent.id.label("birth_id"), AgentEvent.payload.label("birth_payload"),
        ).select_from(Session).outerjoin(Project, and_(
            Project.id == Session.project_id, Project.user_id == user_id,
            Project.workspace_id == workspace_id, Project.is_deleted.is_(False),
        )).outerjoin(AssistantTask, AssistantTask.execution_session_id == Session.id)
        .outerjoin(AgentEvent, and_(AgentEvent.session_id == Session.id,
            AgentEvent.user_id == user_id, AgentEvent.kind == "assistant.isolation.created"))
        .where(Session.id == current_id).execution_options(populate_existing=True))


async def validate_execution_message(db, message, *, user_id, workspace_id, main_id=None, snapshot_checks=None):
    """Validate original materialized inputs, never a later unconsumed followup."""
    if message.user_id != user_id:
        raise _unavailable()
    async def original_lineage():
        return await _lineage(db, message.session_id, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    scope = (user_id, workspace_id, main_id)
    tasks = (await original_lineage() if snapshot_checks is None else await snapshot_checks.check(db,
        "execution_lineage", scope, message.session_id, original_lineage))

    async def original_boundary():
        # A final answer may settle several inputs in one logical turn.
        # Retain the original result boundary when it is available.
        result = await db.scalar(select(TaskResult).where(TaskResult.result_message_id == message.id)
            .order_by(TaskResult.created_at, TaskResult.id).limit(1))
        return result.created_at if result is not None else message.created_at
    before = (await original_boundary() if snapshot_checks is None else await snapshot_checks.check(db,
        "execution_message_boundary", scope, message.id, original_boundary))
    from assistant.command_sources import validate_task_command_sources
    from assistant.schedule_runs import validate_task_schedule_locked
    for task in tasks:
        # Never cache a recursive graph, even when its independent SQL rows
        # are reused within this one read-only snapshot.
        await validate_task_schedule_locked(db, task, snapshot_checks=snapshot_checks)
        await validate_task_command_sources(db, task, before=before, snapshot_checks=snapshot_checks)
