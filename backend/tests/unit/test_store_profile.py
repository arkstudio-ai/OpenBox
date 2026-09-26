"""Store profile (门店档案): API, binding hook, starter cards, persona bootstrap gating."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.stores import StoreCreate, StorePatch, create_store, list_stores, patch_store, starter_cards
from db.base import get_db_session
from db.models.store import Store
from db.repository.user_repo import PgUserRepo
from memory import service as memory_service
from store import persona_init
from store import service as store_service


async def _user(label: str = "merchant") -> dict:
    suffix = uuid4().hex[:10]
    row = await PgUserRepo().create(
        id=f"store_{label}_{suffix}", username=f"{label}-{suffix}",
        email=f"{label}-{suffix}@example.test", password_hash="unused",
    )
    return {"user_id": row["id"], "role": "user", "workspace_id": row["default_workspace_id"]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_create_list_patch_and_conflict():
    user = await _user()
    assert (await list_stores(current_user=user))["items"] == []

    created = await create_store(StoreCreate(name="泽岚鲜果", category="food",
                                             main_platforms=["douyin_laike"]), current_user=user)
    assert created["category"] == "food" and created["categoryOpen"] is True
    assert created["mainPlatforms"] == ["douyin_laike"]
    assert created["personaStatus"] == "none"

    listing = await list_stores(current_user=user)
    assert [s["id"] for s in listing["items"]] == [created["id"]]
    assert "food" in listing["openCategories"]

    with pytest.raises(HTTPException) as conflict:
        await create_store(StoreCreate(name="第二家"), current_user=user)
    assert conflict.value.status_code == 409

    patched = await patch_store(created["id"], StorePatch(name="泽岚鲜果（总店）", main_platforms=["meituan_merchant"]),
                                current_user=user)
    assert patched["name"] == "泽岚鲜果（总店）"
    assert patched["mainPlatforms"] == ["meituan_merchant"]


async def test_other_workspace_reads_as_404():
    owner, other = await _user("owner"), await _user("other")
    created = await create_store(StoreCreate(name="A 店"), current_user=owner)
    with pytest.raises(HTTPException) as missing:
        await patch_store(created["id"], StorePatch(name="改名"), current_user=other)
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as cards_missing:
        await starter_cards(created["id"], locale="zh-CN", current_user=other)
    assert cards_missing.value.status_code == 404


def test_store_create_rejects_unknown_platform_and_category():
    with pytest.raises(ValueError):
        StoreCreate(name="x", main_platforms=["weibo"])
    with pytest.raises(ValueError):
        StoreCreate(name="x", category="bar")


async def test_starter_cards_use_store_name_and_signature():
    user = await _user()
    created = await create_store(StoreCreate(name="泽岚鲜果"), current_user=user)
    plain = await starter_cards(created["id"], locale="zh-CN", current_user=user)
    assert len(plain["items"]) == 3
    assert all("泽岚鲜果" in card["title"] for card in plain["items"])
    assert "「" not in plain["items"][0]["title"]  # no signature yet

    row = await memory_service.write_memory(
        user_id=user["user_id"], workspace_id=user["workspace_id"], scope="LONG_TERM",
        type="SIGNATURE_CASE", value={"summary": "双人果切；分量足"}, owner="USER_CONFIRMED",
    )
    async with get_db_session() as db:
        from db.models.memory import UserMemory
        memory = await db.get(UserMemory, row["id"])
        memory.status = "ACTIVE"
    with_signature = await starter_cards(created["id"], locale="zh-CN", current_user=user)
    assert "「双人果切」" in with_signature["items"][0]["title"]

    english = await starter_cards(created["id"], locale="en-US", current_user=user)
    assert "泽岚鲜果" in english["items"][0]["title"] and "video" in english["items"][0]["title"].lower()

    brief = await store_service.store_brief(user["workspace_id"], user["user_id"])
    assert brief.startswith("门店：泽岚鲜果")


async def test_binding_creates_placeholder_store_and_reports_first_bound():
    user = await _user()
    now = _now()
    async with get_db_session() as db:
        row, first = await store_service.record_platform_binding(
            db, workspace_id=user["workspace_id"], user_id=user["user_id"], site_key="douyin_laike",
            status="bound", identity={"account_name": "泽岚鲜果", "account_id": "7670"}, now=now,
            display_name="抖音来客")
        assert first is True
        assert row.name == "泽岚鲜果" and row.main_platforms == ["douyin_laike"]
        assert row.platform_bindings["douyin_laike"]["bound_at"] == now.isoformat()
    async with get_db_session() as db:
        row, first = await store_service.record_platform_binding(
            db, workspace_id=user["workspace_id"], user_id=user["user_id"], site_key="douyin_laike",
            status="bound", identity={"role": "店长"}, now=_now())
        assert first is False  # already bound: no second bootstrap
        assert row.platform_bindings["douyin_laike"]["account_id"] == "7670"
        assert row.platform_bindings["douyin_laike"]["role"] == "店长"
    async with get_db_session() as db:
        row, first = await store_service.record_platform_binding(
            db, workspace_id=user["workspace_id"], user_id=user["user_id"], site_key="meituan_merchant",
            status="bound", identity={"shop_id": "88"}, now=_now())
        assert first is True and row.main_platforms == ["douyin_laike", "meituan_merchant"]
    public = await store_service.get_store(user["workspace_id"])
    assert public["platformBindings"]["meituan_merchant"]["shopId"] == "88"


async def test_on_desktop_bound_starts_bootstrap_after_commit(monkeypatch):
    user = await _user()
    started: list[tuple[str, str, list[str]]] = []

    async def fake_start(workspace_id, user_id, sites):
        started.append((workspace_id, user_id, sites))

    monkeypatch.setattr(persona_init, "start_after_binding", fake_start)
    account = SimpleNamespace(workspace_id=user["workspace_id"], bound_by_user_id=user["user_id"],
                              external_id="desk:ecd-1", nickname="泽岚鲜果",
                              probe_detail={"display": {"account_name": "泽岚鲜果", "account_id": "7670", "role": "店长"}})
    site = SimpleNamespace(key="douyin_laike", display="抖音来客")
    async with get_db_session() as db:
        await store_service.on_desktop_bound(db, account, site, _now(), user_id="")
        assert started == []  # nothing before commit
    # The after_commit hook schedules a task; let it run.
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == [(user["workspace_id"], user["user_id"], ["douyin_laike"])]

    # A non-store site is ignored entirely.
    async with get_db_session() as db:
        await store_service.on_desktop_bound(db, account, SimpleNamespace(key="douyin_creator", display="创作者中心"),
                                             _now(), user_id=user["user_id"])
    await asyncio.sleep(0)
    assert len(started) == 1


async def test_persona_bootstrap_prompt_and_gating(monkeypatch):
    user = await _user()
    created = await create_store(StoreCreate(name="泽岚鲜果", main_platforms=["douyin_laike"]), current_user=user)
    prompt = persona_init.build_prompt(created, ["douyin_laike"])
    assert "store-persona-init" in prompt and "泽岚鲜果" in prompt and "抖音来客" in prompt

    assert await persona_init._should_start(created, force=False) is True
    async with get_db_session() as db:
        await store_service.set_persona_state(db, user["workspace_id"], status="active")
    active = await store_service.get_store(user["workspace_id"])
    assert await persona_init._should_start(active, force=False) is False
    assert await persona_init._should_start(active, force=True) is True

    async with get_db_session() as db:
        await store_service.set_persona_state(db, user["workspace_id"], status="none", started_at=_now())
    recent = await store_service.get_store(user["workspace_id"])
    assert await persona_init._should_start(recent, force=False) is False  # duplicate trigger within 24h


async def test_start_persona_init_creates_session_and_runs(monkeypatch):
    user = await _user()
    created = await create_store(StoreCreate(name="泽岚鲜果", main_platforms=["douyin_laike"]), current_user=user)
    runs: list[tuple[str, str]] = []

    async def fake_run(session_id, user_id):
        runs.append((session_id, user_id))

    monkeypatch.setattr(persona_init, "_run", fake_run)
    monkeypatch.setattr(persona_init, "AUTO_RUN", True)
    session_id = await persona_init.start_persona_init(user["workspace_id"], user["user_id"], sites=["douyin_laike"])
    assert session_id
    import asyncio
    await asyncio.sleep(0)
    assert runs == [(session_id, user["user_id"])]

    store = await store_service.get_store(user["workspace_id"])
    assert store["personaSessionId"] == session_id and store["personaStartedAt"]
    from session.session import get_messages, get_session
    session = await get_session(session_id, user_id=user["user_id"])
    assert session.title == persona_init.SESSION_TITLE and session.workspace_id == user["workspace_id"]
    messages = await get_messages(session_id, user_id=user["user_id"])
    assert messages and "store-persona-init" in "".join(
        p.get("text", "") for m in messages for p in (
            [x if isinstance(x, dict) else x.model_dump() for x in (m.parts or [])]))
    # Second trigger inside the gap is a no-op.
    assert await persona_init.start_persona_init(user["workspace_id"], user["user_id"], sites=["douyin_laike"]) is None
    assert created["id"] == store["id"]


def test_store_pack_routing_and_notification_kinds():
    from agent.tool_exposure import INTENT_PACKS, ExposureSignals, route_explicit_intent_packs
    assert INTENT_PACKS["store"] == ("creator_context", "desktop_login")
    assert "store" in route_explicit_intent_packs(ExposureSignals(user_task_text="帮我完善门店人设"))
    assert "store" not in route_explicit_intent_packs(ExposureSignals(user_task_text="写一个排序函数"))

    from notifications.inbox import category_for, link_for
    from notifications.templates import render_template
    assert category_for("store_bound") == "session" and category_for("persona_ready") == "session"
    title, body = render_template("store_bound", "泽岚鲜果", "zh-CN")
    assert title == "店铺已连接" and "泽岚鲜果" in body
    assert link_for("persona_ready", workspace_id="ws", session_id="ses")["kind"] == "session"
