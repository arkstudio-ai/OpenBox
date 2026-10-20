"""Video completion survives a busy task and reaches the assistant outbox."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import get_driver_state, reserve_run
from assistant.results import deliver_task_result
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.session import Session
from db.models.video_job import VideoJob
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running, terminal
from tests.unit.test_video_job_recovery import _insert_job, _fetch
from video import job_recovery


@pytest.fixture(autouse=True)
def reset_completion_cursor(monkeypatch):
    monkeypatch.setattr(job_recovery, "_completion_scan_after", None)
    monkeypatch.setattr(job_recovery, "STALE_AFTER_SECONDS", 0)


async def completed_job(lease, **overrides):
    return await _insert_job(age_seconds=0, user_id=lease.user_id, session_id=lease.session_id,
                             status="completed", completed_at=datetime.now(timezone.utc),
                             result_data={"completion_delivery_pending": True}, **overrides)


async def queued(job_id):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.client_id == f"vjob:{job_id}"))).all())


@pytest.mark.parametrize("recorded", [False, True])
async def test_busy_completion_is_durable_and_next_turn_reports_to_assistant(recorded):
    args, created, active, batch = await running()
    overrides = {"request_data": {"_trajectory_context": {
        "run_id": active.run_id, "generation": active.generation}}} if recorded else {}
    job_id = await completed_job(active, **overrides)
    try:
        assert await job_recovery.resume_conversation(await _fetch(job_id))
        assert not await job_recovery.resume_conversation(await _fetch(job_id))
        rows = await queued(job_id)
        assert len(rows) == 1 and rows[0].state == "accepted"
        assert rows[0].target == "next-turn" and rows[0].origin == "system_recovery"
        assert rows[0].origin_ref["intent_revision"] == 1
        state = await get_driver_state(active.session_id)
        assert state.run_id == active.run_id and not active.abort.is_set()
        assert (await inbox.claim_inbox_boundary(active, step=2, include_next_turn=False)).empty
        # This run's timeout reply arrives after completion, as in the incident.
        await terminal(active, batch.messages[0].id)
    finally:
        await active.release(session_status="idle")

    # A fresh driver (including one on process recovery) consumes the SQL inbox.
    next_run = await reserve_run(active.session_id, active.user_id)
    try:
        followup = await inbox.claim_inbox_boundary(next_run, step=1, include_next_turn=True)
        assert [row.id for row in followup.receipts] == [rows[0].id]
        await terminal(next_run, followup.messages[0].id)
    finally:
        await next_run.release(session_status="idle")
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(
            TaskResult.task_id == created["task_id"], TaskResult.generation == next_run.generation))
        assert result.outcome == "succeeded" and result.observed_intent_revision == 1
        result_id = result.id
    receipt = await deliver_task_result(result_id)
    async with get_db_session() as db:
        report = await db.get(AgentInboxItem, receipt["inbox_id"])
        assert report.session_id == args["main_id"] and report.origin == "task_result"
        assert report.origin_ref["task_id"] == created["task_id"]


async def test_completion_scan_retries_accept_failure_and_deduplicates(monkeypatch):
    _, _, active, _ = await running()
    job_id = await completed_job(active)
    original = inbox.accept_inbox_item

    async def unavailable(**kwargs):
        raise OSError("database temporarily unavailable")

    try:
        monkeypatch.setattr(inbox, "accept_inbox_item", unavailable)
        assert not await job_recovery.resume_conversation(await _fetch(job_id))
        assert await queued(job_id) == []
        monkeypatch.setattr(inbox, "accept_inbox_item", original)
        await job_recovery.sweep()
        assert len(await queued(job_id)) == 1
        await job_recovery.sweep()
        assert len(await queued(job_id)) == 1
    finally:
        await active.release(session_status="idle")


async def test_accepted_completion_survives_wake_failure(monkeypatch):
    _, _, active, _ = await running()
    job_id = await completed_job(active)

    async def unavailable(*args):
        raise OSError("wake interrupted")

    try:
        monkeypatch.setattr(inbox, "wake_inbox_session", unavailable)
        assert not await job_recovery.resume_conversation(await _fetch(job_id))
        assert len(await queued(job_id)) == 1
        assert (active.session_id, active.user_id) in await inbox.claimable_inbox_sessions()
        await job_recovery.resume_unattached_completions()
        assert len(await queued(job_id)) == 1
    finally:
        await active.release(session_status="idle")


@pytest.mark.parametrize("blocked", ["paused", "canceled", "archived", "deleted", "new_intent", "wrong_owner"])
async def test_completion_does_not_restart_unavailable_or_superseded_task(blocked):
    _, created, active, _ = await running()
    job_id = await completed_job(active)
    try:
        async with get_db_session() as db:
            task = await db.get(AssistantTask, created["task_id"])
            if blocked in {"paused", "canceled"}:
                task.desired_state = blocked
            elif blocked == "archived":
                task.archived_at = datetime.now(timezone.utc)
            elif blocked == "deleted":
                (await db.get(Session, active.session_id)).is_deleted = True
            elif blocked == "new_intent":
                task.intent_revision += 1
            else:
                (await db.get(VideoJob, job_id)).user_id = "another-user"
        assert not await job_recovery.resume_conversation(await _fetch(job_id))
        await job_recovery.resume_unattached_completions()
        assert await queued(job_id) == []
    finally:
        await active.release(session_status="idle")


@pytest.mark.parametrize("excluded", ["attached", "old", "recent", "failed", "stt", "no_asset", "legacy"])
async def test_completion_scan_only_retries_undelivered_video(excluded):
    _, _, active, _ = await running()
    job_id = await completed_job(active)
    try:
        async with get_db_session() as db:
            row = await db.get(VideoJob, job_id)
            if excluded == "attached": row.attached_message_id = "already-attached"
            elif excluded == "old": row.completed_at -= timedelta(days=8)
            elif excluded == "recent": row.completed_at += timedelta(minutes=1)
            elif excluded == "failed": row.status = "failed"
            elif excluded == "stt": row.kind = "stt"
            elif excluded == "no_asset": row.output_asset_id = None
            else: row.result_data = {}
        await job_recovery.resume_unattached_completions()
        assert await queued(job_id) == []
    finally:
        await active.release(session_status="idle")


async def test_completion_scan_pages_past_temporarily_unavailable_jobs(monkeypatch):
    _, _, active, _ = await running()
    monkeypatch.setattr(job_recovery, "MAX_JOBS_PER_SWEEP", 1)
    first, second = await completed_job(active), await completed_job(active)
    original = job_recovery._resume_reference

    async def temporarily_blocked(job):
        return None if job.id == first else await original(job)

    monkeypatch.setattr(job_recovery, "_resume_reference", temporarily_blocked)
    try:
        await job_recovery.resume_unattached_completions()
        assert await queued(second) == []
        await job_recovery.resume_unattached_completions()
        assert len(await queued(second)) == 1
        monkeypatch.setattr(job_recovery, "_resume_reference", original)
        await job_recovery.resume_unattached_completions()  # end page wraps cursor
        await job_recovery.resume_unattached_completions()
        assert len(await queued(first)) == 1
    finally:
        await active.release(session_status="idle")
