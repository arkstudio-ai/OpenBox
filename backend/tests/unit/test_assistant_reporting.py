"""Report turns: bounded result reads, atomic receipts and report-only scope (V2).

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 7.2): the report input already carries
the result summary, so a finished, non-empty answer settles the report. Reads
stay bounded and scoped to the bound result, but are no longer coverage proof.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent import inbox
from assistant.policy import AssistantError
from assistant.reporting import read_report_sources
from assistant.results import deliver_task_result
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.message import Message
from db.models.part import Part
from models.message import TextPart, ToolStatus
from session.session import save_part, update_message_info
# prepare_report and seen_read are re-exported for older importers of this module.
from tests.unit.assistant_helpers import prepare_report, seen_read  # noqa: F401
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401

ANSWER_TEXT = "The report was created. Browser verification remains untested."


async def answer(ctx, lease, message, read_part, *, finish="stop", text=ANSWER_TEXT):
    fence = (ctx.session_id, lease.run_id, lease.generation)
    read_part.status = ToolStatus.COMPLETED
    read_part.output = "See the authorized read above."
    await save_part(read_part, user_id=ctx.user_id, run_fence=fence)
    if text:
        await save_part(TextPart(session_id=ctx.session_id, message_id=message.id, text=text),
            is_new=True, user_id=ctx.user_id, run_fence=fence)
    message.finish = finish
    await update_message_info(message, user_id=ctx.user_id, run_fence=fence)


async def test_empty_answer_is_not_processed_and_retry_only_creates_a_report():
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    try:
        page = await seen_read(ctx, part, result_id=result_id, max_chars=5)
        assert page["next_offset"] == 5
        await answer(ctx, lease, message, part, text="")
        assert message.finish == "error" and message.error["code"] == "report_failed"
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == "retry_wait" and result.processed_message_id is None
            assert result.last_error_code == "report_failed"
            assert (await db.get(AgentInboxItem, delivered["inbox_id"])).state == "settled"
            result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        retry = await deliver_task_result(result_id)
        assert retry["report_attempt"] == 2 and retry["inbox_id"] != delivered["inbox_id"]
        async with get_db_session() as db:
            second = await db.get(AgentInboxItem, retry["inbox_id"])
            assert second.session_id == ctx.session_id and second.origin_ref["execution_mode"] == "report_only"
    finally:
        await lease.release(session_status="idle")


async def test_paged_result_read_returns_request_and_report_sources():
    ctx, lease, _, part, result_id, _ = await prepare_report()
    try:
        page = await seen_read(ctx, part, result_id=result_id, max_chars=7)
        kinds = {row["kind"] for row in page["sources"]}
        while page["next_offset"] is not None:
            page = await seen_read(ctx, part, result_id=result_id, max_chars=7,
                offset=page["next_offset"], source_version=page["source_version"])
            kinds.update(row["kind"] for row in page["sources"])
        assert kinds == {"request", "report"}
        async with get_db_session() as db:
            assert (await db.get(TaskResult, result_id)).delivery_state == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_answer_without_reading_sources_commits_one_processed_receipt():
    ctx, lease, message, part, result_id, delivered = await prepare_report()
    try:
        # The report input carries the summary; reading sources is optional.
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


async def test_bound_result_read_checks_ownership_once_per_page_without_rehashing(monkeypatch):
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
        # Revocation is not retroactive (D1): an edit is not a hash failure.
        following = await read_report_sources(ctx=ctx, result_id=result_id,
            offset=page["next_offset"], source_version=page["source_version"])
        assert following["source_version"] == page["source_version"]
        assert following["sources"][0]["text"].startswith("ed source after page one")
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
        from agent.driver import request_abort
        assert await request_abort(ctx.session_id, ctx.user_id, expected_run_id=lease.run_id,
                                   expected_generation=lease.generation, reason="user_stop")
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
