"""A human stop wins after an interrupted report settles but before release."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import request_abort
from assistant.delivery import reconcile_report, recover_assistant_results
from assistant.results import deliver_task_result
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.message import Message
from question import runtime
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reporting import answer, prepare_report, seen_read


@pytest.mark.parametrize("phase", ["finalized", "settled"])
async def test_human_stop_after_system_settlement_blocks_the_same_attempt(record_property, phase):
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    await runtime.start_run(ctx.session_id, ctx.user_id, driver_lease=lease)
    try:
        if phase == "finalized":
            await answer(ctx, lease, message, part, finish="aborted")
        else:
            await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="aborted")
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == ("retry_wait" if phase == "finalized" else "accepted")
            assert (await db.get(AgentInboxItem, delivered["inbox_id"])).state == "settled"
            url = db.get_bind().url.render_as_string(hide_password=False)
        # The terminal transaction committed, but this exact Driver still
        # owns its live lease. A stop received now must not disappear.
        assert await request_abort(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id,
                                   expected_generation=lease.generation, reason="user_stop")
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert (result.delivery_state, result.last_error_code) == ("blocked", "user_stopped")
            assert result.assistant_inbox_id == delivered["inbox_id"] and result.report_attempt == 1
            assert result.processed_message_id is None
            result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert (await db.get(Message, message.id)).finish == "aborted"
        # Repeated UI cleanup is harmless and preserves the stop receipt.
        await runtime.cancel_session(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id)
    finally:
        await lease.release(session_status="idle")
    await close_engine()
    init_engine(url)
    assert not await reconcile_report(result_id)
    assert await recover_assistant_results(result_ids=(result_id,)) == 0
    assert await deliver_task_result(result_id) is None
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        item = await db.get(AgentInboxItem, delivered["inbox_id"])
        stops = list((await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == ctx.session_id, AgentEvent.kind == "assistant.report.failed",
        ))).all())
        assert len([event for event in stops if event.payload.get("reason") == "user_stopped"]) == 1
        assert result.delivery_state == "blocked" and result.last_error_code == "user_stopped"
        assert item.state == "settled" and item.outcome == "aborted"
    assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
    record_property("assistant_acceptance", json.dumps({"phase": phase,
        "result_id": result_id, "inbox_id": delivered["inbox_id"], "run_id": lease.run_id,
        "generation": lease.generation, "attempt": 1, "state": "blocked", "reason": "user_stopped",
        "reopen_recovery_created_attempts": 0}))


@pytest.mark.parametrize("state", ["processed", "replaced"])
async def test_stop_cannot_relabel_success_or_a_replacement_report(state):
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    await runtime.start_run(ctx.session_id, ctx.user_id, driver_lease=lease)
    try:
        if state == "processed":
            await seen_read(ctx, part, result_id=result_id)
            await answer(ctx, lease, message, part)
        else:
            await answer(ctx, lease, message, part, finish="aborted")
            async with get_db_session() as db:
                result = await db.get(TaskResult, result_id)
                result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            replacement = await deliver_task_result(result_id)
            assert replacement["report_attempt"] == 2
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            before = {column.name: getattr(result, column.name) for column in result.__table__.columns}
        assert await request_abort(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id,
                                   expected_generation=lease.generation, reason="user_stop")
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert {column.name: getattr(result, column.name) for column in result.__table__.columns} == before
            if state == "processed":
                assert (await db.get(Message, message.id)).finish == "stop"
            else:
                assert (await db.get(AgentInboxItem, replacement["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")
