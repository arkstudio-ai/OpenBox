"""A recovered video job wakes its conversation; API-key cards get a deadline."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from video import job_recovery


async def test_resume_conversation_accepts_a_system_followup(monkeypatch):
    calls = {}

    async def driver_state(session_id):
        return SimpleNamespace(run_id=None, phase="idle")

    async def accept(**kwargs):
        calls["accept"] = kwargs
        return SimpleNamespace(state="accepted", created=True)

    async def wake(session_id, user_id):
        calls["wake"] = (session_id, user_id)
        return "run_1"

    monkeypatch.setattr("agent.driver.get_driver_state", driver_state)
    monkeypatch.setattr("agent.inbox.accept_inbox_item", accept)
    monkeypatch.setattr("agent.inbox.wake_inbox_session", wake)
    job = SimpleNamespace(id="video_1", session_id="session_1", user_id="u1")
    assert await job_recovery.resume_conversation(job) is True
    assert calls["accept"]["client_id"] == "vjob:video_1"
    assert calls["accept"]["system"] is True and calls["accept"]["delivery"] == "followup"
    assert "video_1" in calls["accept"]["prompt"]
    assert calls["wake"] == ("session_1", "u1")


async def test_resume_conversation_leaves_a_live_run_alone(monkeypatch):
    async def driver_state(session_id):
        return SimpleNamespace(run_id="run_9", phase="running")

    async def accept(**kwargs):
        raise AssertionError("must not queue while a run is live")

    monkeypatch.setattr("agent.driver.get_driver_state", driver_state)
    monkeypatch.setattr("agent.inbox.accept_inbox_item", accept)
    assert await job_recovery.resume_conversation(SimpleNamespace(id="v", session_id="s", user_id="u")) is False
    assert await job_recovery.resume_conversation(SimpleNamespace(id="v", session_id=None, user_id="u")) is False


async def test_system_client_ids_are_reserved_for_the_platform():
    from agent.inbox import _validate_input

    with pytest.raises(ValueError):
        _validate_input(prompt="x", attachments=(), client_id="vjob:video_1", output_format=None)
    assert _validate_input(prompt="x", attachments=(), client_id="vjob:video_1", output_format=None, system=True) == ()


async def test_card_deadline_comes_from_the_session_key_policy(monkeypatch):
    from auth import api_key as keys
    from db.base import get_db_session
    from db.models.session import Session as SessionRow
    from db.models.user import User
    from db.models.workspace import Workspace, WorkspaceMember
    from tool import question_tool

    uid, wid = f"u-{uuid4().hex[:8]}", f"w-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=uid, username=uid, role="user", is_active=True, failed_login_count=0, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=wid, name="w", owner_user_id=uid, created_at=now, updated_at=now))
        await db.flush()
        db.add(WorkspaceMember(workspace_id=wid, user_id=uid, role="owner", status="active", created_at=now, updated_at=now))
    row, _ = await keys.issue_key(user_id=uid, workspace_id=wid, name="k", policy={"interaction_timeout_s": 600})
    async with get_db_session() as db:
        db.add(SessionRow(id="session_key", user_id=uid, workspace_id=wid, project_id="p", agent="build",
                          status="idle", api_key_id=row.id, created_at=now, updated_at=now))
        db.add(SessionRow(id="session_web", user_id=uid, workspace_id=wid, project_id="p", agent="build",
                          status="idle", created_at=now, updated_at=now))
    assert await keys.interaction_timeout_for_session("session_key") == 600
    assert await keys.interaction_timeout_for_session("session_web") is None

    seen = {}

    async def fake_ask(**kwargs):
        seen.update(kwargs)
        return [["可以"]]

    monkeypatch.setattr("question.question.ask", fake_ask)
    args = question_tool.QuestionArgs(questions=[question_tool.QuestionItem(
        question="讲稿可以吗？", options=[question_tool.QuestionOption(label="可以")], custom=False)])
    ctx = SimpleNamespace(session_id="session_key", user_id=uid, message_id="m1", part_id="p1")
    await question_tool.execute(args, ctx)
    assert seen["expires_at"] is not None
    assert 590 <= (seen["expires_at"] - datetime.now(timezone.utc)).total_seconds() <= 600
    assert seen["questions"][0].custom is False

    ctx = SimpleNamespace(session_id="session_web", user_id=uid, message_id="m1", part_id="p1")
    await question_tool.execute(args, ctx)
    assert seen["expires_at"] is None
