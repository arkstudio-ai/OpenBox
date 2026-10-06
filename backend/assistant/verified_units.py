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


def _graph(validation, height):
    """What an answer's walk proved: every answer and source ref, and its depth."""
    return frozenset(validation["messages"]), frozenset(validation["refs"]), height


async def _walk(db, message, *, user_id, workspace_id, main_id, checks):
    from assistant.evidence import _validate_message_sources
    from assistant.evidence_cache import measuring_height
    validation = {"messages": set(), "refs": {}, "snapshot_checks": checks}
    with measuring_height() as height:
        await _validate_message_sources(db, message, user_id=user_id, workspace_id=workspace_id,
                                        main_id=main_id, validation=validation)
    return _graph(validation, height[0])


def _message_capture(message_id, *, user_id, workspace_id, main_id):
    async def capture(snapshot):
        from assistant.transactions import boundary_checks
        original = await snapshot.get(Message, message_id)
        if original is None or (original.session_id, original.user_id) != (main_id, user_id):
            raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency could not be verified")
        with boundary_checks(snapshot) as shared:
            return await _walk(snapshot, original, user_id=user_id, workspace_id=workspace_id,
                               main_id=main_id, checks=shared)
    return capture


async def message_sources(db, message, *, user_id, workspace_id, main_id, snapshot_checks):
    """A main answer's whole source graph (evidence.validate_message_sources)."""
    from assistant.evidence_cache import verified
    from assistant.transactions import within_boundary
    unit, scope, key = message_unit(message.id, user_id=user_id, workspace_id=workspace_id, main_id=main_id)

    async def slow():
        if snapshot_checks is not None:
            return await _walk(db, message, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                               checks=snapshot_checks)
        # A top-level validation is one boundary (see BoundaryChecks).
        return await within_boundary(db, lambda checks: _walk(db, message, user_id=user_id,
            workspace_id=workspace_id, main_id=main_id, checks=checks),
            user_id=user_id, workspace_id=workspace_id, main_id=main_id)

    if not clean(message):
        await slow()
        return
    await verified(db, unit, key, scope, slow,
                   _message_capture(message.id, user_id=user_id, workspace_id=workspace_id, main_id=main_id))


async def reuse_message_sources(db, message, *, user_id, workspace_id, main_id, depth, visited, validation):
    """Apply a current answer verdict inside a caller's shared walk, when exact.

    A verdict proven at the top level holds at any position on a path: its
    graph reaches no answer that reaches it, so no cycle forms. Its depth and
    its answers/refs still count against this walk's budgets, so it is used
    only where the walk would not have stopped, and its sets join the walk.
    """
    from assistant.evidence_cache import PROVEN, mode, note_depth, reuse
    if not clean(message) or (visited and message.id in visited):
        return False
    unit, scope, key = message_unit(message.id, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    value = await reuse(db, unit, key, scope,
                        _message_capture(message.id, user_id=user_id, workspace_id=workspace_id, main_id=main_id))
    if value is None:
        return False
    messages, refs, height = value
    added_messages, added_refs = messages - validation["messages"], refs - set(validation["refs"])
    if (depth + height > 64 or len(validation["messages"]) + len(added_messages) >= 200
            or len(validation["refs"]) + len(added_refs) >= 200):
        return False
    if mode() == "verify":
        await _verify_in_place(db, message, value, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                               depth=depth, visited=visited, validation=validation)
    validation["messages"] |= messages
    for ref_key in added_refs:
        validation["refs"][ref_key] = PROVEN
    note_depth(depth + height)
    return True


async def _verify_in_place(db, message, value, *, user_id, workspace_id, main_id, depth, visited, validation):
    from assistant.evidence import _validate_message_sources
    from assistant.policy import AssistantError
    probe = {"messages": set(validation["messages"]), "refs": dict(validation["refs"]),
             "snapshot_checks": validation.get("snapshot_checks")}
    try:
        await _validate_message_sources(db, message, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                        visited=visited, depth=depth, validation=probe)
    except AssistantError as error:
        raise AssertionError(f"evidence cache reused answer {message.id} in place but validation refused it: "
                             f"{error.code}") from error
    if (probe["messages"] - validation["messages"] != value[0] - validation["messages"]
            or set(probe["refs"]) - set(validation["refs"]) != value[1] - set(validation["refs"])):
        raise AssertionError(f"evidence cache reused answer {message.id} in place with a different graph")


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
