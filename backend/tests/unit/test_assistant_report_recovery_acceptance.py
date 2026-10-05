"""Real report retries keep the original execution and fence delayed workers."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import LeaseLostError, request_abort, reserve_run
from assistant.commands import accept_task_command, command_digest
from assistant.delivery import reconcile_report, recover_assistant_results
from assistant.evidence import validate_message_sources
from assistant.policy import AssistantError
from assistant.reporting import REPORT_TOOLS, bound_report_locked
from assistant.results import deliver_task_result
from assistant.service import ensure_main_session
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.message import Message
from db.models.session import Session
from models.message import MessageInfo
from session.agent_event_log import verify_agent_event_parity
from session.session import create_assistant_message, get_messages, update_message_info
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools


async def _execution_facts(task_id):
    """Every execution-side row must remain unchanged during report recovery."""
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task_id)
        rows = {"task": [task]}
        for name, model, predicate in (
            ("commands", AssistantCommand, AssistantCommand.target_id == task.id),
            ("submissions", TaskSubmission, TaskSubmission.task_id == task.id),
            ("execution_inbox", AgentInboxItem, AgentInboxItem.session_id == task.execution_session_id),
            ("execution_driver", AgentDriverState, AgentDriverState.session_id == task.execution_session_id),
            ("execution_events", AgentEvent, AgentEvent.session_id == task.execution_session_id),
        ):
            rows[name] = list((await db.scalars(select(model).where(predicate))).all())
        facts = {name: sorted([{column.name: getattr(row, column.name)
            for column in row.__table__.columns} for row in values], key=lambda row: str(row.get("id", "")))
            for name, values in rows.items()}
        safe = json.loads(json.dumps(facts, default=str))
        return {"counts": {name: len(values) for name, values in safe.items()}, "digest": command_digest(safe)}


@pytest.mark.parametrize("failure", ["provider_error", "system_abort", "settled_before_finalization", "settled_abort"])
async def test_report_recovery_and_delayed_worker_preserve_one_execution(monkeypatch, record_property, failure):
    config = _loop_config()
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)

    async def no_background_provider(*args, **kwargs):
        return None

    # Title/suggestion providers are unrelated to report recovery. Suppress
    # their external work before taking a complete execution-row snapshot.
    monkeypatch.setattr(loop, "_ensure_title", no_background_provider)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_background_provider)

    async def tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools} if agent.name == "assistant" else {},
                               catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    accepted = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        project_id=main.project_id, idempotency_key="report-recovery-task", prompt=(
            "Produce a pure text report. Preserve the limitation that browser tests were not run; "
            "do not create files or a commit."))
    phase, calls, result_id, first_message_id = "execution", [], None, None

    async def stream(**kwargs):
        nonlocal first_message_id
        ctx = kwargs["ctx"]
        calls.append((phase, ctx.session_id))
        count = sum(name == phase for name, _ in calls)
        if phase == "execution":
            assert ctx.session_id == accepted["execution_session_id"]
            yield {"type": "text_delta", "text": "The text report is complete. Browser tests were not run; no file or commit was created."}
        else:
            assert ctx.session_id == main.id and ctx.sandbox is None
            assert {tool.id for tool in kwargs["tools"].values()} == REPORT_TOOLS
            if phase == "first_report":
                first_message_id = ctx.message_id
            if count == 1:
                wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "results.read")
                yield {"type": "tool_call", "tool": wire, "args": {"result_id": result_id},
                       "call_id": f"{phase}-read-original", "invalid": False}
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            evidence = json.dumps(kwargs["messages"])
            assert "Preserve the limitation" in evidence and "Browser tests were not run" in evidence
            assert "no file or commit was created" in evidence
            if phase == "first_report":
                # A consumed partial response cannot be replayed as an LLM
                # transport retry. The actual processor must fail this report.
                yield {"type": "text_delta", "text": "Incomplete report response."}
                if failure == "system_abort":
                    # Recovery-service shutdown requests an exact cooperative
                    # abort without a human stop intent. It must remain retryable.
                    assert await request_abort(ctx.session_id, ctx.user_id,
                        expected_run_id=ctx.run_id, expected_generation=ctx.run_generation)
                    yield {"type": "finish", "reason": "aborted", "usage": {}}
                    return
                raise RuntimeError("deliberate report provider interruption")
            yield {"type": "text_delta", "text": (
                "The original execution reports completion. Browser tests were not run, "
                "and no file or commit was created; I have not independently verified those claims.")}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)

    async def run(session_id):
        lease = await reserve_run(session_id, owner)
        try:
            await loop.run_loop(session_id, user_id=owner, lease=lease)
        finally:
            await lease.release(session_status="idle")
        return lease

    execution_lease = await run(accepted["execution_session_id"])
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
        assert result.delivery_state == "pending" and result.outcome == "succeeded"
        result_id, original_message_id = result.id, result.result_message_id
        url = db.get_bind().url.render_as_string(hide_password=False)
    original_facts = await _execution_facts(accepted["task_id"])
    first = await deliver_task_result(result_id)
    if failure in {"provider_error", "system_abort"}:
        phase = "first_report"
        first_lease = await run(main.id)
        failed_message = MessageInfo.model_validate(next(
            message for message in await get_messages(main.id, user_id=owner)
            if message.id == first_message_id).model_dump())
    else:
        # Model a process stopping after the actual Inbox terminal commit but
        # before a final report receipt. Recovery must inspect durable SQL.
        first_lease = await reserve_run(main.id, owner)
        try:
            batch = await inbox.claim_inbox_boundary(first_lease, step=1, include_next_turn=True)
            failed_message = await create_assistant_message(main.id, batch.messages[0].id,
                model_id=config.model, agent="assistant", user_id=owner,
                run_fence=(main.id, first_lease.run_id, first_lease.generation))
            await inbox.settle_claimed_inbox_items(first_lease, result_message_id=None,
                outcome="aborted" if failure == "settled_abort" else "error")
        finally:
            await first_lease.release(session_status="idle")
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        first_inbox = await db.get(AgentInboxItem, first["inbox_id"])
        expected_outcome = "aborted" if failure in {"system_abort", "settled_abort"} else "error"
        assert first_inbox.state == "settled" and first_inbox.outcome == expected_outcome
        assert result.processed_message_id is None
        assert result.delivery_state == ("retry_wait" if failure in {"provider_error", "system_abort"} else "accepted")
        first_terminal = (await db.get(Message, failed_message.id)).finish
    await close_engine()
    init_engine(url)
    assert await reconcile_report(result_id) is (failure in {"settled_before_finalization", "settled_abort"})
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "retry_wait" and result.report_attempt == 1
        assert result.processed_message_id is None
        # Advance only this isolated fixture's due time; do not sleep or alter
        # the production retry policy to make the test finish quickly.
        result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    wakes = []

    def lost_wake(session_id, user_id):
        wakes.append((session_id, user_id))
        raise RuntimeError("deliberate post-commit wake loss")

    normal_wake = inbox.schedule_inbox_wake
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lost_wake)
    assert await recover_assistant_results(result_ids=(result_id,)) == 1
    assert wakes == [(main.id, owner)]
    monkeypatch.setattr(inbox, "schedule_inbox_wake", normal_wake)
    await close_engine()
    init_engine(url)
    second = await deliver_task_result(result_id)
    assert second["report_attempt"] == 2 and second["inbox_id"] != first["inbox_id"]
    assert await recover_assistant_results(result_ids=(result_id,)) == 0
    assert await _execution_facts(accepted["task_id"]) == original_facts

    retry_lease = await reserve_run(main.id, owner)
    assert retry_lease.generation > first_lease.generation
    try:
        # A delayed attempt-1 worker cannot relabel its old response as the
        # new attempt, even though the underlying TaskResult is the same.
        failed_message.finish, failed_message.error = "stop", None
        with pytest.raises(LeaseLostError):
            await update_message_info(failed_message, user_id=owner,
                run_fence=(main.id, first_lease.run_id, first_lease.generation))
        async with get_db_session() as db:
            main_row = await db.get(Session, main.id)
            with pytest.raises(AssistantError) as stale:
                await bound_report_locked(db, main_row, run_id=first_lease.run_id,
                                          generation=first_lease.generation, verify_sources=False)
            assert stale.value.code == "ASSISTANT_REPORT_STALE"
            retained = await db.get(TaskResult, result_id)
            assert retained.report_attempt == 2 and retained.assistant_inbox_id == second["inbox_id"]
            assert retained.delivery_state == "accepted" and retained.processed_message_id is None
            assert (await db.get(Message, failed_message.id)).finish == first_terminal
        phase = "retry_report"
        await loop.run_loop(main.id, user_id=owner, lease=retry_lease)
    finally:
        await retry_lease.release(session_status="idle")
    await close_engine()
    init_engine(url)
    assert await deliver_task_result(result_id) is None
    assert not await reconcile_report(result_id)
    assert await _execution_facts(accepted["task_id"]) == original_facts
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        second_inbox = await db.get(AgentInboxItem, second["inbox_id"])
        assert result.delivery_state == "processed" and result.report_attempt == 2
        assert result.assistant_inbox_id == second["inbox_id"] and result.retry_count == 1
        assert result.result_message_id == original_message_id
        assert (result.run_id, result.generation) == (execution_lease.run_id, execution_lease.generation)
        assert second_inbox.state == "settled" and second_inbox.outcome == "succeeded"
        assert second_inbox.result_message_id == result.processed_message_id
        assert (await db.get(AgentInboxItem, first["inbox_id"])).outcome == expected_outcome
        await validate_message_sources(db, await db.get(Message, result.processed_message_id),
            user_id=owner, workspace_id=workspace, main_id=main.id)
        counts = {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("results", TaskResult, TaskResult.task_id == accepted["task_id"]),
                ("report_inbox", AgentInboxItem, (AgentInboxItem.session_id == main.id)
                    & (AgentInboxItem.origin == "task_result")),
                ("processed_events", AgentEvent, (AgentEvent.session_id == main.id)
                    & (AgentEvent.kind == "assistant.result.processed")),
            )}
        assert counts == {"results": 1, "report_inbox": 2, "processed_events": 1}
        witness = {"scenarios": ["PA-11", "PA-14"], "failure": failure,
            "task_id": accepted["task_id"], "execution_session_id": accepted["execution_session_id"],
            "result_id": result_id, "execution_run": execution_lease.run_id,
            "execution_generation": execution_lease.generation,
            "report_receipts": [first, second], "processed_message_id": result.processed_message_id,
            "original_execution_unchanged": original_facts, "counts": counts,
            "provider_calls": calls, "lost_wake_recovered": True,
            "late_old_report_write_rejected": True}
    assert sum(name == "execution" for name, _ in calls) == 1
    assert sum(name == "retry_report" for name, _ in calls) == 2
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    assert (await verify_agent_event_parity(accepted["execution_session_id"], user_id=owner)).ok
    record_property("assistant_acceptance", json.dumps(witness))
