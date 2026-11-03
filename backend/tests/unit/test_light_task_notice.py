"""「任务完成」 must not reach the owner before the Light cut does (docs/LIGHT_FOOTAGE_EDIT.md)."""
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
        db.add(VideoJob(id=job_id, user_id=user_id, session_id=session_id, kind="render",
                        idempotency_key=job_id, request_hash="h", status=status,
                        request_data={}, result_data={}, created_at=now, updated_at=now))
        await db.commit()


def _session(workspace, *, light_entry):
    return SimpleNamespace(id=f"session_{uuid4().hex[:16]}", parent_id=None, kind="chat",
                           workspace_id=workspace, title="你拍我剪", light_entry=light_entry)


async def _finish(monkeypatch, session, owner, *, failed=False):
    seen = []

    async def emit(db, **kwargs):
        seen.append(kwargs["kind"])

    monkeypatch.setattr(events, "emit", emit)
    ticket = SimpleNamespace(user_id=owner, run_id="run-1", generation=1, session_id=session.id)
    async with get_db_session() as db:
        await events.task_finished(db, session, ticket, failed=failed)
    return seen


async def test_light_turn_with_a_cut_still_rendering_sends_no_task_completed(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry="footage_edit")
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner) == []


async def test_light_turn_after_the_cut_settled_sends_task_completed(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry="footage_edit")
    await _job(owner, session.id, "completed")
    assert await _finish(monkeypatch, session, owner) == ["task_completed"]


async def test_light_turn_with_no_job_at_all_sends_task_completed(monkeypatch):
    owner, _, workspace = await accounts()
    assert await _finish(monkeypatch, _session(workspace, light_entry="menu"), owner) == ["task_completed"]


async def test_a_failed_light_turn_still_reports_the_failure(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry="footage_edit")
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner, failed=True) == ["task_failed"]


async def test_an_ordinary_conversation_is_not_affected(monkeypatch):
    owner, _, workspace = await accounts()
    session = _session(workspace, light_entry=None)
    await _job(owner, session.id, "in_progress")
    assert await _finish(monkeypatch, session, owner) == ["task_completed"]
