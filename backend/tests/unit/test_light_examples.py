"""The Light reference example library: matching, the prompt section, counting, the admin console."""
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from api.admin_examples import router
from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.light_example import LightExample
from db.models.question import QuestionCheckpoint
from db.repository.user_repo import PgUserRepo
from light import examples
from light.prompt import light_mode_fragment
from light.taxonomy import taxonomy


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
async def empty_library():
    """The test database is shared; an example left behind would show up in other Light prompts."""
    yield
    from sqlalchemy import delete
    async with get_db_session() as db:
        await db.execute(delete(LightExample))
        await db.commit()
    examples._COUNTED.clear()


async def _user(role="user"):
    uid = uuid4().hex
    return await PgUserRepo().create(id=uid, username="lx" + uid[:8], password_hash=None, role=role)


def _client(user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"], "role": user["role"]}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _example(**fields) -> LightExample:
    row = LightExample(id="lex_" + uuid4().hex[:12], entry_id="video", industry="food", shop_type="noodle",
                       tags=[], rating=3, status="active", title="样例", notes="", recipe="", inputs=[],
                       outputs=[], source={}, created_at=_now(), updated_at=_now(), use_count=0)
    for key, value in fields.items():
        setattr(row, key, value)
    async with get_db_session() as db:
        db.add(row)
        await db.commit()
    return row


async def _asset(user_id, workspace_id, name, mime):
    asset_id = "asset_" + uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=user_id, workspace_id=workspace_id, name=name, mime=mime, size=1,
                         oss_key=f"assets/{user_id}/{asset_id}/{name}", status="ready", source="agent",
                         transient=False, is_deleted=False, created_at=_now()))
        await db.commit()
    return asset_id


def test_taxonomy_matches_the_first_video_industries_and_guesses_a_shop_type():
    tax = taxonomy()
    assert tax.entry_ids() == ["video", "menu", "poster", "footage_edit"]
    assert tax.industry_ids() == ["food", "beauty", "retail", "other"]
    for industry in tax.industries:
        assert "other" in tax.shop_type_ids(industry.id)
        assert len(industry.shop_types) >= (5 if industry.id != "other" else 1)
    assert tax.guess_shop_type("food", "巷口面馆", "红烧牛肉面") == "noodle"
    assert tax.guess_shop_type("beauty", "小美美甲") == "nail"
    assert tax.guess_shop_type("food", "老王家") is None and tax.guess_shop_type(None, "面馆") is None
    assert tax.label("food", "hotpot") == "餐饮 / 火锅" and tax.label("beauty") == "美业"


async def test_pick_falls_back_level_by_level_and_prefers_tags_then_rating():
    entry = "pick_" + uuid4().hex[:6]
    exact_low = await _example(entry_id=entry, industry="food", shop_type="noodle", rating=2, title="同店型低分")
    exact_high = await _example(entry_id=entry, industry="food", shop_type="noodle", rating=5, title="同店型高分")
    tagged = await _example(entry_id=entry, industry="food", shop_type="noodle", rating=1, tags=["探店"], title="同店型带标签")
    same_industry = await _example(entry_id=entry, industry="food", shop_type="hotpot", rating=5, title="同行业")
    generic = await _example(entry_id=entry, industry="other", shop_type="other", rating=4, title="通用")
    off = await _example(entry_id=entry, industry="food", shop_type="noodle", rating=5, status="disabled", title="停用")
    await _example(entry_id="other_entry", industry="food", shop_type="noodle", rating=5)

    exact = await examples.pick(entry, "food", "noodle", ["探店"], limit=3)
    assert [r.id for r in exact] == [tagged.id, exact_high.id, exact_low.id]
    assert off.id not in {r.id for r in exact}
    assert [r.id for r in await examples.pick(entry, "food", "noodle", limit=2)] == [exact_high.id, exact_low.id]
    # No bbq example: the whole industry, best first (the tie between two 5s goes to the newer).
    assert [r.id for r in await examples.pick(entry, "food", "bbq")] == [same_industry.id, exact_high.id, exact_low.id]
    assert [r.id for r in await examples.pick(entry, "retail", "fresh")] == [generic.id]
    assert [r.id for r in await examples.pick(entry, None, None)] == [generic.id]
    only_beauty = "pick2_" + uuid4().hex[:6]
    beauty = await _example(entry_id=only_beauty, industry="beauty", shop_type="nail")
    assert [r.id for r in await examples.pick(only_beauty, "food", "noodle")] == [beauty.id]
    assert await examples.pick("nothing_" + uuid4().hex[:6], "food", "noodle") == []


