"""Real-schema voice navigation: unlinked work, output evidence, pagination and isolation."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from core.config import get_config
from db.base import get_db_session
from db.models.assistant import AssistantTask
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.video_job import VideoJob
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready
from voice.tools import CallScope, run
from voice.work import overview


async def video(owner, workspace, *, title="多巴胺居家场景", ready=True, minute=0):
    session = await create_session(user_id=owner, workspace_id=workspace, title=title)
    now = datetime.now(timezone.utc) + timedelta(minutes=minute)
    job_id, asset_id = "job-" + uuid4().hex, "asset-" + uuid4().hex
    async with get_db_session() as db:
        (await db.get(Session, session.id)).updated_at = now
        db.add(FileAsset(id=asset_id, user_id=owner, workspace_id=workspace, session_id=session.id,
            project_id=session.project_id, name="video.mp4", oss_key="unit-test/unused.mp4",
            mime="video/mp4", size=100, status="ready" if ready else "pending", source="agent", created_at=now))
        db.add(VideoJob(id=job_id, user_id=owner, session_id=session.id, project_id=session.project_id,
            kind="segment", idempotency_key=job_id, status="completed", output_asset_id=asset_id,
            prompt="多彩客厅里的人物玩手机，配粤语旁白。", created_at=now, updated_at=now, completed_at=now))
    return session, job_id, asset_id


async def test_unlinked_video_and_watched_work_are_both_read_without_creating_tasks():
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        (await db.get(AssistantTask, accepted["task_id"])).observed_state = "waiting_input"
    session, job_id, asset_id = await video(owner, workspace, minute=2)
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id)
    value = await run("tasks_overview", scope, "{}")
    assert value["status"] == "ok"
    newest, old = value["tasks"]
    assert newest["session_id"] == session.id and newest["watched"] is False and newest["task_id"] is None
    assert newest["state"] == "completed" and newest["conversation_state"] == "idle"
    assert newest["video"]["job_id"] == job_id and newest["video"]["asset_id"] == asset_id
    assert newest["video"]["output_available"] is True
    assert old["state"] == "waiting_input" and value["coverage"] == "own_project_conversations_and_watched_tasks"
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(AssistantTask.user_id == owner)) == 1


async def test_video_filter_ignores_stt_and_does_not_claim_unavailable_output_is_complete():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    session, job_id, _ = await video(owner, workspace, ready=False)
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    async with get_db_session() as db:
        db.add(VideoJob(id="stt-newer", user_id=owner, session_id=session.id, kind="stt",
            idempotency_key="stt-newer", status="completed", created_at=now, updated_at=now))
    await create_session(user_id=owner, workspace_id=workspace, title="没有视频的聊天")
    value = await overview(CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id), {"media_kind": "video"})
    [item] = value["items"]
    assert item["video"]["job_id"] == job_id and not item["video"]["output_available"]
    assert item["state"] == "output_unavailable" and item["video"]["asset_id"] is None


async def test_status_includes_current_decision_without_replaying_answered_or_expired_cards():
    from tests.unit.test_assistant_requests_v2 import ask_in
    from db.models.question import QuestionCheckpoint
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    session = await create_session(user_id=owner, workspace_id=workspace, title="鲜果店视频")
    identity, _ = await ask_in(session, owner, question="新图方案21.71积分，是否开始生成？", options=[{"label": "可以"}])
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id)
    [item] = (await overview(scope, {}))["items"]
    assert item["state"] == "waiting_input" and item["waiting_for_user"]
    assert item["pending_request"]["request_id"] == identity
    assert "21.71" in item["pending_request"]["questions"][0]["question"]
    async with get_db_session() as db:
        (await db.get(QuestionCheckpoint, identity)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert "pending_request" not in (await overview(scope, {}))["items"][0]
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, identity)
        row.expires_at, row.status = None, "answered"
    assert "pending_request" not in (await overview(scope, {}))["items"][0]


async def test_work_pages_bind_actor_and_filters_and_do_not_leak_shared_workspace_members(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "test-voice-work-cursor-secret")
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    first, _, _ = await video(owner, workspace, minute=2)
    second, _, _ = await video(owner, workspace)
    await create_session(user_id=other, workspace_id=workspace, title="别人的秘密")
    child = await create_session(user_id=owner, workspace_id=workspace, parent_id=first.id, title="隐藏子任务")
    deleted = await create_session(user_id=owner, workspace_id=workspace, title="已删除")
    async with get_db_session() as db:
        (await db.get(Session, deleted.id)).is_deleted = True
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id)
    page = await overview(scope, {}, limit=1)
    assert [item["session_id"] for item in page["items"]] == [first.id] and page["next_cursor"]
    following = await overview(scope, {"cursor": page["next_cursor"]}, limit=1)
    assert [item["session_id"] for item in following["items"]] == [second.id] and not following["next_cursor"]
    with pytest.raises(AssistantError):
        await overview(scope, {"cursor": page["next_cursor"], "query": "different"}, limit=1)
    foreign_scope = CallScope(user_id=other, workspace_id=workspace, main_session_id=main.id)
    assert await run("tasks_overview", foreign_scope, "{}") == {"status": "unavailable"}
    missing = await overview(scope, {"query": "没有这个标题"})
    assert missing["query_relaxed"] and not missing["title_filter_only"]
    assert [item["session_id"] for item in missing["items"]] == [first.id, second.id]
    assert missing["untrusted_data"] and missing["grants_authority"] is False
    relaxed = await overview(scope, {"query": "多巴胺风格居家", "media_kind": "video"}, limit=1)
    assert relaxed["query_relaxed"] and relaxed["next_cursor"]
    rest = await overview(scope, {"query": "多巴胺风格居家", "media_kind": "video",
                                  "cursor": relaxed["next_cursor"]}, limit=1)
    assert rest["query_relaxed"] and [item["session_id"] for item in rest["items"]] == [second.id]
