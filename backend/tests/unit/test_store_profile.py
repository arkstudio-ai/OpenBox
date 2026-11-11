"""Store profile (门店档案): the API, the first-video backfill, starter cards, the Light prompt."""
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.stores import StoreCreate, StorePatch, create_store, list_stores, patch_store, starter_cards
from db.base import get_db_session
from db.models.store import Store
from db.repository.preference_repo import PgPreferenceRepo
from db.repository.user_repo import PgUserRepo
from light.entries import light_entries
from light.prompt import shop_fragment
from store import service as store_service


async def _user(label: str = "merchant") -> dict:
    suffix = uuid4().hex[:10]
    row = await PgUserRepo().create(
        id=f"store_{label}_{suffix}", username=f"{label}-{suffix}",
        email=f"{label}-{suffix}@example.test", password_hash="unused",
    )
    return {"user_id": row["id"], "role": "user", "workspace_id": row["default_workspace_id"]}


async def _filled_form(user: dict, **fields) -> None:
    record = {"status": "submitted", "workspace_id": user["workspace_id"], "industry": "food",
              "shop_name": "巷口面馆", "headline": "手擀牛肉面", **fields}
    await PgPreferenceRepo().upsert(user["user_id"], extra={"first_video": record})


async def test_create_list_patch_and_conflict():
    user = await _user()
    assert (await list_stores(current_user=user))["items"] == []

    created = await create_store(StoreCreate(name="泽岚鲜果", category="food", headline="现切果盘",
                                             main_platforms=["douyin_laike"]), current_user=user)
    assert created["headline"] == "现切果盘" and created["mainPlatforms"] == ["douyin_laike"]
    assert [s["id"] for s in (await list_stores(current_user=user))["items"]] == [created["id"]]

    with pytest.raises(HTTPException) as conflict:
        await create_store(StoreCreate(name="第二家"), current_user=user)
    assert conflict.value.status_code == 409

    patched = await patch_store(created["id"], StorePatch(name="泽岚鲜果（总店）", headline=None), current_user=user)
    assert patched["name"] == "泽岚鲜果（总店）" and patched["headline"] is None


async def test_other_workspace_reads_as_404():
    owner, other = await _user("owner"), await _user("other")
    created = await create_store(StoreCreate(name="A 店"), current_user=owner)
    for call in (patch_store(created["id"], StorePatch(name="改名"), current_user=other),
                 starter_cards(created["id"], locale="zh-CN", current_user=other)):
        with pytest.raises(HTTPException) as missing:
            await call
        assert missing.value.status_code == 404


async def test_first_read_turns_the_first_video_form_into_a_store_once():
    user = await _user()
    await _filled_form(user)
    first = (await list_stores(current_user=user))["items"]
    assert [(s["name"], s["headline"], s["category"]) for s in first] == [("巷口面馆", "手擀牛肉面", "food")]
    await PgPreferenceRepo().upsert(user["user_id"], extra={"first_video": {
        "workspace_id": user["workspace_id"], "shop_name": "别的店"}})
    assert [s["id"] for s in (await list_stores(current_user=user))["items"]] == [first[0]["id"]]


@pytest.mark.parametrize("fields", [{"shop_name": "  "}, {"workspace_id": "another"}])
async def test_a_form_without_a_name_or_from_another_workspace_makes_no_store(fields):
    user = await _user()
    await _filled_form(user, **fields)
    assert (await list_stores(current_user=user))["items"] == []


async def test_an_unknown_industry_reads_as_other():
    user = await _user()
    await _filled_form(user, industry="nope")
    assert (await list_stores(current_user=user))["items"][0]["category"] == "other"


async def test_a_store_already_there_wins_over_the_form():
    user = await _user()
    winner = await create_store(StoreCreate(name="先到的店"), current_user=user)
    await _filled_form(user)
    assert (await store_service.record_first_video_shop(
        workspace_id=user["workspace_id"], user_id=user["user_id"], shop_name="后到的店",
        headline=None, industry="food"))["id"] == winner["id"]


async def test_starter_cards_use_the_name_and_headline_and_open_real_entries():
    user = await _user()
    created = await create_store(StoreCreate(name="巷口面馆", headline="手擀牛肉面，二十年老汤"), current_user=user)
    body = await starter_cards(created["id"], locale="zh-CN", current_user=user)
    assert len(body["items"]) == 3
    assert body["items"][0]["title"] == "给巷口面馆的招牌菜「手擀牛肉面」做一条短视频"
    assert all("巷口面馆" in card["title"] for card in body["items"])
    known = {entry.id for entry in light_entries().entries}
    assert {card["entry"] for card in body["items"]} <= known


@pytest.mark.parametrize("category", ["food", "beauty", "retail", "other"])
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_starter_cards_read_cleanly_without_a_headline(category, locale):
    user = await _user()
    created = await create_store(StoreCreate(name="A", category=category), current_user=user)
    async with get_db_session() as db:
        row = await db.get(Store, created["id"])
        cards = store_service.starter_cards(row, [], locale=locale)
    assert all("{" not in card["title"] and "「」" not in card["title"] and "  " not in card["title"] for card in cards)


async def test_the_light_prompt_reads_the_store_not_the_form():
    user = await _user()
    await create_store(StoreCreate(name="改过名的店", headline="现切果盘"), current_user=user)
    await _filled_form(user)
    fragment = await shop_fragment(user["user_id"], user["workspace_id"])
    assert "店名「改过名的店」" in fragment and "主打「现切果盘」" in fragment and "巷口面馆" not in fragment
    assert await shop_fragment(user["user_id"]) == ""
