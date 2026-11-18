"""Guided form writes, inbox receipts, and same-session result projection."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, select

from api.light import router
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import _upgrade_desktop_assistant_columns, get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.repository.user_repo import PgUserRepo
from light import mode, production, production_form as forms
from session.session import get_session


async def _user():
    user_id = uuid4().hex
    return await PgUserRepo().create(id=user_id, username=f"lp{user_id[:8]}", password_hash=None)


def _client(user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": user["id"], "workspace_id": user["default_workspace_id"], "ui_language": "zh-CN",
    }
    app.dependency_overrides[get_workspace] = lambda: {"id": user["default_workspace_id"], "role": "owner"}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_saved_draft_and_idempotent_queue_share_the_agent_session(monkeypatch):
    owner, other = await _user(), await _user()

    async def facts(_):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(mode, "_workspace_facts", facts)
    monkeypatch.setattr(production, "schedule_inbox_wake", lambda *_: None)
    async with _client(owner) as client:
        entry = await client.post("/api/light/productions", json={"entry_id": "video", "brief": "小龙虾"})
        assert entry.status_code == 201, entry.text
        session_id = entry.json()["session_id"]
        path = f"/api/light/productions/{session_id}"
        session = await get_session(session_id, user_id=owner["id"])
        assert session.light_entry == "video" and "light_production" not in session.model_dump()

        patch = await client.patch(path, json={"revision": 1, "template": "twist", "page": "review", "shop": "巷口"})
        assert patch.json()["revision"] == 2
        assert (await client.patch(path, json={"revision": 1, "script": "stale"})).status_code == 409
        assert (await client.patch(path, json={"revision": 2, "template": "warm_red"})).status_code == 400
        assert (await client.patch(path, json={"revision": 2, "asset_ids": ["foreign"]})).status_code == 400
        args = {"revision": 2, "action": "prepare", "request_id": uuid4().hex}
        for _ in range(2):
            queued = await client.post(f"{path}/actions", json=args)
            assert queued.json()["status"] == "working", queued.text
        assert (await client.post(f"{path}/actions", json={**args, "request_id": uuid4().hex})).status_code == 409
        history = (await client.get("/api/light/productions")).json()["items"]
        assert [(row["session_id"], row["page"]) for row in history] == [(session_id, "review")]

    async with _client(other) as client:
        assert (await client.get(path)).status_code == 404
        assert (await client.patch(path, json={"revision": 2, "script": "foreign"})).status_code == 404

    async with get_db_session() as db:
        items = (await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == session_id))).all()
        assert len(items) == 1
        assert items[0].origin_ref["entrypoint"] == "light_production"
        assert items[0].prompt == "请生成视频文案。"


def test_video_progress_uses_the_composition_receipt_instead_of_one_shot():
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(
        light_entry="video", status="idle", created_at=now - timedelta(seconds=10),
        light_production={**forms.initial_form("video", "巷口", ""), "template": "twist", "run": {"action": "render"}},
    )
    partial = SimpleNamespace(created_at=now, data={"asset_id": "clip", "mime_type": "video/mp4", "relation": {"role": "final"}})
    assert production._output_candidates(row, [], [partial]) == []
    job = SimpleNamespace(kind="compose", status="in_progress", result_data={})
    assert production._work_view(row, None, [job], None, False) == {"status": "working", "phase": "compose"}
    job.status, job.output_asset_id, job.completed_at = "completed", "ims-result", now
    assert production._output_candidates(row, [job], [partial]) == ["ims-result"]


def test_writing_copy_exposes_only_preparation_tools():
    names = ("light_production_form", "footage_analyze", "video_generate", "image_gen", "desktop_publish")
    tools = {name: SimpleNamespace(id=name) for name in names}
    session = SimpleNamespace(light_entry="footage_edit", light_production={"revision": 1})
    assert set(forms.restrict_guided_tools(tools, session, "prepare")) == {"light_production_form", "footage_analyze"}
    assert set(forms.restrict_guided_tools(tools, session, "render")) == {"footage_analyze"}


def test_older_desktop_database_preserves_its_sessions():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE sessions (id TEXT, user_id TEXT, workspace_id TEXT, kind TEXT, is_deleted BOOLEAN)"
        )
        connection.exec_driver_sql("INSERT INTO sessions VALUES ('existing', 'owner', 'shop', 'normal', 0)")
        _upgrade_desktop_assistant_columns(connection)
        assert connection.exec_driver_sql("SELECT id, light_production FROM sessions").one() == ("existing", None)
    engine.dispose()
