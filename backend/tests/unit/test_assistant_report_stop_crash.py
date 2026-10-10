"""A committed explicit stop is never lost between request and API cleanup."""
from dataclasses import asdict
import json

import pytest
from sqlalchemy import select

from agent import inbox, loop, processor
from agent.driver import request_abort, reserve_run
from agent.recovery_service import AgentRecoveryService
from assistant.results import deliver_task_result
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from question import runtime
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready
from tests.unit.test_assistant_reporting import prepare_report
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts


@pytest.mark.parametrize("phase", ["reserved", "running"])
async def test_committed_stop_survives_loss_before_cancel_wait_or_marker(monkeypatch, record_property, phase):
    config = _loop_config()
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    calls = []
    published = []
    real_publish_status = runtime.publish_status

    async def publish_status(*args, **kwargs):
        await real_publish_status(*args, **kwargs)
        published.append(args)

    monkeypatch.setattr(runtime, "publish_status", publish_status)

    async def no_external(*args, **kwargs):
        return None

    async def provider(**kwargs):
        calls.append(kwargs["ctx"].session_id)
        yield {"type": "text_delta", "text": "An explicitly stopped report must not call this provider."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(loop, "_ensure_title", no_external)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_external)
    monkeypatch.setattr(processor, "stream_llm", provider)
    owner, _, main, accepted, execution, _ = await result_ready()
    await execution.release(session_status="idle")
    facts = await _execution_facts(accepted["task_id"])
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
        url = db.get_bind().url.render_as_string(hide_password=False)
    delivered = await deliver_task_result(result_id)
    lease = await reserve_run(main.id, owner)
    await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    if phase == "running":
        await runtime.start_run(main.id, owner, driver_lease=lease)
        await lease.set_phase("running")
    assert await request_abort(main.id, owner, expected_run_id=lease.run_id,
                               expected_generation=lease.generation, reason="user_stop")
    assert lease.abort.is_set() and (main.id, owner, "idle") in published
    # No cancel_session(), wait, marker or normal release follows this commit.
    # Preserve the same expired SQL identity, then lose connections and service
    # instances exactly as the existing production recovery contract requires.
    await lease.preserve_for_recovery(session_status="idle")
    await close_engine()
    init_engine(url)
    service = AgentRecoveryService(interval_seconds=3600)
    try:
        recovered = await service.start()
        assert recovered is not None
        assert recovered.resumed_inbox_sessions == 0
        assert await deliver_task_result(result_id) is None
        repeated = await service.run_once()
        assert repeated.assistant_results_recovered == repeated.resumed_inbox_sessions == 0
        assert not calls
        assert await _execution_facts(accepted["task_id"]) == facts
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            item = await db.get(AgentInboxItem, delivered["inbox_id"])
            assert result.delivery_state == "blocked" and result.last_error_code == "user_stopped"
            assert item.state == "settled" and item.outcome == "aborted"
            assert (await db.get(AgentDriverState, main.id)).phase == "idle"
        assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
        record_property("assistant_acceptance", json.dumps({"phase": phase, "task_id": accepted["task_id"],
            "main_id": main.id, "run_id": lease.run_id, "generation": lease.generation,
            "result_id": result_id, "inbox_id": delivered["inbox_id"], "report_attempt": 1,
            "original_execution_unchanged": facts, "recovered": asdict(recovered), "provider_calls": calls}))
    finally:
        await service.stop()


async def test_failed_stop_transaction_does_not_leave_an_abort_intent_or_partial_report(monkeypatch):
    ctx, lease, _, _, result_id, delivered = await prepare_report()
    await runtime.start_run(ctx.session_id, ctx.user_id, driver_lease=lease)
    original = runtime.invalidate_locked

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("stop transaction interrupted")

    monkeypatch.setattr(runtime, "invalidate_locked", crash)
    try:
        with pytest.raises(RuntimeError, match="stop transaction interrupted"):
            await request_abort(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id,
                                expected_generation=lease.generation, reason="user_stop")
        async with get_db_session() as db:
            assert (await db.get(AgentDriverState, ctx.session_id)).abort_requested_at is None
            assert (await db.get(TaskResult, result_id)).delivery_state == "accepted"
            assert (await db.get(AgentInboxItem, delivered["inbox_id"])).state == "claimed"
    finally:
        await lease.release(session_status="idle")
