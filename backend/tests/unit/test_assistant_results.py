"""Durable execution results use real Inbox, event, lease and SQL boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.commands import accept_task_command
from assistant.results import deliver_task_result
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.part import Part
from db.models.workspace import WorkspaceMember
from models.message import TextPart
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


async def result_ready(*, settle=True):
    owner, _, workspace, main, kwargs = await setup_task()
    accepted = await accept_task_command(**kwargs)
    lease = await reserve_run(accepted["execution_session_id"], owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (lease.session_id, lease.run_id, lease.generation)
    message = await create_assistant_message(lease.session_id, batch.messages[0].id,
        model_id="test/model", agent="build", user_id=owner, run_fence=fence)
    await save_part(TextPart(session_id=lease.session_id, message_id=message.id,
                             text="The report is saved. Browser verification is still untested."),
                    user_id=owner, is_new=True, run_fence=fence)
    message.finish = "stop"
    await update_message_info(message, user_id=owner, run_fence=fence)
    if settle:
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    return owner, workspace, main, accepted, lease, message


async def test_settlement_and_result_outbox_rollback_together(monkeypatch):
    owner, _, _, accepted, lease, message = await result_ready(settle=False)
    from assistant import results
    original = results.record_execution_result_locked

    async def crash_after_result(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated process failure before commit")

    monkeypatch.setattr(results, "record_execution_result_locked", crash_after_result)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, accepted["inbox_id"])).state == "claimed"
            assert (await db.get(AssistantTask, accepted["task_id"])).observed_state == "running"
            assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == accepted["task_id"])) == 0
            assert await db.scalar(select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == lease.session_id, AgentEvent.kind == "assistant.execution.completed")) == 0
        monkeypatch.setattr(results, "record_execution_result_locked", original)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
            assert result.consumed_inbox_ids == [accepted["inbox_id"]]
            assert result.run_id == lease.run_id and result.generation == lease.generation
            assert result.delivery_state == "pending" and result.processed_message_id is None
            assert (await db.get(AssistantTask, accepted["task_id"])).latest_result_id == result.id
    finally:
        await lease.release(session_status="idle")


async def test_outbox_survives_closed_engine_and_two_workers_deliver_once():
    owner, _, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        url = db.get_bind().url
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
        result_id = result.id
    # Recreate connections to model loss of every process-local queue/cache.
    await close_engine()
    init_engine(url.render_as_string(hide_password=False))
    first, second = await asyncio.gather(deliver_task_result(result_id), deliver_task_result(result_id))
    assert first == second and first["report_attempt"] == 1
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        report = await db.get(AgentInboxItem, first["inbox_id"])
        assert report.session_id == main.id and report.origin == "task_result" and report.delivery == "followup"
        assert report.origin_ref["execution_mode"] == "report_only"
        assert result.delivery_state == "accepted" and result.processed_message_id is None
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
            AgentInboxItem.user_id == owner, AgentInboxItem.session_id == main.id)) == 1


async def test_maintenance_recovery_keeps_actual_terminal_identity():
    _, _, _, accepted, lease, message = await result_ready(settle=False)
    original_run, original_generation = lease.run_id, lease.generation
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        row = await db.get(AgentInboxItem, accepted["inbox_id"])
        row.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert await inbox.settle_orphaned_claims() >= 1
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
        assert result.run_id == original_run and result.generation == original_generation
        assert result.settlement_fence["run_id"] != original_run
        assert result.result_message_id == message.id and result.outcome == "succeeded"
    await inbox.settle_orphaned_claims()
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == accepted["task_id"])) == 1


@pytest.mark.parametrize("change", ["revoke", "source"])
async def test_delivery_blocks_revoked_or_changed_result_sources(change):
    owner, workspace, _, accepted, lease, message = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
        if change == "revoke":
            (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
        else:
            part = await db.scalar(select(Part).where(Part.message_id == message.id))
            part.data = {**part.data, "text": "Changed evidence"}
    assert await deliver_task_result(result_id) is None
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "blocked"
        assert result.assistant_inbox_id is None and result.processed_message_id is None
