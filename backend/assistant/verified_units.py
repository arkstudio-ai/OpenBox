"""Validation units whose verdicts are reused while their closure is current.

Each unit wraps one original validation (assistant.evidence_cache): ``slow``
runs it unchanged in the caller's session and ``capture`` runs the same
validation from ids in a new read-only snapshot. A unit is used only for a
clean persisted row, since a capture reads the row from the database again.
"""
from sqlalchemy import inspect as sa_inspect

from assistant.policy import AssistantError
from db.models.assistant import AssistantTask
from db.models.message import Message


def clean(row):
    """A persisted row without pending changes is the row a capture reads."""
    state = sa_inspect(row, raiseerr=False)
    return state is not None and state.persistent and not state.modified


def _moment(value):
    return value.isoformat() if value is not None else None


def task_graph_unit(task_id, *, user_id, workspace_id, main_id, before):
    return ("task_graph", (user_id, workspace_id), (task_id, main_id, _moment(before)))


def message_unit(message_id, *, user_id, workspace_id, main_id):
    return ("message_sources", (user_id, workspace_id), (message_id, main_id))


def execution_message_unit(message_id, *, user_id, workspace_id, main_id):
    return ("execution_message", (user_id, workspace_id), (message_id, main_id or ""))


async def _scoped_task(snapshot, task_id, user_id, workspace_id, main_id):
    task = await snapshot.get(AssistantTask, task_id)
    if task is None or (task.user_id, task.workspace_id, task.assistant_session_id) != (user_id, workspace_id, main_id):
        raise AssistantError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable")
    return task


async def task_graph(db, task, *, before, checks):
    """A Task's schedule and command-source graph, up to ``before`` when given."""
    from assistant.command_sources import validate_task_command_sources
    from assistant.evidence_cache import verified
    from assistant.schedule_runs import validate_task_schedule_locked
    task_id, user_id, workspace_id, main_id = task.id, task.user_id, task.workspace_id, task.assistant_session_id
    unit, scope, key = task_graph_unit(task_id, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                       before=before)

    async def slow():
        await validate_task_schedule_locked(db, task, snapshot_checks=checks)
        await validate_task_command_sources(db, task, before=before, snapshot_checks=checks)
        return True

    async def capture(snapshot):
        from assistant.transactions import boundary_checks
        original = await _scoped_task(snapshot, task_id, user_id, workspace_id, main_id)
        with boundary_checks(snapshot) as shared:
            await validate_task_schedule_locked(snapshot, original, snapshot_checks=shared)
            await validate_task_command_sources(snapshot, original, before=before, snapshot_checks=shared)
        return True

    if not clean(task):
        return await slow()
    return (await verified(db, unit, key, scope, slow, capture))[0]


async def message_sources(db, message, *, user_id, workspace_id, main_id, snapshot_checks):
    """A main answer's whole source graph (evidence.validate_message_sources)."""
    from assistant.evidence import _validate_message_sources, validate_message_sources
    from assistant.evidence_cache import verified
    unit, scope, key = message_unit(message.id, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    message_id = message.id

    async def slow():
        await _validate_message_sources(db, message, user_id=user_id, workspace_id=workspace_id,
                                        main_id=main_id, snapshot_checks=snapshot_checks)
        return True

    async def capture(snapshot):
        from assistant.transactions import boundary_checks
        original = await snapshot.get(Message, message_id)
        if original is None or (original.session_id, original.user_id) != (main_id, user_id):
            raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency could not be verified")
        with boundary_checks(snapshot) as shared:
            await validate_message_sources(snapshot, original, user_id=user_id, workspace_id=workspace_id,
                main_id=main_id, validation={"messages": set(), "refs": {}, "snapshot_checks": shared})
        return True

    if not clean(message):
        await slow()
        return
    await verified(db, unit, key, scope, slow, capture)


async def execution_message(db, message, *, user_id, workspace_id, main_id, snapshot_checks):
    """An execution message's original inputs (execution_sources.validate_execution_message)."""
    from assistant.evidence_cache import verified
    from assistant.execution_sources import _checked_execution_message
    unit, scope, key = execution_message_unit(message.id, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    message_id = message.id

    async def slow():
        await _checked_execution_message(db, message, user_id=user_id, workspace_id=workspace_id,
                                         main_id=main_id, snapshot_checks=snapshot_checks)
        return True

    async def capture(snapshot):
        from assistant.transactions import boundary_checks
        original = await snapshot.get(Message, message_id)
        if original is None or original.user_id != user_id:
            raise AssistantError(410, "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE", "Execution sources are unavailable")
        with boundary_checks(snapshot) as shared:
            await _checked_execution_message(snapshot, original, user_id=user_id, workspace_id=workspace_id,
                                             main_id=main_id, snapshot_checks=shared)
        return True

    if not clean(message):
        await slow()
        return
    await verified(db, unit, key, scope, slow, capture)
