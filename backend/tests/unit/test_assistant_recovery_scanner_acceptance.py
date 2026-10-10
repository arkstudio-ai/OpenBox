"""The real recovery service closes result-commit and lost-wake gaps."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from agent.recovery_service import AgentRecoveryService
from assistant.commands import accept_task_command
from assistant.reporting import REPORT_TOOLS
from assistant.results import deliver_task_result
from assistant.service import ensure_main_session
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.message import Message
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts
from tool.assistant_tools import assistant_tools


@pytest.mark.parametrize("gap", ["initial_result", "failed_report"])
async def test_new_recovery_service_delivers_and_drives_report_after_lost_wake(monkeypatch, record_property, gap):
    config = _loop_config()
    config.permission = {"*": "allow"}
    history_names = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", history_names)

    async def no_background_provider(*args, **kwargs):
        return None

    monkeypatch.setattr(loop, "_ensure_title", no_background_provider)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_background_provider)

    async def tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools} if agent.name == "assistant" else {},
                               catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    previous_service = AgentRecoveryService(interval_seconds=3600)
    initial = await previous_service.start()
    assert initial is not None and initial.resumed_inbox_sessions == 0
    phase, calls, result_id = "execution", [], None

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        calls.append((phase, ctx.session_id))
        count = sum(name == phase for name, _ in calls)
        if phase == "execution":
            yield {"type": "text_delta", "text": "Scanner acceptance: 7 + 8 = 15. No browser tests were run."}
        else:
            assert ctx.session_id == main.id and ctx.sandbox is None
            assert {tool.id for tool in kwargs["tools"].values()} == REPORT_TOOLS
            if count == 1:
                wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "results.read")
                yield {"type": "tool_call", "tool": wire, "args": {"result_id": result_id},
                       "call_id": f"{phase}-original-result", "invalid": False}
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            payload = json.dumps(kwargs["messages"])
            assert "Compute 7 + 8" in payload and "No browser tests were run" in payload
            if phase == "first_report":
                yield {"type": "text_delta", "text": "Interrupted original report."}
                raise RuntimeError("report provider interrupted before recovery")
            yield {"type": "text_delta", "text": "The original task reports 15; no browser tests were run."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)

    async def run_explicitly(session_id):
        lease = await reserve_run(session_id, owner)
        try:
            await loop.run_loop(session_id, user_id=owner, lease=lease)
        finally:
            await lease.release(session_status="idle")
        return lease

    try:
        task = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
            project_id=main.project_id, idempotency_key="scanner-task", prompt="Compute 7 + 8 using text only.")
        execution = await run_explicitly(task["execution_session_id"])
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task["task_id"]))
            assert result.delivery_state == "pending" and result.report_attempt == 1
            result_id, original_message = result.id, result.result_message_id
            url = db.get_bind().url.render_as_string(hide_password=False)
        if gap == "failed_report":
            assert (await deliver_task_result(result_id))["report_attempt"] == 1
            phase = "first_report"
            await run_explicitly(main.id)
            async with get_db_session() as db:
                result = await db.get(TaskResult, result_id)
                assert result.delivery_state == "retry_wait" and result.processed_message_id is None
                # Only this disposable fixture's retry clock is advanced.
                result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        original_facts = await _execution_facts(task["task_id"])
    finally:
        await previous_service.stop()
    await close_engine()
    init_engine(url)

    lost_wakes, dispatched = [], []
    completed = asyncio.Event()
    drive = inbox._drive_claimed

    def lose_first_wake(session_id, user_id):
        lost_wakes.append((session_id, user_id))
        if len(lost_wakes) == 1:
            raise RuntimeError("wake was lost after durable report acceptance")

    async def observe_actual_drive(lease, batch):
        dispatched.append({"session_id": lease.session_id, "run_id": lease.run_id,
                           "generation": lease.generation, "inbox_ids": [item.id for item in batch.receipts]})
        try:
            await drive(lease, batch)
        finally:
            completed.set()

    monkeypatch.setattr(inbox, "schedule_inbox_wake", lose_first_wake)
    monkeypatch.setattr(inbox, "_drive_claimed", observe_actual_drive)
    phase = "recovered_report"
    recovery = AgentRecoveryService(interval_seconds=3600)
    try:
        recovered = await recovery.start()
        assert recovered is not None and recovered.assistant_results_recovered == 1
        assert recovered.resumed_inbox_sessions == 1
        await asyncio.wait_for(completed.wait(), timeout=15)
        # A repeat startup/periodic pass must not re-execute or re-report.
        repeated = await recovery.run_once()
        assert repeated.assistant_results_recovered == repeated.resumed_inbox_sessions == 0
    finally:
        await recovery.stop()
        pending = inbox._wake_tasks.get((owner, main.id))
        if pending is not None:
            await asyncio.wait_for(asyncio.shield(pending), timeout=15)
    assert lost_wakes[0] == (main.id, owner)
    assert len(dispatched) == 1 and dispatched[0]["session_id"] == main.id
    assert sum(name == "execution" for name, _ in calls) == 1
    assert sum(name == "recovered_report" for name, _ in calls) == 2
    assert await _execution_facts(task["task_id"]) == original_facts
    await close_engine()
    init_engine(url)
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        expected_attempt = 1 if gap == "initial_result" else 2
        assert result.delivery_state == "processed" and result.report_attempt == expected_attempt
        assert result.result_message_id == original_message
        assert (result.run_id, result.generation) == (execution.run_id, execution.generation)
        assert result.assistant_inbox_id == dispatched[0]["inbox_ids"][0]
        report_inbox = await db.get(AgentInboxItem, result.assistant_inbox_id)
        assert report_inbox.state == "settled" and report_inbox.outcome == "succeeded"
        assert report_inbox.result_message_id == result.processed_message_id
        assert (await db.get(AgentDriverState, main.id)).phase == "idle"
        assert (await db.get(Message, result.processed_message_id)).finish == "stop"
        counts = {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("results", TaskResult, TaskResult.task_id == task["task_id"]),
                ("report_inbox", AgentInboxItem, (AgentInboxItem.session_id == main.id)
                    & (AgentInboxItem.origin == "task_result")),
                ("processed_events", AgentEvent, (AgentEvent.session_id == main.id)
                    & (AgentEvent.kind == "assistant.result.processed")),
            )}
        assert counts == {"results": 1, "report_inbox": expected_attempt, "processed_events": 1}
        witness = {"gap": gap, "task_id": task["task_id"], "execution_session_id": task["execution_session_id"],
            "execution_run": execution.run_id, "execution_generation": execution.generation,
            "result_id": result_id, "report_attempt": result.report_attempt,
            "processed_message_id": result.processed_message_id, "recovery_pass": asdict(recovered),
            "scanner_dispatch": dispatched[0], "counts": counts, "execution_unchanged": original_facts,
            "lost_wake": True, "provider_calls": calls}
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    assert (await verify_agent_event_parity(task["execution_session_id"], user_id=owner)).ok
    record_property("assistant_acceptance", json.dumps(witness))
