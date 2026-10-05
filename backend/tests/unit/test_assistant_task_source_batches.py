"""Task source batches preserve every observation and fresh database authority."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import event

from assistant.commands import command_digest, read_task_scopes
from assistant.policy import AssistantError
from assistant.task_context import task_context, validate_task_snapshots
from db.base import get_db_session, get_engine
from db.models.assistant import AssistantTask, TaskResult
from db.models.session import Session
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_source_queries import original_result  # noqa: F401


async def observed_task(scope):
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        _, refs = await task_context(db, main)
        assert len(refs) == 1 and refs[0]["snapshot"]["latest_result"]
        return refs[0]


async def test_full_source_budget_batches_reads_and_checks_duplicate_observations(original_result):
    scope, _, _ = original_result
    original = await observed_task(scope)
    refs = [deepcopy(original) for _ in range(200)]
    # The same task may occur in many historical observations. Each body and
    # version still needs checking, even when it shares a database lookup.
    refs[-1]["snapshot"]["task"]["observed_state"] = "queued"
    refs[-1]["snapshot_digest"] = command_digest(refs[-1]["snapshot"])
    queries = []
    def count(*args):
        queries.append(args[2])
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", count)
        try:
            await validate_task_snapshots(db, main, refs)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert len(queries) <= 4, len(queries)
        refs[-1]["snapshot"]["latest_result"]["outcome"] = "failed"
        refs[-1]["snapshot_digest"] = command_digest(refs[-1]["snapshot"])
        with pytest.raises(AssistantError) as denied:
            await validate_task_snapshots(db, main, refs)
        assert denied.value.code == "ASSISTANT_TASK_SNAPSHOT_CHANGED"


async def test_task_scope_batches_keep_order_and_reject_a_revoked_later_batch(original_result):
    scope, receipt, _ = original_result
    ids, sessions = [], []
    async with get_db_session() as db:
        original = await db.get(AssistantTask, receipt["task_id"])
        execution = await db.get(Session, receipt["execution_session_id"])
        for _ in range(105):
            session_id, task_id = uuid4().hex, uuid4().hex
            session = Session(**{column.name: deepcopy(getattr(execution, column.name))
                for column in Session.__table__.columns if column.name != "id"}, id=session_id)
            task = AssistantTask(**{column.name: deepcopy(getattr(original, column.name))
                for column in AssistantTask.__table__.columns
                if column.name not in {"id", "execution_session_id", "latest_result_id"}},
                id=task_id, execution_session_id=session_id, latest_result_id=None)
            db.add(session)
            await db.flush()
            db.add(task)
            ids.append(task_id)
            sessions.append(session_id)
    requested = [*reversed(ids), ids[0], ids[-1]]
    queries = []
    def count(*args):
        queries.append(args[2])
    async with get_db_session() as db:
        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", count)
        try:
            values = await read_task_scopes(db, **scope, task_ids=requested)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert [task.id for task, _ in values] == requested
        assert len(queries) == 2
    # The first-created task is in the second SQL batch in the reversed read.
    async with get_db_session() as db:
        (await db.get(Session, sessions[0])).visibility = "workspace"
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as denied:
            await read_task_scopes(db, **scope, task_ids=requested)
        assert denied.value.code == "ASSISTANT_EXECUTION_UNAVAILABLE"


@pytest.mark.parametrize("result_id", [None, [], {}, 42])
async def test_malformed_result_ids_fail_as_unavailable_evidence(original_result, result_id):
    scope, _, _ = original_result
    ref = await observed_task(scope)
    ref["snapshot"]["latest_result"]["result_id"] = result_id
    ref["snapshot_digest"] = command_digest(ref["snapshot"])
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        with pytest.raises(AssistantError) as denied:
            await validate_task_snapshots(db, main, [ref])
        assert denied.value.code == "ASSISTANT_TASK_SNAPSHOT_CHANGED"


@pytest.mark.parametrize("field", ["outcome", "result_message_id", "generation"])
async def test_held_result_is_refreshed_after_an_independent_source_change(original_result, field):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Fresh READ COMMITTED source reads require independent PostgreSQL connections")
    scope, _, result_id = original_result
    ref = await observed_task(scope)
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        held = await db.get(TaskResult, result_id)
        await validate_task_snapshots(db, main, [ref])
        async with get_db_session() as writer:
            result = await writer.get(TaskResult, result_id)
            value = {"outcome": "failed", "result_message_id": None, "generation": result.generation + 1}[field]
            setattr(result, field, value)
        assert held is not None
        with pytest.raises(AssistantError) as denied:
            await validate_task_snapshots(db, main, [ref])
        assert denied.value.code == "ASSISTANT_TASK_SNAPSHOT_CHANGED"
