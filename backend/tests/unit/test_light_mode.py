"""Light mode: entries and availability, starting a topic chat, ui_mode, the prompt fragment, recent results."""
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from api.light import router
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session as SessionRow
from db.repository.user_repo import PgUserRepo
from light import mode as light_mode
from light.entries import light_entries
from light.prompt import light_mode_fragment
from tool.question_tool import QuestionArgs, QuestionItem, QuestionOption, to_questions


async def _user():
    uid = uuid4().hex
    return await PgUserRepo().create(id=uid, username="lm" + uid[:8], password_hash=None)


def _app(user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"], "workspace_id": user["default_workspace_id"]}
    app.dependency_overrides[get_workspace] = lambda: {"id": user["default_workspace_id"], "role": "owner"}
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_entries_config_is_complete():
    config = light_entries()
    ids = [entry.id for entry in config.entries]
    assert ids == ["video", "menu", "poster", "douyin_publish", "review", "shopkeeper", "footage_edit"]
    for entry in config.entries:
        if entry.kind == "coming_soon":
            continue
        assert entry.steps and entry.opening_card.text and entry.skill
        assert len(entry.step_ids()) == len(set(entry.step_ids()))
    video = config.entry("video")
    assert [sub.id for sub in video.subtypes] == ["dish", "shop", "visit"]
    assert "「餐品展示 · 烟火气特写」" in video.opening("dish", "dish_closeup").text
    assert "{sample}" not in video.opening().text and "「" not in video.opening().text
    assert config.entry("review").requires.binding == "douyin" and config.entry("douyin_publish").requires.desktop


def test_availability_follows_plan_desktop_and_bindings():
    config = light_entries()
    free = {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}
    paid = {"plan_id": "pro", "paid_plan": True, "desktop": True, "bound": {"douyin"}}
    assert light_mode.availability(config.entry("video"), free) == "ok"
    assert light_mode.availability(config.entry("douyin_publish"), free) == "needs_desktop"
    assert light_mode.availability(config.entry("review"), {**paid, "bound": set()}) == "needs_binding"
    assert light_mode.availability(config.entry("review"), paid) == "ok"
    assert light_mode.availability(config.entry("footage_edit"), paid) == "coming_soon"


async def test_entries_endpoint_marks_desktop_entries_for_a_free_workspace(monkeypatch):
    user = await _user()

    async def facts(workspace_id):
        assert workspace_id == user["default_workspace_id"]
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(light_mode, "_workspace_facts", facts)
    async with _client(_app(user)) as client:
        body = (await client.get("/api/light/entries")).json()
    assert body["ui_mode"] == "pro" and body["plan_id"] == "free"
    by_id = {entry["id"]: entry["availability"] for entry in body["entries"]}
    assert by_id == {"video": "ok", "menu": "ok", "poster": "ok", "douyin_publish": "needs_desktop",
                     "review": "needs_desktop", "shopkeeper": "needs_desktop", "footage_edit": "coming_soon"}


async def test_start_creates_a_guided_session_with_its_opening_card(monkeypatch):
    user = await _user()

    async def facts(workspace_id):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(light_mode, "_workspace_facts", facts)
    async with _client(_app(user)) as client:
        started = (await client.post("/api/light/start", json={
            "entry_id": "video", "subtype": "dish", "sample": "dish_closeup"})).json()
        assert started["opening_card"]["text"].startswith("好，做一条「餐品展示 · 烟火气特写」")
        assert [step["id"] for step in started["steps"]] == ["topic", "photos", "confirm", "generate", "deliver"]
        assert (await client.post("/api/light/start", json={"entry_id": "nope"})).status_code == 400
        blocked = await client.post("/api/light/start", json={"entry_id": "douyin_publish"})
        assert blocked.status_code == 409 and blocked.json()["detail"]["availability"] == "needs_desktop"
        assert (await client.post("/api/light/start", json={"entry_id": "video", "subtype": "dish", "sample": "x"})).status_code == 400
    async with get_db_session() as db:
        row = await db.get(SessionRow, started["session_id"])
        messages = (await db.scalars(select(Message).where(Message.session_id == row.id).order_by(Message.created_at))).all()
        parts = (await db.scalars(select(Part).where(Part.session_id == row.id))).all()
    assert row.light_entry == "video" and row.title == "做一条视频" and row.kind == "normal"
    assert [m.role for m in messages] == ["user", "assistant"] and messages[1].finish == "stop"
    texts = {p.data["text"] for p in parts if p.type == "text"}
    assert any("选了「餐品展示 · 烟火气特写」" in t for t in texts) and started["opening_card"]["text"] in texts
    from session.session import get_session
    assert (await get_session(row.id, user_id=user["id"])).light_entry == "video"


async def test_ui_mode_defaults_and_switches():
    user = await _user()
    assert await light_mode.ui_mode(user["id"]) == "pro"
    await light_mode.default_mode_for_new_user(user["id"])
    assert await light_mode.ui_mode(user["id"]) == "light"
    async with _client(_app(user)) as client:
        assert (await client.get("/api/light/mode")).json() == {"ui_mode": "light"}
        assert (await client.put("/api/light/mode", json={"ui_mode": "pro"})).json() == {"ui_mode": "pro"}
        assert (await client.put("/api/light/mode", json={"ui_mode": "dark"})).status_code == 400
    # Other preference writers keep the mode.
    from db.repository.preference_repo import PgPreferenceRepo
    await PgPreferenceRepo().upsert(user["id"], extra={"browser_mode": "auto"}, onboarding={"welcome": True})
    assert await light_mode.ui_mode(user["id"]) == "pro"


async def test_prompt_fragment_only_for_light_sessions():
    user = await _user()
    from session.session import create_session, update_session
    plain = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=user["default_workspace_id"])
    assert await light_mode_fragment(user["id"], plain) == ""
    guided = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=user["default_workspace_id"])
    guided = await update_session(guided.id, user_id=user["id"], light_entry="menu")
    fragment = await light_mode_fragment(user["id"], guided)
    assert "界面模式：Light" in fragment and "entry_id=menu" in fragment and "style(选样式)" in fragment and "imagegen" in fragment
    await light_mode.set_ui_mode(user["id"], "light")
    plain_light = await light_mode_fragment(user["id"], plain)
    assert "界面模式：Light" in plain_light and "entry_id=" not in plain_light
    assert await light_mode_fragment(user["id"], None) == ""


