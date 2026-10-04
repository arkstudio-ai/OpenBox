"""Report receipts require real bounded reads, not model-written acknowledgments."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.policy import AssistantError
from assistant.reporting import read_report_sources, record_provider_report_reads
from assistant.results import deliver_task_result
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.message import Message
from db.models.part import Part
from models.message import TextPart, ToolPartData, ToolStatus
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready
from tool.tool import ToolContext
from tests.unit.assistant_source_fixtures import consume_context


async def prepare_report():
    owner, workspace, main, accepted, execution_lease, _ = await result_ready()
    await execution_lease.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    delivered = await deliver_task_result(result_id)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (main.id, lease.run_id, lease.generation)
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=owner, run_fence=fence)
    part = ToolPartData(tool="results.read", canonical_tool_id="results.read", call_id="report-read",
        wire_tool_name="results_read", provider_binding_digest="b" * 64, provider_dialect="openai",
        stream_seq=0, status=ToolStatus.RUNNING, input={"result_id": result_id},
        session_id=main.id, message_id=message.id)
    await save_part(part, is_new=True, user_id=owner, run_fence=fence)
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id,
        part_id=part.id)
    return ctx, lease, message, part, result_id, delivered


async def answer(ctx, lease, message, read_part, *, finish="stop"):
    fence = (ctx.session_id, lease.run_id, lease.generation)
    read_part.status = ToolStatus.COMPLETED
    read_part.output = "See the authorized read above."
    await save_part(read_part, user_id=ctx.user_id, run_fence=fence)
    await save_part(TextPart(session_id=ctx.session_id, message_id=message.id,
        text="The report was created. Browser verification remains untested."),
        is_new=True, user_id=ctx.user_id, run_fence=fence)
    message.finish = finish
    await update_message_info(message, user_id=ctx.user_id, run_fence=fence)


async def seen_read(ctx, part, **kwargs):
    """Model-facing page delivery; the real processor path has its own E2E test."""
    page = await read_report_sources(ctx=ctx, **kwargs)
    part.status, part.output = ToolStatus.COMPLETED, json.dumps(page)
    await save_part(part, user_id=ctx.user_id, run_fence=ctx.run_fence)
    messages = [{"role": "tool", "tool_call_id": part.call_id, "content": part.output}]
    await consume_context(ctx, messages=messages)
    await record_provider_report_reads(ctx, messages)
    return page


async def test_partial_read_is_not_processed_and_retry_only_creates_a_report():
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    try:
        page = await seen_read(ctx, part, result_id=result_id, max_chars=5)
        assert page["next_offset"] == 5
        await answer(ctx, lease, message, part)
        assert message.finish == "error"
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == "retry_wait" and result.processed_message_id is None
            assert result.last_error_code == "report_evidence_incomplete"
            assert (await db.get(AgentInboxItem, delivered["inbox_id"])).state == "settled"
            result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        retry = await deliver_task_result(result_id)
        assert retry["report_attempt"] == 2 and retry["inbox_id"] != delivered["inbox_id"]
        async with get_db_session() as db:
            second = await db.get(AgentInboxItem, retry["inbox_id"])
            assert second.session_id == ctx.session_id and second.origin_ref["execution_mode"] == "report_only"
    finally:
        await lease.release(session_status="idle")


async def test_actual_complete_reads_and_answer_commit_one_processed_receipt():
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    try:
        page = await seen_read(ctx, part, result_id=result_id, max_chars=7)
        kinds = {row["kind"] for row in page["sources"]}
        while page["next_offset"] is not None:
            page = await seen_read(ctx, part, result_id=result_id, max_chars=7,
                offset=page["next_offset"], source_version=page["source_version"])
            kinds.update(row["kind"] for row in page["sources"])
        assert kinds == {"request", "report"}
        await answer(ctx, lease, message, part)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == "processed" and result.processed_message_id == message.id
            assert (await db.get(Message, message.id)).finish == "stop"
            accepted = await db.get(AgentInboxItem, delivered["inbox_id"])
            assert accepted.state == "settled" and accepted.outcome == "succeeded"
        assert await deliver_task_result(result_id) is None
    finally:
        await lease.release(session_status="idle")


async def test_bound_result_read_checks_sources_once_and_rechecks_the_next_page(monkeypatch):
    from assistant import reporting
    ctx, lease, _, _, result_id, _ = await prepare_report()
    original = reporting.validate_result_source
    calls = []

    async def checked(*args, **kwargs):
        calls.append(args[1].id)
        return await original(*args, **kwargs)

    monkeypatch.setattr(reporting, "validate_result_source", checked)
    try:
        page = await read_report_sources(ctx=ctx, result_id=result_id, max_chars=5)
        assert calls == [result_id]
        assert page["next_offset"] == 5
        async with get_db_session() as db:
            source = await db.get(Part, page["sources"][0]["part_id"])
            source.data = {**source.data, "text": "Changed source after page one"}
        with pytest.raises(AssistantError):
            await read_report_sources(ctx=ctx, result_id=result_id,
                offset=page["next_offset"], source_version=page["source_version"])
        assert calls == [result_id, result_id]
    finally:
        await lease.release(session_status="idle")


async def test_final_answer_and_processed_receipt_rollback_together(monkeypatch):
    from assistant import reporting
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    await seen_read(ctx, part, result_id=result_id)
    original = reporting.append_agent_event_locked

    async def fail_before_commit(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs["kind"] == "inbox.settled":
            raise RuntimeError("simulated finalization crash")
        return result

    monkeypatch.setattr(reporting, "append_agent_event_locked", fail_before_commit)
    try:
        with pytest.raises(RuntimeError, match="finalization crash"):
            await answer(ctx, lease, message, part)
        async with get_db_session() as db:
            assert (await db.get(Message, message.id)).finish is None
            assert (await db.get(TaskResult, result_id)).delivery_state == "accepted"
            assert (await db.get(AgentInboxItem, delivered["inbox_id"])).state == "claimed"
    finally:
        await lease.release(session_status="idle")


async def test_wrong_result_and_cursor_version_fail_closed_and_stop_does_not_retry():
    ctx, lease, message, part, result_id, _ = await prepare_report()
    try:
        with pytest.raises(AssistantError) as wrong_result:
            await read_report_sources(ctx=ctx, result_id="a-different-result")
        assert wrong_result.value.code == "ASSISTANT_REPORT_SCOPE"
        with pytest.raises(AssistantError) as wrong_version:
            await read_report_sources(ctx=ctx, result_id=result_id, offset=1, source_version="stale")
        assert wrong_version.value.code == "ASSISTANT_REPORT_VERSION"
        await answer(ctx, lease, message, part, finish="aborted")
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == "blocked" and result.last_error_code == "user_stopped"
        assert await deliver_task_result(result_id) is None
    finally:
        await lease.release(session_status="idle")


async def test_report_tools_are_scoped_and_a_finished_attempt_cannot_become_ordinary():
    from assistant.runtime import authorize_assistant_tool, runtime_view
    ctx, lease, message, part, result_id, _ = await prepare_report()
    try:
        view = await runtime_view(session_id=ctx.session_id, user_id=ctx.user_id,
                                  run_id=lease.run_id, generation=lease.generation)
        assert view["mode"] == "report_only"
        for tool_id, args in [("tasks.submit", {}), ("tasks.followup", {}), ("batch", {}),
                              ("bash", {}), ("capability_search", {}), ("tasks.get", {"task_id": "other"}),
                              ("history.read", {"session_id": "other"})]:
            with pytest.raises(AssistantError):
                await authorize_assistant_tool(ctx, tool_id, args)
        await authorize_assistant_tool(ctx, "results.read", {"result_id": result_id})
        await seen_read(ctx, part, result_id=result_id)
        await answer(ctx, lease, message, part)
        await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
        with pytest.raises(AssistantError, match="no longer current"):
            await authorize_assistant_tool(ctx, "tasks.submit", {})
    finally:
        await lease.release(session_status="idle")


async def test_source_change_after_read_blocks_finalization_without_losing_terminal_receipt():
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    try:
        await seen_read(ctx, part, result_id=result_id)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            source = await db.get(Part, result.output_refs[-1]["part_id"])
            source.data = {**source.data, "text": "Replaced source"}
        await answer(ctx, lease, message, part)
        assert message.finish == "error"
        await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == "blocked" and result.last_error_code == "ASSISTANT_RESULT_SOURCE_CHANGED"
            assert result.processed_message_id is None
            assert (await db.get(AgentInboxItem, delivered["inbox_id"])).state == "settled"
    finally:
        await lease.release(session_status="idle")


async def test_crash_reconciliation_retries_only_the_report_and_a_lost_wake_is_durable(monkeypatch):
    import asyncio
    from assistant.delivery import reconcile_report, recover_assistant_results
    from db.models.agent_driver import AgentDriverState
    ctx, lease, _, _, result_id, delivered = await prepare_report()
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        original_run, original_generation = result.run_id, result.generation
    # Simulate failure in the processor before it can finalize the answer.
    await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="error")
    await lease.release(session_status="idle")
    changed = await asyncio.gather(reconcile_report(result_id), reconcile_report(result_id))
    assert sorted(changed) == [False, True]
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "retry_wait" and result.processed_message_id is None
        result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    calls = []
    def lost_wake(session_id, user_id):
        calls.append((session_id, user_id))
        raise RuntimeError("lost wake after acceptance")
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lost_wake)
    await recover_assistant_results(result_ids=(result_id,))
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "accepted" and result.report_attempt == 2
        assert result.assistant_inbox_id != delivered["inbox_id"]
        assert result.run_id == original_run and result.generation == original_generation
        assert (await db.get(AgentInboxItem, result.assistant_inbox_id)).state == "accepted"
        execution_session = result.output_refs[-1]["session_id"]
        assert (await db.get(AgentDriverState, execution_session)).generation == original_generation
    assert (ctx.session_id, ctx.user_id) in calls
    assert not await reconcile_report(result_id)


async def test_service_read_without_a_subsequent_provider_projection_cannot_ack_result():
    ctx, lease, message, part, result_id, _ = await prepare_report()
    try:
        await read_report_sources(ctx=ctx, result_id=result_id)
        # A model can emit read calls and a final-looking answer together.
        # The server must not claim it received their yet-unseen outputs.
        await answer(ctx, lease, message, part)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == "retry_wait" and result.processed_message_id is None
            assert result.last_error_code == "report_evidence_incomplete"
    finally:
        await lease.release(session_status="idle")
