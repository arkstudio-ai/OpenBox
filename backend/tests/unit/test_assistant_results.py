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
from models.message import TextPart, ToolPartData, ToolStatus
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


async def result_ready(*, settle=True, finish="stop", tool_outcomes=()):
    owner, _, workspace, main, kwargs = await setup_task()
    accepted = await accept_task_command(**kwargs)
    lease = await reserve_run(accepted["execution_session_id"], owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (lease.session_id, lease.run_id, lease.generation)
    message = await create_assistant_message(lease.session_id, batch.messages[0].id,
        model_id="test/model", agent="build", user_id=owner, run_fence=fence)
    for index, (tool, args, metadata) in enumerate(tool_outcomes):
        await save_part(ToolPartData(tool=tool, canonical_tool_id=tool, call_id=f"outcome-{index}",
            wire_tool_name=tool, provider_binding_digest="d" * 64, provider_dialect="openai", stream_seq=index,
            status=ToolStatus.ERROR if metadata.get("error") else ToolStatus.COMPLETED,
            input=args, output="Publication failed" if metadata.get("error") else "Tool completed",
            metadata=metadata, session_id=lease.session_id, message_id=message.id),
            user_id=owner, is_new=True, run_fence=fence)
    await save_part(TextPart(session_id=lease.session_id, message_id=message.id,
                             text="The report is saved. Browser verification is still untested."),
                    user_id=owner, is_new=True, run_fence=fence)
    message.finish = finish
    await update_message_info(message, user_id=owner, run_fence=fence)
    if settle:
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    return owner, workspace, main, accepted, lease, message


@pytest.mark.parametrize("last_action,expected", [("status", "error"), ("publish", "succeeded")])
async def test_stopping_after_publish_refusal_is_not_business_success(last_action, expected):
    outcomes = [("desktop_publish", {"action": "publish", "asset_id": "video-a"},
                 {"error": True, "refused": True, "business_outcome": "failed", "business_target": "video-a"}),
                ("desktop_publish", {"action": last_action, "asset_id": "video-a"},
                 {"business_outcome": "succeeded", "business_target": "video-a"} if last_action == "publish" else {})]
    owner, _, _, accepted, lease, _ = await result_ready(tool_outcomes=outcomes)
    try:
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
            task = await db.get(AssistantTask, accepted["task_id"])
            assert result.outcome == expected
            assert task.observed_state == ("completed" if expected == "succeeded" else "error")
            if expected == "error":
                assert "Publication failed" in result.summary
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("verified", [False, True])
async def test_browser_fallback_needs_a_matching_receipt_before_success(verified):
    from assistant.follow_through import unverified_publication
    from tool.tool import ToolContext
    outcomes = [("desktop_publish", {"action":"publish", "asset_id":"video-a"},
        {"error":True, "refused":True, "job_id":"pub-a", "business_outcome":"failed", "business_target":"video-a"}),
        ("browser_action", {"script":"await page.getByText('Publish').click()"}, {})]
    if verified:
        outcomes.append(("desktop_publish", {"action":"status", "job_id":"pub-a", "verify":True},
            {"business_outcome":"succeeded", "business_target":"video-a", "publication_state":"submitted"}))
    owner, _, _, accepted, lease, message = await result_ready(tool_outcomes=outcomes)
    try:
        reminder = await unverified_publication(ToolContext(user_id=owner, session_id=lease.session_id), [message.id])
        assert (reminder is None) is verified
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
            assert result.outcome == ("succeeded" if verified else "error")
    finally:
        await lease.release(session_status="idle")


async def test_review_or_browser_failure_followed_by_observation_is_not_completion():
    _, _, _, accepted, lease, _ = await result_ready(tool_outcomes=[
        ("question", {}, {"error": True}), ("computer", {"action": "screenshot"}, {})])
    try:
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
            assert result.outcome == "error"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("target,expected", [("video-a", "succeeded"), ("other-video", "error")])
async def test_receipt_can_resolve_uncertain_attempt_without_repeating_publish(target, expected):
    _, _, _, accepted, lease, _ = await result_ready(tool_outcomes=[
        ("desktop_publish", {"action": "publish", "asset_id": "video-a"},
         {"error": True, "business_outcome": "failed", "business_target": "video-a"}),
        ("desktop_publish", {"action": "status", "verify": True},
         {"business_outcome": "succeeded", "business_target": target})])
    try:
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
            assert result.outcome == expected
    finally:
        await lease.release(session_status="idle")


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


REPORT_TEXT = "The report is saved. Browser verification is still untested."


async def test_legacy_recovery_marker_settles_without_attributing_current_intent():
    from agent.driver import get_driver_state
    _, _, _, accepted, lease, _ = await result_ready(settle=False)
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        row = await db.get(AgentInboxItem, accepted["inbox_id"])
        row.origin = "system_recovery"
        row.origin_ref = {"entrypoint": "question_answer"}
        row.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        task = await db.get(AssistantTask, accepted["task_id"])
        task.intent_revision = 5
        state_before = task.observed_state
    assert await inbox.settle_orphaned_claims() >= 1
    async with get_db_session() as db:
        row = await db.get(AgentInboxItem, accepted["inbox_id"])
        task = await db.get(AssistantTask, accepted["task_id"])
        assert row.state == "settled" and row.outcome == "recovered"
        assert "intent_revision" not in row.origin_ref
        assert task.intent_revision == 5 and task.observed_state == state_before
        assert task.latest_result_id is None
        assert await db.scalar(select(func.count()).select_from(TaskResult)) == 0
    assert (await get_driver_state(lease.session_id)).phase == "idle"
    assert await inbox.settle_orphaned_claims() == 0


@pytest.mark.parametrize("origin,outcome", [
    ("human_text", "recovered"), ("assistant_delegation", "recovered"),
    ("unknown", "recovered"), ("system_recovery", "succeeded"),
])
async def test_unverified_user_or_non_recovery_result_remains_rejected(origin, outcome):
    from assistant.policy import AssistantError
    _, _, _, accepted, lease, message = await result_ready(settle=False)
    async with get_db_session() as db:
        row = await db.get(AgentInboxItem, accepted["inbox_id"])
        row.origin = origin
        row.origin_ref = {}
    try:
        with pytest.raises(AssistantError) as denied:
            await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome=outcome)
        assert denied.value.code == "ASSISTANT_INTENT_UNVERIFIED"
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, accepted["inbox_id"])).state == "claimed"
            assert await db.scalar(select(func.count()).select_from(TaskResult)) == 0
    finally:
        await lease.release(session_status="idle")