async def test_fragment_renders_examples_for_this_shop_and_counts_once_per_session(monkeypatch):
    from db.repository.preference_repo import PgPreferenceRepo
    from session.session import create_session, update_session
    monkeypatch.setattr("light.media.sign_media", lambda value: value and value.replace("oss:", "https://signed/"))
    user = await _user()
    ws = user["default_workspace_id"]
    await PgPreferenceRepo().upsert(user["id"], extra={"first_video": {
        "status": "ready", "industry": "food", "workspace_id": ws, "shop_name": "巷口面馆", "headline": "红烧牛肉面"}})
    guided = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=ws)
    guided = await update_session(guided.id, user_id=user["id"], light_entry="video")
    before = await light_mode_fragment(user["id"], guided)
    assert "参考样例" not in before

    row = await _example(entry_id="video", industry="food", shop_type="noodle", rating=5, title="面馆探店样例",
                         notes="先门头再上面，收尾给价格", recipe="画面：牛肉面特写\n时长：15 秒竖屏",
                         outputs=[{"asset_id": "a1", "oss_key": "assets/u/a1/final.mp4", "name": "final.mp4", "mime": "video/mp4"}])
    fragment = await light_mode_fragment(user["id"], guided)
    section = fragment.split("参考样例", 1)[1]
    assert "「面馆探店样例」（餐饮 / 面馆 / 粉店，评分 5/5）：先门头再上面，收尾给价格" in section
    assert "当时的方案卡/提示词：画面：牛肉面特写 时长：15 秒竖屏" in section
    assert "final.mp4（https://signed/assets/u/a1/final.mp4）" in section
    assert "规则：1) 只学样例的结构" in section and "2) 一律换成这家店的真实信息" in section and "3) 老板说要不一样" in section
    assert fragment.index("店名「巷口面馆」") < fragment.index("参考样例")

    await light_mode_fragment(user["id"], guided)
    async with get_db_session() as db:
        assert (await db.get(LightExample, row.id)).use_count == 1
    other = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=ws)
    other = await update_session(other.id, user_id=user["id"], light_entry="video")
    await light_mode_fragment(user["id"], other)
    async with get_db_session() as db:
        assert (await db.get(LightExample, row.id)).use_count == 2
    plain = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=ws)
    assert await examples.examples_fragment(user["id"], plain) == ""


async def test_hint_counts_active_examples_per_entry():
    entry = "hint_" + uuid4().hex[:6]
    assert await examples.examples_hint([entry]) == ""
    assert await examples.examples_hint(["video"], industry="no_such_industry") == ""
    await _example(entry_id="menu", industry="beauty", shop_type="nail")
    await _example(entry_id="menu", industry="beauty", shop_type="nail", status="disabled")
    hint = await examples.examples_hint(["menu", "poster"], industry="beauty")
    assert hint.startswith("这类活有同行业样例可参考（做菜单 ") and "条" in hint and "做海报" not in hint


