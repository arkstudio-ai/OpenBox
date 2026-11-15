"""The Light example card (docs/LIGHT_EXAMPLE_PICK_PLAN.md): which examples it shows, what the answer
writes, and what the prompt holds afterwards."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from agent import inbox
from agent.driver import reserve_run
from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.light_example import LightExample
from db.models.part import Part
from db.models.session import Session
from db.repository.preference_repo import PgPreferenceRepo
from light import examples
from light.entries import light_entries
from light.prompt import entry_fragment, light_mode_fragment
from light.taxonomy import taxonomy
from models.message import ToolPartData
from question import cards, question as q, runtime
from question.continuation import apply_answers
from session.session import create_assistant_message, get_session, save_part, update_message_info, update_session
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, card, conversation
from tool import example_pick as ep


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Oss:
    def presign_get(self, key, expires_sec=3600, download_name=None, *, internal=False):
        return f"https://signed/{key}"

    def _presign(self, method, key, expires_sec, subresource=None, internal=False):
        return f"https://signed/{key}?{(subresource or {}).get('x-oss-process', '')}"


@pytest.fixture(autouse=True)
async def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "example-pick-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr("core.oss.get_oss", lambda: _Oss())
    monkeypatch.setattr("light.media.sign_media", lambda value: value and value.replace("oss:", "https://signed/"))

    async def no_vision(row, ctx):  # no test reaches a vision model unless it sets its own reader
        raise ValueError("no vision model in tests")
    monkeypatch.setattr("light.example_look.read", no_vision)
    MAIN_MESSAGES.clear()
    yield
    from sqlalchemy import delete
    async with get_db_session() as db:
        await db.execute(delete(LightExample))
        await db.commit()
    examples._COUNTED.clear()


async def _example(entry_id, title, *, scene=None, industry="food", shop_type="noodle", rating=3, age=0,
                   mime="image/png", status="active", outputs=None, **fields) -> LightExample:
    example_id = "lex_" + uuid4().hex[:12]
    name = "final.mp4" if mime.startswith("video/") else "final.png"
    row = LightExample(id=example_id, entry_id=entry_id, industry=industry, shop_type=shop_type, scene=scene,
                       tags=[], rating=rating, status=status, title=title, notes=f"{title}的评语", recipe=f"{title}的方案卡",
                       inputs=[], source={}, use_count=0, pick_count=0,
                       outputs=[{"asset_id": "a_" + example_id, "oss_key": f"assets/u/{example_id}/{name}",
                                 "name": name, "mime": mime}] if outputs is None else outputs,
                       created_at=_now() - timedelta(minutes=age), updated_at=_now())
    for key, value in fields.items():
        setattr(row, key, value)
    async with get_db_session() as db:
        db.add(row)
        await db.commit()
    return row


async def _guided(entry_id="poster", **fields):
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "做海报", visibility="private")
    await update_session(session.id, user_id=owner, light_entry=entry_id, **fields)
    await PgPreferenceRepo().upsert(owner, extra={"first_video": {
        "status": "ready", "industry": "food", "shop_name": "巷口面馆", "headline": "红烧牛肉面"}})
    return owner, session


async def _call(session, owner, **arguments):
    """One ``example_pick`` call in a real turn: its result, or the id of the card it waits on."""
    await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup", prompt="团购",
                                  origin="human", origin_ref={"actor_user_id": owner})
    lease = await reserve_run(session.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    ticket = await runtime.start_run(session.id, owner, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    result, request_id = None, None
    try:
        fence = (session.id, lease.run_id, lease.generation)
        message = await create_assistant_message(session.id, batch.messages[0].id, agent="build",
                                                 model_id="test/model", user_id=owner, run_fence=fence)
        part = ToolPartData(session_id=session.id, message_id=message.id, tool="example_pick", status="running")
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        ctx = SimpleNamespace(session_id=session.id, user_id=owner, message_id=message.id, part_id=part.id)
        try:
            result = await ep.execute(ep.ExamplePickArgs(**arguments), ctx)
        except q.QuestionSuspended as suspended:
            request_id = suspended.request_id
            message.finish = "waiting_input"
            await update_message_info(message, user_id=owner, run_fence=fence)
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input" if request_id else "idle")
    return result, request_id


async def _row(session_id):
    async with get_db_session() as db:
        return await db.get(Session, session_id)


# --- the library -------------------------------------------------------------------------------

def test_taxonomy_names_the_scenes_of_the_three_entries():
    tax = taxonomy()
    assert tax.scene_ids("poster") == ["new_item", "holiday", "opening", "groupbuy"]
    assert tax.scene_ids("footage_edit") == ["visit", "new_item", "promo", "daily"]
    assert tax.scene_ids("menu") == ["menu_mono", "menu_red", "menu_chalk", "menu_dark"]
    assert tax.scene_ids("video") == [] and tax.scene_ids("nothing") == []
    assert tax.scene_label("poster", "groupbuy") == "团购" and tax.scene_label("poster", "zzz") == "zzz"
    # A menu's scene is its gallery style, so the pick made before the chat needs no second question.
    menu = light_entries().entry("menu")
    assert [sample.id for sample in menu.subtypes[0].samples] == tax.scene_ids("menu")


async def test_candidates_put_the_scene_first_then_the_shop_and_fill_up_from_other_scenes():
    entry = "cand_" + uuid4().hex[:6]
    same_low = await _example(entry, "同类型低分", scene="groupbuy", rating=2)
    same_high = await _example(entry, "同类型高分", scene="groupbuy", rating=5, industry="beauty", shop_type="nail")
    other_scene = await _example(entry, "别的类型同店型", scene="holiday", rating=5)
    generic = await _example(entry, "没分类通用", industry="other", shop_type="other", rating=5)
    await _example(entry, "停用", scene="groupbuy", rating=5, status="disabled")
    await _example(entry, "没有成品", scene="groupbuy", rating=5, outputs=[])
    await _example(entry, "只有文档", scene="groupbuy", rating=5,
                   outputs=[{"asset_id": "d", "oss_key": "assets/u/d/menu.pdf", "name": "menu.pdf", "mime": "application/pdf"}])

    # Same scene first (the shop's own kind before another industry), then the other scenes fill the row.
    rows = await examples.candidates(entry, "groupbuy", "food", "noodle")
    assert [r.id for r in rows] == [same_low.id, same_high.id, other_scene.id]
    assert [r.id for r in await examples.candidates(entry, "groupbuy", "food", "noodle", limit=4)][-1] == generic.id
    # No scene named: how well the shop fits, then rating.
    assert [r.id for r in await examples.candidates(entry, None, "food", "noodle")] == [other_scene.id, same_low.id, generic.id]
    # The owner's last pick leads when it is still there; a stale id changes nothing.
    assert [r.id for r in await examples.candidates(entry, "groupbuy", "food", "noodle", first=generic.id)][0] == generic.id
    assert [r.id for r in await examples.candidates(entry, "groupbuy", "food", "noodle", first="lex_gone")][0] == same_low.id
    assert await examples.candidates("empty_" + uuid4().hex[:6], "groupbuy", "food", "noodle") == []


async def test_pick_prefers_the_conversations_scene_within_a_level():
    entry = "scene_" + uuid4().hex[:6]
    high = await _example(entry, "高分别的类型", scene="holiday", rating=5)
    low = await _example(entry, "低分同类型", scene="opening", rating=1)
    assert [r.id for r in await examples.pick(entry, "food", "noodle")] == [high.id, low.id]
    assert [r.id for r in await examples.pick(entry, "food", "noodle", scene="opening")] == [low.id, high.id]


async def test_preview_signs_the_piece_and_a_still_of_it():
    image = await _example("poster", "海报样例")
    clip = await _example("footage_edit", "剪辑样例", mime="video/mp4")
    shown = examples.preview(image)
    assert shown["example_id"] == image.id and shown["title"] == "海报样例" and shown["mime"] == "image/png"
    assert shown["media_url"] == f"https://signed/assets/u/{image.id}/final.png"
    assert shown["poster_url"].endswith("?image/resize,w_720/format,jpg/quality,q_85")
    assert examples.preview(clip)["poster_url"].endswith("?video/snapshot,t_1000,f_jpg,w_720,m_fast,ar_auto")
    assert examples.preview(await _example("poster", "空", outputs=[])) is None


def test_last_pick_is_remembered_per_entry_and_scene():
    extra = examples.with_pick({"ui_language": "vi-VN"}, "poster", "groupbuy", "lex_1")
    extra = examples.with_pick(extra, "poster", None, "lex_2")
    extra = examples.with_pick(extra, "menu", "menu_red", "lex_3")
    assert extra["ui_language"] == "vi-VN"
    assert examples.last_pick(extra, "poster", "groupbuy") == "lex_1" and examples.last_pick(extra, "poster", None) == "lex_2"
    assert examples.last_pick(extra, "menu", "menu_red") == "lex_3"
    assert examples.last_pick(extra, "poster", "holiday") is None and examples.last_pick(None, "poster", None) is None
    assert examples.last_pick({"light_example_picks": "broken"}, "poster", None) is None


# --- the card ----------------------------------------------------------------------------------

async def test_card_shows_the_examples_with_previews_and_lights_the_example_step():
    owner, session = await _guided("poster")
    first = await _example("poster", "团购大字报", scene="groupbuy", rating=5)
    second = await _example("poster", "团购大字报", scene="groupbuy", rating=4)  # the same title twice
    third = await _example("poster", "节日海报", scene="holiday", rating=5)
    result, request_id = await _call(session, owner, scene="groupbuy")
    assert result is None and request_id
    row = await card(request_id)
    [question] = row.questions
    detail = question["detail"]
    assert detail["kind"] == ep.KIND and detail["present"] == "inline" and detail["light"] == {"step_id": "example"}
    assert question["custom"] is False and question["header"] == "挑样例"
    assert [option["label"] for option in question["options"]] == ["团购大字报", "团购大字报 (2)", "节日海报", "都不太像，直接做"]
    assert [option["id"] for option in detail["options"]] == [first.id, second.id, third.id, "none"]
    assert [option["role"] for option in detail["options"]] == ["choose", "choose", "choose", "cancel"]
    shown = detail["options"][0]
    assert shown["example_id"] == first.id and shown["mime"] == "image/png" and shown["last_pick"] is False
    assert shown["media_url"].startswith("https://signed/") and "image/resize" in shown["poster_url"]
    assert shown["scene_label"] == "团购" and detail["options"][2]["scene_label"] == "节日活动"
    assert row.continuation["card"]["payload"] == {"entry_id": "poster", "scene": "groupbuy",
                                                   "example_ids": [first.id, second.id, third.id]}
    assert (await _row(session.id)).light_scene == "groupbuy"
    assert cards.voice_answerable(row.continuation) is False


async def test_picking_an_example_keeps_it_on_the_conversation_and_the_prompt_holds_only_that_one():
    owner, session = await _guided("poster")
    liked = await _example("poster", "团购大字报", scene="groupbuy", rating=3)
    other = await _example("poster", "团购实拍", scene="groupbuy", rating=5)
    before = await light_mode_fragment(owner, await get_session(session.id, user_id=owner))
    assert "「团购大字报」" in before and "「团购实拍」" in before  # nobody chose yet: the library's own pick

    _, request_id = await _call(session, owner, scene="groupbuy")
    await q.reply(request_id, [["团购大字报"]], owner)
    assert await apply_answers(session.id, owner) is not None
    saved = await _row(session.id)
    assert saved.light_example_id == liked.id and saved.light_scene == "groupbuy"
    row = await card(request_id)
    async with get_db_session() as db:
        part = await db.get(Part, row.part_id)
        assert (await db.get(LightExample, liked.id)).pick_count == 1
        assert (await db.get(LightExample, other.id)).pick_count == 0
    assert part.data["status"] == "completed" and part.data["title"] == "照「团购大字报」做"
    assert part.data["metadata"]["card"] == {"kind": ep.KIND, "option": liked.id}
    assert part.data["metadata"]["example_id"] == liked.id
    assert "团购大字报的方案卡" in part.data["output"] and "只学样例的结构" in part.data["output"]

    after = await light_mode_fragment(owner, await get_session(session.id, user_id=owner))
    section = after.split("参考样例", 1)[1]
    assert "老板在样例卡上亲自选的这一条" in section and "「团购大字报」" in section and "「团购实拍」" not in section
    extra = (await PgPreferenceRepo().get(owner))["extra"]
    assert examples.last_pick(extra, "poster", "groupbuy") == liked.id
    assert extra["first_video"]["shop_name"] == "巷口面馆"  # the rest of the preferences is untouched

    # 「再做一条」 in the same conversation reuses the pick without a second card.
    again, waiting = await _call(session, owner, scene="groupbuy")
    assert waiting is None and again.metadata == {"example_id": liked.id, "example_title": "团购大字报", "reused": True}
    # 「换个样子」 shows the card again, last pick first.
    _, second_card = await _call(session, owner, scene="groupbuy", again=True)
    detail = (await card(second_card)).questions[0]["detail"]
    assert detail["options"][0]["id"] == liked.id and detail["options"][0]["last_pick"] is True
    assert (await card(second_card)).questions[0]["options"][0]["description"] == "上次选的"


@pytest.mark.parametrize("answer", ["decline", "dismiss"])
async def test_choosing_none_means_no_example_in_the_prompt_and_no_second_card(answer):
    owner, session = await _guided("poster")
    await _example("poster", "团购大字报", scene="groupbuy")
    _, request_id = await _call(session, owner, scene="groupbuy")
    if answer == "decline":
        await q.reply(request_id, [["都不太像，直接做"]], owner)
    else:
        await q.reject(request_id, owner)
    assert await apply_answers(session.id, owner) is not None
    assert (await _row(session.id)).light_example_id == examples.DECLINED
    row = await card(request_id)
    async with get_db_session() as db:
        part = await db.get(Part, row.part_id)
    assert part.data["title"] == "不参考样例" and part.data["metadata"]["card"] == {"kind": ep.KIND, "option": None}
    assert "参考样例" not in await light_mode_fragment(owner, await get_session(session.id, user_id=owner))
    result, waiting = await _call(session, owner, scene="groupbuy")
    assert waiting is None and result.metadata == {"example_id": None}
    assert "already chose none" in result.output


async def test_no_card_without_examples_outside_a_guided_chat_or_with_nobody_there(monkeypatch):
    owner, session = await _guided("poster")
    result, waiting = await _call(session, owner, scene="groupbuy")  # an empty library
    assert waiting is None and "no finished example" in result.output
    assert (await _row(session.id)).light_example_id is None  # nothing was asked, so nothing is recorded

    await _example("poster", "团购大字报", scene="groupbuy")
    monkeypatch.setattr("tool.video_model_card.unattended", lambda session, session_id: True)
    result, waiting = await _call(session, owner, scene="holiday")
    assert waiting is None and "nobody is here" in result.output
    assert (await _row(session.id)).light_scene == "holiday"  # the scene still steers the automatic pick
    monkeypatch.undo()
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)

    plain_owner, _, workspace, main = await assistant()
    plain = await conversation(plain_owner, workspace, main, "普通对话", visibility="private")
    result, waiting = await _call(plain, plain_owner, scene="groupbuy")
    assert waiting is None and "not a guided Light conversation" in result.output


async def test_an_unknown_scene_is_dropped_and_a_disabled_pick_falls_back(monkeypatch):
    owner, session = await _guided("poster")
    liked = await _example("poster", "团购大字报", scene="groupbuy")
    _, request_id = await _call(session, owner, scene="not_a_scene")
    assert (await card(request_id)).continuation["card"]["payload"]["scene"] is None
    assert (await _row(session.id)).light_scene is None
    await q.reply(request_id, [["团购大字报"]], owner)
    assert await apply_answers(session.id, owner) is not None
    async with get_db_session() as db:
        (await db.get(LightExample, liked.id)).status = "disabled"
        await db.commit()
    spare = await _example("poster", "新样例")
    # The picked one was taken out of the library: the prompt goes back to the library's own pick.
    fragment = await light_mode_fragment(owner, await get_session(session.id, user_id=owner))
    assert "「新样例」" in fragment and "「团购大字报」" not in fragment and "亲自选的" not in fragment
    _, again = await _call(session, owner)
    assert [o["id"] for o in (await card(again)).questions[0]["detail"]["options"]] == [spare.id, "none"]


async def test_card_speaks_the_interface_language():
    owner, session = await _guided("footage_edit")
    await PgPreferenceRepo().upsert(owner, extra={"ui_language": "vi-VN"})
    await _example("footage_edit", "探店快剪", scene="visit", mime="video/mp4")
    _, request_id = await _call(session, owner, scene="visit")
    [question] = (await card(request_id)).questions
    assert question["header"] == "Chọn mẫu" and question["options"][-1]["label"] == "Không mẫu nào, cứ làm luôn"
    assert question["options"][0]["label"] == "探店快剪"  # the library's own title
    assert question["detail"]["options"][0]["mime"] == "video/mp4"


# --- wiring ------------------------------------------------------------------------------------

def test_entries_skills_and_the_registry_carry_the_example_step():
    from pathlib import Path
    from tool.registry import get_tool, register_builtin_tools
    for lang in ("zh-CN", "en-US", "vi-VN"):
        config = light_entries(lang)
        assert config.entry("poster").step_ids() == ["purpose", "example", "copy", "title", "deliver"]
        assert config.entry("menu").step_ids() == ["style", "example", "items", "review", "layout", "deliver"]
        assert config.entry("footage_edit").step_ids() == ["type", "example", "upload", "plan", "compose", "deliver"]
        assert "example" not in config.entry("video").step_ids()
        # The poster's look now comes from the example card, not from a fixed style page before the chat.
        assert config.entry("poster").subtypes == [] and config.entry("poster").opening_card.text_with_sample is None
    assert {light_entries(lang).entry("poster").steps[1].label for lang in ("zh-CN", "en-US", "vi-VN")} == {
        "挑样例", "Pick a look", "Chọn mẫu"}
    assert "example_pick" in entry_fragment("poster") and "example_pick" not in entry_fragment("video")
    skills = Path(__file__).resolve().parents[2] / ".openbox" / "skills"
    for name, scenes in (("poster-maker", taxonomy().scene_ids("poster")), ("menu-maker", taxonomy().scene_ids("menu")),
                         ("footage-edit", taxonomy().scene_ids("footage_edit"))):
        text = (skills / name / "SKILL.md").read_text(encoding="utf-8")
        assert "  - example_pick\n" in text.split("---")[1] and "| `example` |" in text
        assert all(f"`{scene}`" in text for scene in scenes), name
    register_builtin_tools(load_custom=False)
    assert get_tool("example_pick") is not None
    assert q.QUESTION_TOOL_CONTINUATIONS["example_pick"] == "question"
    # A registered tool the build agent may not use is one the model can neither call nor discover
    # (gw2 2026-11-15: capability_search("example_pick") found nothing; image_to_pdf had the same gap).
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    from agent.tool_exposure import INTENT_PACKS
    for tool_id in ("example_pick", "image_to_pdf"):
        assert tool_id in AGENTS["build"].tools and tool_id in BUILD_ONLY_WORKFLOW_TOOLS
        assert tool_id in INTENT_PACKS["image"]
    assert "capability_search" in entry_fragment("menu")
    assert cards.get(ep.KIND).present == "inline"


async def test_start_keeps_the_menu_style_as_the_scene(monkeypatch):
    from light import mode
    owner, _, workspace, _ = await assistant()

    async def exchange(*args, **kwargs):
        return None
    monkeypatch.setattr("light.chat.exchange", exchange)
    started = await mode.start(user_id=owner, workspace_id=workspace, entry_id="menu",
                               subtype="menu_style", sample="menu_red")
    assert (await _row(started["session_id"])).light_scene == "menu_red"
    plain = await mode.start(user_id=owner, workspace_id=workspace, entry_id="menu")
    assert (await _row(plain["session_id"])).light_scene is None


# --- the App's refresh and the console ---------------------------------------------------------

async def test_preview_endpoint_refreshes_links_for_an_active_example_only():
    from api.light import router
    owner, _, workspace, _ = await assistant()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": owner, "workspace_id": workspace}
    row = await _example("poster", "团购大字报")
    off = await _example("poster", "停用", status="disabled")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(f"/api/light/examples/{row.id}/preview")
        assert ok.status_code == 200 and ok.json()["example_id"] == row.id and ok.json()["media_url"].startswith("https://signed/")
        assert (await client.get(f"/api/light/examples/{off.id}/preview")).status_code == 404
        assert (await client.get("/api/light/examples/none/preview")).status_code == 404


async def test_console_takes_a_scene_filters_by_it_and_counts_the_cells():
    from api.admin_examples import router
    from db.repository.user_repo import PgUserRepo
    uid = uuid4().hex
    admin = await PgUserRepo().create(id=uid, username="ep" + uid[:8], password_hash=None, role="admin")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": admin["id"], "role": admin["role"]}
    await _example("poster", "团购一", scene="groupbuy")
    await _example("poster", "团购二", scene="groupbuy")
    await _example("poster", "没分类")
    await _example("poster", "停用", scene="groupbuy", status="disabled")
    body = {"entry_id": "poster", "industry": "food", "shop_type": "noodle", "title": "开业海报", "scene": "opening"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/admin/examples", json=body)
        assert created.status_code == 201, created.text
        assert created.json()["scene"] == "opening" and created.json()["scene_label"] == "开业" and created.json()["pick_count"] == 0
        bad = await client.post("/api/admin/examples", json={**body, "scene": "visit"})  # a cut's scene on a poster
        assert bad.status_code == 422
        listed = (await client.get("/api/admin/examples", params={"entry_id": "poster", "scene": "groupbuy"})).json()
        assert sorted(item["title"] for item in listed["items"]) == ["停用", "团购一", "团购二"]
        unset = (await client.get("/api/admin/examples", params={"entry_id": "poster", "scene": "none"})).json()
        assert [item["title"] for item in unset["items"]] == ["没分类"]
        coverage = (await client.get("/api/admin/examples/coverage")).json()
        assert (await client.get("/api/admin/examples/taxonomy")).json()["entries"][2]["scenes"][3] == {"id": "groupbuy", "label": "团购"}
    assert coverage["target"] == 3
    poster = next(row for row in coverage["entries"] if row["entry_id"] == "poster")
    assert {cell["scene"]: cell["count"] for cell in poster["scenes"]} == {
        "new_item": 0, "holiday": 0, "opening": 1, "groupbuy": 2}  # active only
    assert poster["unclassified"] == 1 and poster["total"] == 4
    assert next(row for row in coverage["entries"] if row["entry_id"] == "video")["scenes"] == []


# --- the look of a picked example (light/example_look.py) --------------------------------------

LOOK = ("版式：横版，左右两栏，左栏是招牌区\n配色：深蓝底，金色标题\n字体：粗宋体标题，正文细黑\n"
        "图片：每道菜一张圆形照片\n装饰：金色「推荐」徽章\n菜名价格：菜名在图下，价格在右")


def test_look_cleaning_takes_out_names_and_prices():
    from light import example_look
    cleaned = example_look.clean("版式：竖版，顶部是「好味来烧腊」标题\n\n配色：橙色  底，价格 ¥25 和 优惠价17元 用红色\n装饰：「招牌」徽章")
    assert cleaned == "版式：竖版，顶部是文字标题\n配色：橙色 底，价格 价格 和 优惠价价格 用红色\n装饰：「招牌」徽章"
    assert len(example_look.clean("字" * 5000)) == example_look.LOOK_LIMIT


async def test_the_card_reads_each_candidates_look_once_and_a_cut_is_not_read(monkeypatch):
    from light import example_look
    read = []

    async def fake_read(row, ctx):
        read.append(row.id)
        if row.title == "读不出来":
            raise ValueError("vision down")
        return LOOK
    monkeypatch.setattr(example_look, "read", fake_read)
    owner, session = await _guided("menu")
    good = await _example("menu", "韩料模板", scene="menu_red", rating=5)
    broken = await _example("menu", "读不出来", scene="menu_red", rating=4)
    known = await _example("menu", "已经读过", scene="menu_red", rating=3, look="版式：早就读过")
    _, request_id = await _call(session, owner, scene="menu_red")
    assert sorted(read) == sorted([good.id, broken.id])  # the one with a look is not read again
    async with get_db_session() as db:
        assert (await db.get(LightExample, good.id)).look == LOOK
        assert (await db.get(LightExample, broken.id)).look == ""  # a failed reading leaves the card as it was
        assert (await db.get(LightExample, known.id)).look == "版式：早就读过"
    assert [o["id"] for o in (await card(request_id)).questions[0]["detail"]["options"]][:3] == [good.id, broken.id, known.id]

    # A cut's plan card is its model; nothing looks at a frame of it.
    clip = await _example("footage_edit", "探店快剪", mime="video/mp4")
    assert example_look.readable(clip) is None and await example_look.ensure(clip, None) == ""
    assert read.count(clip.id) == 0


async def test_a_picked_example_hands_over_its_look_instead_of_the_old_prompt(monkeypatch):
    from light import example_look

    async def fake_read(row, ctx):
        return LOOK
    monkeypatch.setattr(example_look, "read", fake_read)
    owner, session = await _guided("menu")
    liked = await _example("menu", "韩料模板", scene="menu_red", recipe="生成提示词：好味来烧腊 经典烧鹅饭 优惠价 25元")
    _, request_id = await _call(session, owner, scene="menu_red")
    await q.reply(request_id, [["韩料模板"]], owner)
    assert await apply_answers(session.id, owner) is not None
    row = await card(request_id)
    async with get_db_session() as db:
        output = (await db.get(Part, row.part_id)).data["output"]
    assert "样子说明" in output and "配色：深蓝底，金色标题" in output and "图片：每道菜一张圆形照片" in output
    assert "Style and Layout lines" in output and "unchanged on any retry" in output
    assert "好味来烧腊" not in output and "25元" not in output  # the other shop's dishes and prices stay out
    fragment = await light_mode_fragment(owner, await get_session(session.id, user_id=owner))
    assert "配色：深蓝底，金色标题" in fragment and "好味来烧腊" not in fragment
    # Without a look (the reading failed), the old prompt is still what the agent gets.
    plain = await _example("poster", "没读过", recipe="生成提示词：大字促销")
    assert "当时的方案卡/提示词：生成提示词：大字促销" in examples.render([plain], chosen=True)
    # The automatic three keep their prompts: nobody chose them, and three looks would blur into one.
    assert "样子说明" not in examples.render([liked]) and "好味来烧腊" in examples.render([liked])


async def test_console_keeps_a_look_and_drops_it_when_the_picture_changes():
    from api.admin_examples import router
    from db.models.file_asset import FileAsset
    from db.repository.user_repo import PgUserRepo
    uid = uuid4().hex
    admin = await PgUserRepo().create(id=uid, username="lk" + uid[:8], password_hash=None, role="admin")
    ws = admin["default_workspace_id"]
    ids = []
    async with get_db_session() as db:
        for name in ("a.png", "b.png"):
            asset_id = "asset_" + uuid4().hex[:10]
            ids.append(asset_id)
            db.add(FileAsset(id=asset_id, user_id=uid, workspace_id=ws, name=name, mime="image/png", size=1,
                             oss_key=f"assets/{uid}/{asset_id}/{name}", status="ready", source="agent",
                             transient=False, is_deleted=False, created_at=_now()))
        await db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": uid, "role": "admin"}
    body = {"entry_id": "menu", "industry": "food", "shop_type": "noodle", "title": "红底菜单",
            "outputs": [{"asset_id": ids[0]}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = (await client.post("/api/admin/examples", json=body)).json()
        assert created["look"] == ""
        async with get_db_session() as db:
            (await db.get(LightExample, created["id"])).look = LOOK
            await db.commit()
        kept = (await client.put(f"/api/admin/examples/{created['id']}", json={**body, "title": "红底菜单二"})).json()
        assert kept["look"] == LOOK  # an edit that leaves the picture alone keeps what was read
        edited = (await client.put(f"/api/admin/examples/{created['id']}", json={**body, "look": "版式：人工改的"})).json()
        assert edited["look"] == "版式：人工改的"
        swapped = (await client.put(f"/api/admin/examples/{created['id']}",
                                    json={**body, "outputs": [{"asset_id": ids[1]}]})).json()
        assert swapped["look"] == ""  # another picture: read again next time the card shows it
