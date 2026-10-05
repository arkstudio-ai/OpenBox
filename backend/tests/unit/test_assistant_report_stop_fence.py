"""Explicit report stop is atomic with ticket revocation and refuses late writers."""
import pytest
from sqlalchemy import func, select

from agent.driver import request_abort
from assistant.results import deliver_task_result
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.message import Message
from db.models.part import Part
from question import runtime
from session.agent_event_log import verify_agent_event_parity
from session.session import update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reporting import prepare_report


async def stopped_state(ctx, result_id, delivered):
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        item = await db.get(AgentInboxItem, delivered["inbox_id"])
        return {"delivery": result.delivery_state, "reason": result.last_error_code,
            "attempt": result.report_attempt, "processed": result.processed_message_id,
            "inbox": item.state, "outcome": item.outcome,
            "failed": await db.scalar(select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == ctx.session_id, AgentEvent.kind == "assistant.report.failed"))}


async def test_stop_closes_original_report_and_revokes_late_provider_writes(monkeypatch):
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    ticket = await runtime.start_run(ctx.session_id, ctx.user_id, driver_lease=lease)
    try:
        assert await request_abort(ctx.session_id, ctx.user_id,
            expected_run_id=lease.run_id, expected_generation=lease.generation)
        await runtime.cancel_session(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id)
        await runtime.cancel_session(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id)
        assert await stopped_state(ctx, result_id, delivered) == {
            "delivery": "blocked", "reason": "user_stopped", "attempt": 1,
            "processed": None, "inbox": "settled", "outcome": "aborted", "failed": 1}
        # The Driver has not released yet: SQL ticket revocation itself must
        # stop a late completion, not just an in-process abort Event.
        # A different worker has no local revocation cache.
        monkeypatch.setattr(runtime, "_revoked", runtime._Recent())
        token = runtime.current_run.set(ticket)
        try:
            message.finish = "stop"
            with pytest.raises(runtime.RunRevoked):
                await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
        finally:
            runtime.current_run.reset(token)
        async with get_db_session() as db:
            assert (await db.get(Message, message.id)).finish == "aborted"
            tool = await db.get(Part, part.id)
            assert tool.data["status"] == "error" and tool.data["metadata"]["execution_outcome"] == "unknown"
        assert await deliver_task_result(result_id) is None
        assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
    finally:
        await lease.release(session_status="idle")


async def test_report_stop_and_question_revocation_rollback_together(monkeypatch):
    ctx, lease, message, _, result_id, delivered = await prepare_report()
    ticket = await runtime.start_run(ctx.session_id, ctx.user_id, driver_lease=lease)
    original = runtime.invalidate_locked

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("crash before revocation commit")

    try:
        assert await request_abort(ctx.session_id, ctx.user_id,
            expected_run_id=lease.run_id, expected_generation=lease.generation)
        monkeypatch.setattr(runtime, "invalidate_locked", crash)
        with pytest.raises(RuntimeError, match="before revocation commit"):
            await runtime.cancel_session(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id)
        assert await stopped_state(ctx, result_id, delivered) == {
            "delivery": "accepted", "reason": None, "attempt": 1,
            "processed": None, "inbox": "claimed", "outcome": None, "failed": 0}
        async with get_db_session() as db:
            assert (await db.get(Message, message.id)).finish is None
        token = runtime.current_run.set(ticket)
        try:
            await runtime.assert_current("rollback-survivor")
        finally:
            runtime.current_run.reset(token)
        monkeypatch.setattr(runtime, "invalidate_locked", original)
        await runtime.cancel_session(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id)
        assert (await stopped_state(ctx, result_id, delivered))["reason"] == "user_stopped"
    finally:
        await lease.release(session_status="idle")


async def test_stale_stop_cannot_change_the_current_report():
    ctx, lease, _, _, result_id, delivered = await prepare_report()
    await runtime.start_run(ctx.session_id, ctx.user_id, driver_lease=lease)
    try:
        assert not await request_abort(ctx.session_id, ctx.user_id,
            expected_run_id="older-run", expected_generation=lease.generation)
        await runtime.cancel_session(ctx.session_id, ctx.user_id, expected_run_id="older-run")
        assert (await stopped_state(ctx, result_id, delivered))["delivery"] == "accepted"
        async with runtime.transaction(ctx.session_id, ctx.user_id, fence=False) as (db, main, _):
            from assistant.report_stop import stop_report_locked
            await stop_report_locked(db, main, expected_run_id=lease.run_id)
        assert (await stopped_state(ctx, result_id, delivered))["delivery"] == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_stop_after_claim_before_first_provider_message_records_an_aborted_report():
    from agent import inbox
    from agent.driver import reserve_run
    from tests.unit.test_assistant_results import result_ready
    owner, _, main, accepted, execution, _ = await result_ready()
    await execution.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    delivered = await deliver_task_result(result_id)
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        await runtime.start_run(main.id, owner, driver_lease=lease)
        assert await request_abort(main.id, owner, expected_run_id=lease.run_id, expected_generation=lease.generation)
        await runtime.cancel_session(main.id, owner, expected_run_id=lease.run_id)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            item = await db.get(AgentInboxItem, delivered["inbox_id"])
            answer = await db.get(Message, item.result_message_id)
            assert result.delivery_state == "blocked" and result.last_error_code == "user_stopped"
            assert item.state == "settled" and item.outcome == answer.finish == "aborted"
            assert answer.parent_id == batch.messages[0].id
            assert await db.scalar(select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == main.id, AgentEvent.kind == "model.requested")) == 0
        assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    finally:
        await lease.release(session_status="idle")