def test_question_step_id_rides_in_detail():
    args = QuestionArgs(questions=[
        QuestionItem(question="选哪种？", options=[QuestionOption(label="A")], custom=False, step_id="topic"),
        QuestionItem(question="多少秒？"),
    ])
    first, second = to_questions(args)
    assert first.detail == {"light": {"step_id": "topic"}} and first.custom is False
    assert second.detail is None
    with pytest.raises(ValueError):
        QuestionItem(question="x", step_id="Bad Step")


async def test_recent_groups_results_by_conversation(monkeypatch):
    user = await _user()
    ws = user["default_workspace_id"]
    from models.message import FilePart, FileRelation, TextPart
    from session.session import create_assistant_message, create_session, create_user_message, save_part, update_session

    async def conversation(title, entry, outputs):
        session = await create_session(model="m", agent="build", title=title, user_id=user["id"], workspace_id=ws)
        await update_session(session.id, user_id=user["id"], light_entry=entry)
        um = await create_user_message(session_id=session.id, text="go", user_id=user["id"])
        am = await create_assistant_message(session.id, um.id, user_id=user["id"])
        for mime, role, kind in outputs:
            await save_part(FilePart(path="/workspace/x", mime_type=mime, asset_id="asset_" + uuid4().hex[:8],
                                     relation=FileRelation(role=role, kind=kind, label=kind),
                                     session_id=session.id, message_id=am.id), is_new=True, user_id=user["id"])
        await save_part(TextPart(text="done", session_id=session.id, message_id=am.id), is_new=True, user_id=user["id"])
        return session.id

    older = await conversation("海报", "poster", [("image/png", "result", "generated_image"), ("image/png", "input", "upload")])
    newer = await conversation("做一条视频", "video", [("video/mp4", "intermediate", "video_segment"), ("video/mp4", "final", "video_final")])
    async with _client(_app(user)) as client:
        body = (await client.get("/api/light/recent?limit=5")).json()
    groups = {g["session_id"]: g for g in body["groups"]}
    assert list(groups) == [newer, older]
    assert [o["kind"] for o in groups[newer]["outputs"]] == ["video_final"] and groups[newer]["entry_id"] == "video"
    assert [o["kind"] for o in groups[older]["outputs"]] == ["generated_image"] and groups[older]["title"] == "海报"