async def test_prefill_reads_inputs_outputs_recipe_and_the_shop_from_a_session():
    from db.repository.preference_repo import PgPreferenceRepo
    from models.message import FilePart, FileRelation, ToolPartData
    from session.session import create_assistant_message, create_session, create_user_message, save_part, update_session
    user = await _user()
    ws = user["default_workspace_id"]
    await PgPreferenceRepo().upsert(user["id"], extra={"first_video": {
        "status": "ready", "industry": "food", "shop_name": "巷口面馆", "headline": "红烧牛肉面"}})
    session = await create_session(model="m", agent="build", title="做一条视频", user_id=user["id"], workspace_id=ws)
    await update_session(session.id, user_id=user["id"], light_entry="video")
    um = await create_user_message(session_id=session.id, text="看这张", user_id=user["id"])
    photo = await _asset(user["id"], ws, "photo.jpg", "image/jpeg")
    clip = await _asset(user["id"], ws, "final.mp4", "video/mp4")
    middle = await _asset(user["id"], ws, "segment.mp4", "video/mp4")
    await save_part(FilePart(path="/workspace/photo.jpg", mime_type="image/jpeg", asset_id=photo,
                             relation=FileRelation(role="input", kind="user_attachment"),
                             session_id=session.id, message_id=um.id), is_new=True, user_id=user["id"])
    am = await create_assistant_message(session.id, um.id, user_id=user["id"])
    await save_part(ToolPartData(tool="video_generate", call_id="c1", status="completed",
                                 input={"action": "submit", "prompt": "竖屏美食短片：牛肉面特写"},
                                 metadata={"job_id": "job_1"}, session_id=session.id, message_id=am.id),
                    is_new=True, user_id=user["id"])
    await save_part(FilePart(path="/workspace/segment.mp4", mime_type="video/mp4", asset_id=middle,
                             relation=FileRelation(role="intermediate", kind="video_segment"),
                             session_id=session.id, message_id=am.id), is_new=True, user_id=user["id"])
    await save_part(FilePart(path="/workspace/final.mp4", mime_type="video/mp4", asset_id=clip,
                             relation=FileRelation(role="final", kind="video_final"),
                             session_id=session.id, message_id=am.id), is_new=True, user_id=user["id"])
    async with get_db_session() as db:
        db.add(QuestionCheckpoint(id="q_" + uuid4().hex[:8], session_id=session.id, user_id=user["id"], generation=1,
                                  status="answered", draft=[], continuation={}, applied=True,
                                  questions=[{"question": "画面：牛肉面特写\n时长：15 秒竖屏\n店名：巷口面馆\n\n就这样？",
                                              "detail": {"light": {"step_id": "confirm"}}},
                                             {"question": "要哪种？", "detail": {"light": {"step_id": "topic"}}}],
                                  created_at=_now(), updated_at=_now()))
        await db.commit()

    draft = await examples.prefill(session.id)
    assert draft["entry_id"] == "video" and draft["industry"] == "food" and draft["shop_type"] == "noodle"
    assert draft["title"] == "做一条视频"
    assert [i["asset_id"] for i in draft["inputs"]] == [photo] and draft["inputs"][0]["oss_key"].endswith("photo.jpg")
    assert [o["asset_id"] for o in draft["outputs"]] == [clip] and draft["outputs"][0]["mime"] == "video/mp4"
    assert draft["recipe"].startswith("方案卡：\n画面：牛肉面特写") and "生成提示词：\n竖屏美食短片：牛肉面特写" in draft["recipe"]
    assert draft["source"] == {"session_id": session.id, "job_id": "job_1", "user_id": user["id"], "workspace_id": ws,
                               "shop_name": "巷口面馆", "headline": "红烧牛肉面"}
    assert [o["asset_id"] for o in (await examples.prefill(session.id, middle))["outputs"]] == [middle]
    assert await examples.prefill("no_such_session") is None


async def test_console_requires_a_live_admin_and_round_trips_an_example():
    admin = await _user(role="admin")
    plain = await _user()
    body = {"entry_id": "video", "industry": "food", "shop_type": "noodle", "tags": ["探店", "探店", ""], "rating": 5,
            "title": "面馆样例", "notes": "好", "recipe": "画面…", "inputs": [], "outputs": [], "source": {"session_id": "s1", "x": 1}}
    async with _client(plain) as client:
        assert (await client.get("/api/admin/examples")).status_code == 403
        assert (await client.post("/api/admin/examples", json=body)).status_code == 403
        assert (await client.post("/api/admin/examples/prefill", json={"session_id": "s"})).status_code == 403
    async with _client(admin) as client:
        assert (await client.get("/api/admin/examples/taxonomy")).json()["industries"][0]["id"] == "food"
        bad = await client.post("/api/admin/examples", json={**body, "shop_type": "nail"})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "EXAMPLE_INVALID"
        missing = await client.post("/api/admin/examples", json={**body, "outputs": [{"asset_id": "nope"}]})
        assert missing.status_code == 422
        created = (await client.post("/api/admin/examples", json=body)).json()
        assert created["tags"] == ["探店"] and created["source"] == {"session_id": "s1"} and created["use_count"] == 0
        assert created["created_by"] == admin["id"] and created["shop_type_label"] == "餐饮 / 面馆 / 粉店"
        clip = await _asset(plain["id"], plain["default_workspace_id"], "final.mp4", "video/mp4")
        updated = (await client.put(f"/api/admin/examples/{created['id']}", json={
            **body, "rating": 4, "outputs": [{"asset_id": clip}]})).json()
        assert updated["rating"] == 4 and updated["outputs"][0]["name"] == "final.mp4" and updated["outputs"][0]["oss_key"]
        off = (await client.post(f"/api/admin/examples/{created['id']}/status", json={"status": "disabled"})).json()
        assert off["status"] == "disabled"
        listed = (await client.get("/api/admin/examples?entry_id=video&industry=food&shop_type=noodle&status=disabled")).json()
        assert created["id"] in {item["id"] for item in listed["items"]} and listed["total"] >= 1
        assert created["id"] not in {item["id"] for item in (await client.get("/api/admin/examples?status=active&entry_id=video")).json()["items"]}
        assert (await client.get("/api/admin/examples/nope")).status_code == 404
        assert (await client.post("/api/admin/examples/prefill", json={"session_id": "nope"})).status_code == 404
        assert (await client.get("/api/admin/examples/assets/nope/url")).status_code == 404
