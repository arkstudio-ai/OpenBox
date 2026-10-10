"""PA-10: two independent workers observe pending before either takes the lock."""
import asyncio
import json

import pytest
from sqlalchemy import func, select, text

from assistant import results
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult, TaskSubmission
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready


async def test_pa10_workers_recheck_pending_under_lock_and_replay_one_attempt(monkeypatch, record_property):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent concurrent pending reads require PostgreSQL")
    owner, _, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        url = db.get_bind().url
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
        result_id, original_run, original_generation = result.id, result.run_id, result.generation
    # The durable pending result survives loss of all connections and queues.
    await close_engine()
    init_engine(url.render_as_string(hide_password=False))
    readers, observed = set(), []
    both_read_pending = asyncio.Event()
    original_lock = results._lock_fenced

    async def rendezvous(db, *args, **kwargs):
        # These queries use each production worker's own live transaction.
        # Both must see pending before either is allowed to take the main lock.
        readers.add(await db.scalar(text("SELECT pg_backend_pid()")))
        observed.append(await db.scalar(select(TaskResult.delivery_state).where(TaskResult.id == result_id)))
        if len(observed) == 2:
            both_read_pending.set()
        await asyncio.wait_for(both_read_pending.wait(), 5)
        return await original_lock(db, *args, **kwargs)

    monkeypatch.setattr(results, "_lock_fenced", rendezvous)
    workers = [asyncio.create_task(results.deliver_task_result(result_id)) for _ in range(2)]
    try:
        first, second = await asyncio.wait_for(asyncio.gather(*workers), 10)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        monkeypatch.setattr(results, "_lock_fenced", original_lock)
    assert len(readers) == 2 and observed == ["pending", "pending"]
    assert first == second and first["result_id"] == result_id and first["report_attempt"] == 1
    # Lose the response and every connection again. Replay must not advance
    # the attempt or launch either an execution or a report provider.
    await close_engine()
    init_engine(url.render_as_string(hide_password=False))
    assert await results.deliver_task_result(result_id) == first
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        task = await db.get(AssistantTask, accepted["task_id"])
        report = await db.get(AgentInboxItem, first["inbox_id"])
        driver = await db.get(AgentDriverState, task.execution_session_id)
        counts = {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("results", TaskResult, TaskResult.task_id == task.id),
                ("submissions", TaskSubmission, TaskSubmission.task_id == task.id),
                ("main_inbox", AgentInboxItem, AgentInboxItem.session_id == main.id),
                ("accepted_events", AgentEvent, (AgentEvent.session_id == main.id)
                    & (AgentEvent.kind == "assistant.result.accepted")),
            )}
        assert counts == {"results": 1, "submissions": 1, "main_inbox": 1, "accepted_events": 1}
        assert result.delivery_state == "accepted" and result.report_attempt == 1 and result.retry_count == 0
        assert result.assistant_inbox_id == report.id and result.processed_message_id is None
        assert (result.run_id, result.generation) == (original_run, original_generation)
        assert report.state == "accepted" and report.origin == "task_result"
        assert report.origin_ref == {"result_id": result.id, "task_id": task.id,
            "report_attempt": 1, "execution_mode": "report_only"}
        assert driver.phase == "idle" and driver.generation == original_generation
        assert await db.get(AgentDriverState, main.id) is None
        accepted_event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "assistant.result.accepted"))
        witness = {"scenario": "PA-10", "main_id": main.id, "task_id": task.id,
            "execution_session_id": task.execution_session_id, "command_id": accepted["command_id"],
            "submission_id": accepted["submission_id"], "execution_inbox_id": accepted["inbox_id"],
            "task_revision": task.control_revision, "run_id": original_run, "generation": original_generation,
            "result_id": result.id, "accepted_event_id": accepted_event.id, "receipt": first,
            "independent_connection_count": len(readers), "initial_states": observed,
            "counts": counts, "post_reopen_receipt_identical": True, "new_execution_or_report_run": False}
    record_property("assistant_acceptance", json.dumps(witness))