async def test_delivery_carries_the_settled_summary_and_ignores_later_source_edits():
    _, _, main, accepted, lease, message = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
        assert result.summary == REPORT_TEXT
        part = await db.scalar(select(Part).where(Part.message_id == message.id))
        part.data = {**part.data, "text": "Changed evidence"}
    # Revocation is not retroactive (D1): delivery uses the summary captured
    # at settlement and does not re-hash the original parts.
    receipt = await deliver_task_result(result.id)
    assert receipt is not None and receipt["report_attempt"] == 1
    async with get_db_session() as db:
        delivered = await db.get(TaskResult, result.id)
        report = await db.get(AgentInboxItem, receipt["inbox_id"])
        assert delivered.delivery_state == "accepted" and delivered.assistant_inbox_id == report.id
        assert report.session_id == main.id and report.origin_ref["execution_mode"] == "report_only"
        assert REPORT_TEXT in report.prompt and "Changed evidence" not in report.prompt


@pytest.mark.parametrize("change", ["member", "execution", "project"])
async def test_delivery_blocks_a_result_after_current_permission_is_revoked(change):
    from db.models.project import Project
    from db.models.session import Session
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
        if change == "member":
            (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
        elif change == "execution":
            (await db.get(Session, accepted["execution_session_id"])).is_deleted = True
        else:
            (await db.get(Project, main.project_id)).is_deleted = True
    assert await deliver_task_result(result_id) is None
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "blocked"
        assert result.assistant_inbox_id is None and result.processed_message_id is None


async def test_an_empty_reply_is_recorded_with_its_reason_not_as_an_unexplained_failure():
    # Measured 2026-10-26: a redundant video-completion wake replied with
    # nothing; the result had no summary and was reported as "failed, no cause".
    owner, _, workspace, main, kwargs = await setup_task()
    accepted = await accept_task_command(**kwargs)
    lease = await reserve_run(accepted["execution_session_id"], owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (lease.session_id, lease.run_id, lease.generation)
    message = await create_assistant_message(lease.session_id, batch.messages[0].id,
        model_id="test/model", agent="build", user_id=owner, run_fence=fence)
    message.finish = "stop"
    await update_message_info(message, user_id=owner, run_fence=fence)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
        assert result.outcome == "error"
        assert "EMPTY_REPLY" in result.summary and "without a reply" in result.summary
