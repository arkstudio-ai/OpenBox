"""Continuation authority, interruption, replay and rollback on real SQL."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.commands import accept_task_command, command_digest
from assistant.continuation import enqueue, next_step, recover_continuations
from assistant.control import accept_control_command
from assistant.policy import AssistantError
from assistant.reporting import read_result_sources, record_provider_report_reads
from assistant.scheduling import task_hold
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskSubmission
from db.models.part import Part
from models.message import ToolPartData, ToolStatus
from session.session import create_assistant_message, save_part
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reporting import answer, prepare_report, seen_read
from tool.assistant_tools import _read_descriptor
from tool.tool import ToolContext

QUOTE = "Keep working on this same text task until complete, with at most one followup."


async def ready(monkeypatch):
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args: None)
    values = await setup_task()
    kwargs = {**values[-1], "prompt": QUOTE,
              "continuation": {"authorization_quote": QUOTE, "max_followups": 1,
                  "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}}

    async def bounded_task():
        return (*values[:-1], kwargs)

    monkeypatch.setattr("tests.unit.test_assistant_results.setup_task", bounded_task)
    ctx, lease, message, part, result_id, _ = await prepare_report()
    try:
        await seen_read(ctx, part, result_id=result_id)
        await answer(ctx, lease, message, part)
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.user_id == ctx.user_id))
    return values, task, result_id


async def coordinator(values, task, result_id, *, read=True):
    owner, _, workspace, main, _ = values
    receipt = await enqueue(task.id)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(main.id, batch.messages[0].id,
        model_id="test/model", agent="assistant", user_id=owner,
        run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
    if read:
        args = {"result_id": result_id, "detail": "full", "offset": 0, "max_chars": 8000, "source_version": None}
        part = await call_part(ctx, "results.read", args)
        page = await read_result_sources(user_id=owner, workspace_id=workspace, main_id=main.id,
            result_id=result_id, ctx=ctx)
        part.status, part.output = ToolStatus.COMPLETED, json.dumps(page)
        part.metadata = {"transient_assistant_refs": _read_descriptor("results.read", args, page, ctx)}
        await save_part(part, user_id=owner, run_fence=ctx.run_fence)
        messages = [{"role": "tool", "tool_call_id": part.call_id, "content": part.output}]
        await consume_context(ctx, messages=messages)
        await record_provider_report_reads(ctx, messages)
    else:
        await consume_context(ctx)
    return ctx, lease, receipt


async def call_part(ctx, operation, request):
    part = ToolPartData(tool=operation, canonical_tool_id=operation, call_id="call-" + command_digest(request)[:12],
        wire_tool_name=operation.replace(".", "_"), provider_binding_digest="b" * 64, provider_dialect="openai",
        stream_seq=0, status=ToolStatus.RUNNING, input=request, session_id=ctx.session_id, message_id=ctx.message_id)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    ctx.part_id = part.id
    return part


async def test_recovery_race_reopen_and_original_authority_survive(monkeypatch):
    values, task, _ = await ready(monkeypatch)
    async with get_db_session() as db:
        url = db.get_bind().url
    await close_engine()
    init_engine(url)
    first, second = await asyncio.gather(enqueue(task.id), enqueue(task.id))
    assert bool(first) != bool(second)
    async with get_db_session() as db:
        current = await db.get(AssistantTask, task.id)
        item = await db.get(AgentInboxItem, current.continuation_policy["last_inbox_id"])
        assert item.origin == "system_recovery" and item.origin_ref["execution_mode"] == "coordination"
        assert current.continuation_policy["grant_command_id"] == task.continuation_policy["grant_command_id"]
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1


@pytest.mark.parametrize("decision", ["continue", "complete", "needs_decision"])
async def test_next_step_requires_result_bytes_observed_by_provider(monkeypatch, decision):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id, read=False)
    request = {"decision": decision, **({"instructions": "Finish the original report."} if decision == "continue" else {})}
    try:
        await call_part(ctx, "tasks.next_step", request)
        with pytest.raises(AssistantError) as denied:
            await next_step(ctx, request)
        assert denied.value.code == "ASSISTANT_CONTINUATION_READ_REQUIRED"
    finally:
        await lease.release(session_status="idle")


async def test_only_a_completed_original_receipt_can_end_coordination(monkeypatch):
    from assistant.continuation import terminal_decision
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    request = {"decision": "continue", "instructions": "Finish the original report."}
    try:
        part = await call_part(ctx, "tasks.next_step", request)
        assert not await terminal_decision(ctx)
        receipt = await next_step(ctx, request)
        assert not await terminal_decision(ctx)  # A still-running tool has no completed receipt.
        part.status, part.output = ToolStatus.COMPLETED, json.dumps(receipt)
        await save_part(part, user_id=ctx.user_id, run_fence=ctx.run_fence)
        assert await terminal_decision(ctx)
        await accept_control_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, task_id=task.id, idempotency_key="pause-before-terminal", action="pause",
            expected_revision=receipt["task_revision"])
        assert not await terminal_decision(ctx)
    finally:
        await lease.release(session_status="idle")


async def test_only_one_next_step_receipt_survives_response_loss_and_conflicting_retry(monkeypatch):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    request = {"decision": "continue", "instructions": "Finish the original report."}
    try:
        await call_part(ctx, "tasks.next_step", request)
        one, two = await asyncio.gather(next_step(ctx, request), next_step(ctx, request))
        assert one == two
        await call_part(ctx, "tasks.next_step", request)  # A new Part cannot duplicate a committed decision.
        assert await next_step(ctx, request) == one
        with pytest.raises(AssistantError) as denied:
            await next_step(ctx, {"decision": "complete"})
        assert denied.value.code == "ASSISTANT_COMMAND_CONFLICT"
        async with get_db_session() as db:
            current = await db.get(AssistantTask, task.id)
            assert current.continuation_policy["followups_used"] == 1
            assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 2
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("altered", [
    {"decision": "continue", "instructions": "An altered next step."},
    {"decision": "complete"},
    {"decision": "needs_decision"},
])
async def test_next_step_cannot_replace_its_persisted_decision_or_instructions(monkeypatch, altered):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    try:
        await call_part(ctx, "tasks.next_step", {"decision": "continue", "instructions": "Finish the original report."})
        with pytest.raises(AssistantError) as denied:
            await next_step(ctx, altered)
        assert denied.value.code == "ASSISTANT_CALL_UNVERIFIED"
        async with get_db_session() as db:
            current = await db.get(AssistantTask, task.id)
            assert current.continuation_policy["state"] == "active"
            assert current.continuation_policy["followups_used"] == 0
            assert current.continuation_policy["last_receipt"] is None
            assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_task_control_cancels_only_original_coordination_and_resume_refreshes_revision(monkeypatch, action):
    values, task, _ = await ready(monkeypatch)
    owner, _, workspace, main, _ = values
    queued = await enqueue(task.id)
    receipt = await accept_control_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        task_id=task.id, idempotency_key="control", action=action, expected_revision=task.control_revision)
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, queued["inbox_id"])).state == "canceled"
        assert (await db.get(AssistantTask, task.id)).continuation_policy["state"] == ("active" if action == "pause" else "revoked")
    assert await enqueue(task.id) is None
    if action == "pause":
        resumed = await accept_control_command(user_id=owner, workspace_id=workspace, main_id=main.id,
            task_id=task.id, idempotency_key="resume", action="resume", expected_revision=receipt["task_revision"])
        fresh = await enqueue(task.id)
        assert fresh["inbox_id"] != queued["inbox_id"]
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, fresh["inbox_id"])).origin_ref["expected_revision"] == resumed["task_revision"]


async def test_lost_coordination_does_not_automatically_execute_or_retry(monkeypatch):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="error")
    await lease.release(session_status="idle")
    assert await recover_continuations() == 0
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, task.id)).continuation_policy["state"] == "needs_decision"
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1
    paused = await accept_control_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
        main_id=ctx.session_id, task_id=task.id, idempotency_key="pause-interrupted", action="pause",
        expected_revision=task.control_revision)
    await accept_control_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
        main_id=ctx.session_id, task_id=task.id, idempotency_key="resume-interrupted", action="resume",
        expected_revision=paused["task_revision"])
    fresh = await enqueue(task.id)
    assert fresh is not None
    async with get_db_session() as db:
        current = await db.get(AssistantTask, task.id)
        assert current.continuation_policy["state"] == "active"
        assert current.continuation_policy["followups_used"] == 0
        assert current.continuation_policy["grant_command_id"] == task.continuation_policy["grant_command_id"]
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1


@pytest.mark.parametrize("case", ["expired", "human_decision"])
async def test_resume_cannot_restore_expired_authority_or_a_new_decision(monkeypatch, case):
    from assistant import continuation
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    if case == "human_decision":
        await call_part(ctx, "tasks.next_step", {"decision": "needs_decision"})
        await next_step(ctx, {"decision": "needs_decision"})
    await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="error")
    await lease.release(session_status="idle")
    await recover_continuations()
    async with get_db_session() as db:
        current = await db.get(AssistantTask, task.id)
        revision = current.control_revision
    paused = await accept_control_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
        main_id=ctx.session_id, task_id=task.id, idempotency_key="pause", action="pause", expected_revision=revision)
    if case == "expired":
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=2)
        monkeypatch.setattr(continuation, "datetime", Later)
        with pytest.raises(AssistantError) as rejected:
            await accept_control_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                main_id=ctx.session_id, task_id=task.id, idempotency_key="resume", action="resume",
                expected_revision=paused["task_revision"])
        assert rejected.value.code == "ASSISTANT_CONTINUATION_EXPIRED"
    else:
        await accept_control_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, task_id=task.id, idempotency_key="resume", action="resume",
            expected_revision=paused["task_revision"])
    assert await enqueue(task.id) is None
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, task.id)).continuation_policy["state"] == "needs_decision"
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1


async def test_new_human_input_cancels_queued_auto_input_and_replaces_authority(monkeypatch):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    request = {"decision": "continue", "instructions": "Finish the original report."}
    try:
        await call_part(ctx, "tasks.next_step", request)
        accepted = await next_step(ctx, request)
    finally:
        await lease.release(session_status="idle")
    human = await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
        task_id=task.id, idempotency_key="new-human", prompt="Only acknowledge this new request.",
        expected_revision=accepted["task_revision"])
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, accepted["inbox_id"])).state == "canceled"
        assert (await db.get(AgentInboxItem, human["inbox_id"])).state == "accepted"
        assert (await db.get(AssistantTask, task.id)).continuation_policy["state"] == "revoked"
    assert await task_hold(task.execution_session_id, ctx.user_id) is None


async def test_source_change_between_read_and_decision_blocks_new_execution(monkeypatch):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    request = {"decision": "continue", "instructions": "Finish the original report."}
    try:
        await call_part(ctx, "tasks.next_step", request)
        async with get_db_session() as db:
            original = await db.get(AssistantCommand, task.continuation_policy["grant_command_id"])
            item = await db.get(AgentInboxItem, original.source_ref["continuation_grant"]["inbox_id"])
            part = await db.scalar(select(Part).where(Part.message_id == item.message_id, Part.type == "text"))
            part.data = {**part.data, "text": "Changed original instructions"}
        with pytest.raises(AssistantError):
            await next_step(ctx, request)
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1
    finally:
        await lease.release(session_status="idle")


async def test_expiry_stops_an_already_accepted_automatic_input_but_keeps_original_results(monkeypatch):
    from assistant import continuation
    from assistant.results import validate_result_source
    from db.models.assistant import TaskResult
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    request = {"decision": "continue", "instructions": "Finish the original report."}
    try:
        await call_part(ctx, "tasks.next_step", request)
        await next_step(ctx, request)
    finally:
        await lease.release(session_status="idle")

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr(continuation, "datetime", Later)
    assert (await task_hold(task.execution_session_id, ctx.user_id)).state == "unavailable"
    assert await enqueue(task.id) is None
    async with get_db_session() as db:
        current = await db.get(AssistantTask, task.id)
        assert current.continuation_policy["state"] == "needs_decision"
        assert current.continuation_policy["reason"] == "ASSISTANT_CONTINUATION_EXPIRED"
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
            AgentInboxItem.session_id == task.execution_session_id, AgentInboxItem.state == "accepted")) == 0
        await validate_result_source(db, await db.get(TaskResult, result_id), user_id=ctx.user_id,
            workspace_id=ctx.workspace_id, main_id=ctx.session_id)


async def test_next_step_transaction_rollback_preserves_budget_and_does_not_queue_input(monkeypatch):
    from assistant import commands
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    request = {"decision": "continue", "instructions": "Finish the original report."}
    original = commands.append_agent_event_locked

    async def crash(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs.get("kind") == "assistant.submission.accepted":
            raise RuntimeError("continuation transaction crash")
        return result

    try:
        await call_part(ctx, "tasks.next_step", request)
        monkeypatch.setattr(commands, "append_agent_event_locked", crash)
        with pytest.raises(RuntimeError, match="continuation transaction crash"):
            await next_step(ctx, request)
        async with get_db_session() as db:
            current = await db.get(AssistantTask, task.id)
            assert current.continuation_policy["followups_used"] == 0
            assert current.continuation_policy["last_receipt"] is None
            assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1
        monkeypatch.setattr(commands, "append_agent_event_locked", original)
        assert (await next_step(ctx, request))["state"] == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_pause_interrupts_only_the_exact_main_coordinator(monkeypatch):
    from db.models.agent_driver import AgentDriverState
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    try:
        await accept_control_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, task_id=task.id, idempotency_key="pause-running-coordinator",
            action="pause", expected_revision=task.control_revision)
        async with get_db_session() as db:
            driver = await db.get(AgentDriverState, ctx.session_id)
            assert driver.run_id == lease.run_id and driver.generation == lease.generation
            assert driver.abort_requested_at is not None
        request = {"decision": "continue", "instructions": "Finish the report."}
        with pytest.raises(AssistantError):
            await next_step(ctx, request)
    finally:
        await lease.release(session_status="idle")


async def test_coordinator_history_cannot_read_unrelated_main_messages(monkeypatch):
    from assistant.history import read_history
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    try:
        with pytest.raises(AssistantError) as denied:
            await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
                session_id=ctx.session_id, ctx=ctx)
        assert denied.value.code == "ASSISTANT_REPORT_SCOPE"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("quote,expiry", [("Unmatched original text", None),
    (QUOTE, (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())])
async def test_invalid_original_grant_rolls_back_all_task_records(quote, expiry):
    values = await setup_task()
    with pytest.raises(AssistantError):
        await accept_task_command(**{**values[-1], "prompt": QUOTE},
            continuation={"authorization_quote": quote, "expires_at": expiry})
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(AssistantTask.user_id == values[0])) == 0
