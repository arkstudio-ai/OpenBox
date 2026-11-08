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
    # The voice card sits where 做菜单 was, and 做菜单 fills the slot beside 你拍我剪 (I0, LIGHT_MODE_I18N_PLAN §3).
    assert ids == ["video", "voice_assistant", "poster", "footage_edit", "menu", "douyin_publish", "review", "shopkeeper"]
    for entry in config.entries:
        if entry.kind == "coming_soon" or not entry.is_topic:
            continue
        assert entry.steps and entry.opening_card.text and entry.skill
        assert len(entry.step_ids()) == len(set(entry.step_ids()))
    video = config.entry("video")
    assert [sub.id for sub in video.subtypes] == ["dish", "shop", "visit"]
    assert "「餐品展示 · 烟火气特写」" in video.opening("dish", "dish_closeup").text
    assert "「烟火红」" in config.entry("menu").opening("menu_style", "menu_red").text
    assert "{sample}" not in video.opening().text and "「" not in video.opening().text
    assert config.entry("review").requires.binding == "douyin" and config.entry("douyin_publish").requires.desktop
    voice = config.entry("voice_assistant")
    assert voice.action == "voice_call" and voice.kind == "primary" and not voice.is_topic
    assert not voice.steps and not voice.skill and voice.opening_card is None and not voice.price_hint
    assert voice.public()["action"] == "voice_call"


def test_an_action_entry_carries_no_topic_parts():
    from light.entries import Entry
    base = {"id": "voice_assistant", "title": "语音助手", "kind": "primary", "action": "voice_call"}
    assert Entry.model_validate(base).action == "voice_call"
    for extra in ({"steps": [{"id": "a", "label": "A"}]}, {"skill": "x"}, {"opening_card": {"text": "hi"}}):
        with pytest.raises(ValueError):
            Entry.model_validate({**base, **extra})
    with pytest.raises(ValueError):  # a topic entry still needs its skill and opening card
        Entry.model_validate({"id": "menu", "title": "做菜单", "kind": "primary", "steps": [{"id": "a", "label": "A"}]})


def test_private_sample_media_is_signed_when_served():
    from light.entries import Entry
    entry = Entry.model_validate({
        "id": "video", "title": "做一条视频", "kind": "primary", "skill": "video-production",
        "steps": [{"id": "topic", "label": "选主题"}], "opening_card": {"text": "hi"},
        "subtypes": [{"id": "dish", "label": "餐品", "samples": [
            {"id": "a", "label": "A", "media_url": "oss:light/samples/a.mp4", "poster_url": "oss:light/samples/a.jpg"},
            {"id": "b", "label": "B", "media_url": "https://cdn.example/b.jpg"},
        ]}],
    })
    samples = entry.public(lambda v: v and v.replace("oss:", "https://signed/"))["subtypes"][0]["samples"]
    assert samples[0]["media_url"] == "https://signed/light/samples/a.mp4"
    assert samples[0]["poster_url"] == "https://signed/light/samples/a.jpg"
    assert samples[1]["media_url"] == "https://cdn.example/b.jpg" and samples[1]["poster_url"] is None
    assert entry.public()["subtypes"][0]["samples"][0]["media_url"] == "oss:light/samples/a.mp4"
    with pytest.raises(ValueError):
        Entry.model_validate({**entry.model_dump(), "subtypes": [{"id": "x", "label": "X", "samples": [
            {"id": "c", "label": "C", "media_url": "http://plain/c.mp4"}]}]})


def test_every_entry_skill_ships_with_the_backend():
    """An entry pointing at a skill that is not on disk would leave the agent with nothing to read."""
    from pathlib import Path
    skills = Path(__file__).resolve().parents[2] / ".openbox" / "skills"
    for entry in light_entries().entries:
        if entry.kind == "coming_soon" or not entry.is_topic:
            continue
        assert (skills / entry.skill / "SKILL.md").is_file(), entry.skill


def test_availability_follows_plan_desktop_and_bindings():
    config = light_entries()
    free = {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}
    paid = {"plan_id": "pro", "paid_plan": True, "desktop": True, "bound": {"douyin"}}
    assert light_mode.availability(config.entry("video"), free) == "ok"
    assert light_mode.availability(config.entry("douyin_publish"), free) == "needs_desktop"
    assert light_mode.availability(config.entry("review"), {**paid, "bound": set()}) == "needs_binding"
    assert light_mode.availability(config.entry("review"), paid) == "ok"
    # 你拍我剪 runs without a desktop (M3), so a free workspace can use it too.
    assert light_mode.availability(config.entry("footage_edit"), free) == "ok"
    assert light_mode.availability(config.entry("voice_assistant"), free) == "ok"


async def test_start_refuses_an_action_entry(monkeypatch):
    """The voice card dials in place; there is no topic chat to create for it."""
    with pytest.raises(light_mode.LightError) as info:
        await light_mode.start(user_id="u", workspace_id="w", entry_id="voice_assistant")
    assert info.value.code == "LIGHT_ENTRY_ACTION" and info.value.status == 409
    assert info.value.extra == {"action": "voice_call"}


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
    assert by_id == {"video": "ok", "voice_assistant": "ok", "menu": "ok", "poster": "ok", "douyin_publish": "needs_desktop",
                     "review": "needs_desktop", "shopkeeper": "needs_desktop", "footage_edit": "ok"}


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
    assert "界面模式：Light" in fragment and "entry_id=menu" in fragment and "style(选样式)" in fragment and "menu-maker" in fragment
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


async def test_prompt_fragment_names_the_shop_from_the_first_video_form():
    from db.repository.preference_repo import PgPreferenceRepo
    from session.session import create_session, update_session
    user = await _user()
    guided = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=user["default_workspace_id"])
    guided = await update_session(guided.id, user_id=user["id"], light_entry="video")
    assert "店名" not in await light_mode_fragment(user["id"], guided)
    await PgPreferenceRepo().upsert(user["id"], extra={"first_video": {"status": "ready", "shop_name": "巷口面馆", "headline": "红烧牛肉面"}})
    fragment = await light_mode_fragment(user["id"], guided)
    assert "店名「巷口面馆」" in fragment and "主打「红烧牛肉面」" in fragment and "light-video" in fragment
