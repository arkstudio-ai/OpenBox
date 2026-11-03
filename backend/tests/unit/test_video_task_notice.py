"""「任务完成」 must not reach the owner before a still-rendering video does.

The video tool's inline wait budget ends the model's turn while the job is
in flight; video.job_recovery.resume_conversation later wakes the session
with a followup turn, whose own finish_run sends the notice once the film is
attached. task_finished therefore stays silent while any video job of the
session is unsettled (gw2, 30 days to 2026-11-04: 204 of 1167 task_completed
rows went out with a job still in flight, median 217s early).
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.video_job import VideoJob
from notifications import events
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _job(user_id, session_id, status):
    now = datetime.now(timezone.utc)
    job_id = f"video_{uuid4().hex[:16]}"
    async with get_db_session() as db:
        db.add(VideoJob(id=job_id, user_id=user_id, session_id=session_id, kind="segment",
                        idempotency_key=job_id, request_hash="h", status=status,
                        request_data={}, result_data={}, created_at=now, updated_at=now))
        await db.commit()
    return job_id


def _session(workspace):
    return SimpleNamespace(id=f"session_{uuid4().hex[:16]}", parent_id=None, kind="chat",
                           workspace_id=workspace, title="做一条短视频")


async def _finish(monkeypatch, session, owner, *, failed=False):
    seen = []

    async def emit(db, **kwargs):
        seen.append(kwargs["kind"])

    monkeypatch.setattr(events, "emit", emit)
    ticket = SimpleNamespace(user_id=owner, run_id="run-1", generation=1, session_id=session.id)
    async with get_db_session() as db:
        await events.task_finished(db, session, ticket, failed=failed)
    return seen


@pytest.mark.parametrize("status", ["submitting", "in_progress", "finalizing", "queued"])
async def test_a_turn_with_a_video_still_rendering_sends_no_task_completed(monkeypatch, status):
    owner, _, workspace = await accounts()
    session = _session(workspace)
    await _job(owner, session.id, status)
    assert await _finish(monkeypatch, session, owner) == []


@pytest.mark.parametrize("status", list(events.VIDEO_JOB_SETTLED))
async def test_a_turn_after_the_video_settled_sends_task_completed(monkeypatch, status):
    owner, _, workspace = await accounts()
    session = _session(workspace)
    await _job(owner, session.id, status)
    assert await _finish(monkeypatch, session, owner) == ["task_completed"]


async def test_a_turn_with_no_video_job_sends_task_completed(monkeypatch):
    owner, _, workspace = await accounts()
    assert await _finish(monkeypatch, _session(workspace), owner) == ["task_completed"]


async def test_one_unsettled_job_among_settled_ones_still_holds_the_notice(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace)
    await _job(owner, session.id, "completed")
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner) == []


async def test_another_sessions_in_flight_job_does_not_hold_this_session(monkeypatch):
    owner, _, workspace = await accounts()
    other = _session(workspace)
    await _job(owner, other.id, "in_progress")
    assert await _finish(monkeypatch, _session(workspace), owner) == ["task_completed"]


async def test_a_failed_turn_still_reports_the_failure(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace)
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner, failed=True) == ["task_failed"]


async def test_the_followup_turn_after_settlement_delivers_the_notice_once(monkeypatch):
    """The wake from job_recovery ends its own run with the job settled: that one notifies."""
    owner, _, workspace = await accounts()
    session = _session(workspace)
    job_id = await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner) == []
    async with get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        job.status = "completed"
        await db.commit()
    assert await _finish(monkeypatch, session, owner) == ["task_completed"]
