"""Guided form writes, inbox receipts, and same-session result projection."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select

from api.light import router
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import _upgrade_desktop_assistant_columns, get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.light_example import LightExample
from db.repository.user_repo import PgUserRepo
from light import examples, mode, production, production_form as forms, video_tiers
from light import remake, remake_case
from light.prompt import light_mode_fragment
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


async def test_production_history_filters_before_paginating(monkeypatch):
    owner = await _user()

    async def facts(_):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(mode, "_workspace_facts", facts)
    async with _client(owner) as client:
        for entry_id, brief in [("menu", "晚市菜单"), ("video", "菜馆宣传"), ("video", "招牌菜")]:
            created = await client.post("/api/light/productions", json={
                "entry_id": entry_id, "brief": brief,
            })
            assert created.status_code == 201, created.text
        path = "/api/light/productions"
        first = (await client.get(path, params={"entry_id": "video", "limit": 1})).json()
        assert [item["brief"] for item in first["items"]] == ["招牌菜"] and first["has_more"]
        second = (await client.get(path, params={"entry_id": "video", "limit": 1, "offset": 1})).json()
        assert [item["brief"] for item in second["items"]] == ["菜馆宣传"] and not second["has_more"]
        menu = (await client.get(path, params={"entry_id": "menu", "limit": 1})).json()
        assert [item["brief"] for item in menu["items"]] == ["晚市菜单"] and not menu["has_more"]


async def test_production_uses_only_the_selected_active_example(monkeypatch):
    owner = await _user()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    menu_id, disabled_id, other_id = (uuid4().hex for _ in range(3))
    async with get_db_session() as db:
        for example_id, entry, status in (
            (menu_id, "menu", "active"), (disabled_id, "menu", "disabled"),
            (other_id, "video", "active"),
        ):
            db.add(LightExample(
                id=example_id, entry_id=entry, industry="food", status=status,
                title=f"样例 {example_id}", created_at=now, updated_at=now,
                scene="menu_mono" if entry == "menu" else None,
                tags=["早茶", "餐厅"],
                look="版式：双栏，菜名和价格右侧对齐。",
                outputs=[{"oss_key": f"samples/{example_id}",
                          "mime": "image/jpeg" if entry == "menu" else "video/mp4"}],
            ))
    monkeypatch.setattr(examples, "preview", lambda row: {"example_id": row.id})
    async with _client(owner) as client:
        items = (await client.get("/api/light/examples", params={"entry_id": "menu"})).json()["items"]
        assert items == [{
            "example_id": menu_id, "scene": "menu_mono", "scene_label": "简约黑白",
            "tags": ["早茶", "餐厅"],
        }]
        created = (await client.post("/api/light/productions", json={"entry_id": "menu"})).json()
        session_id = created["session_id"]
        path = f"/api/light/productions/{session_id}"
        for invalid in (disabled_id, other_id):
            rejected = await client.patch(path, json={"revision": 1, "reference_example_id": invalid})
            assert rejected.json()["detail"]["code"] == "LIGHT_PRODUCTION_EXAMPLE_UNAVAILABLE"
        saved = (await client.patch(path, json={"revision": 1, "reference_example_id": menu_id})).json()
        assert saved["reference_example_title"] == f"样例 {menu_id}"
        assert saved["reference_example_id"] == menu_id
        assert saved["template"] == ""
        session = await get_session(session_id, user_id=owner["id"])
        fragment = await light_mode_fragment(owner["id"], session, lang="vi-VN")
        assert "版式：双栏" in fragment and menu_id in fragment
        assert "Show each confirmed dish and its price only once" in fragment
        assert disabled_id not in fragment and other_id not in fragment
        reset = (await client.patch(path, json={
            "revision": saved["revision"], "template": "warm_red", "reference_example_id": "",
        })).json()
        assert "reference_example_id" not in reset
        assert reset["template"] == "warm_red"
        session = await get_session(session_id, user_id=owner["id"])
        assert menu_id not in await light_mode_fragment(owner["id"], session, lang="vi-VN")

        video = (await client.post("/api/light/productions", json={"entry_id": "video"})).json()
        video_path = f"/api/light/productions/{video['session_id']}"
        built_in = (await client.patch(video_path, json={
            "revision": video["revision"], "template": "soda_story",
        })).json()
        library = (await client.patch(video_path, json={
            "revision": built_in["revision"], "reference_example_id": other_id,
        })).json()
        assert library["template"] == "" and library["reference_example_id"] == other_id

        async with get_db_session() as db:
            chosen = await db.get(LightExample, menu_id)
            assert (chosen.pick_count, chosen.use_count) == (1, 1)


async def test_poster_draft_uses_library_sample_and_queues_image_render_in_same_session(monkeypatch):
    owner = await _user()
    monkeypatch.setattr(production, "schedule_inbox_wake", lambda *_: None)
    active, disabled, menu = (uuid4().hex for _ in range(3))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_db_session() as db:
        for example_id, kind, status in (
            (active, "poster", "active"), (disabled, "poster", "disabled"), (menu, "menu", "active"),
        ):
            db.add(LightExample(
                id=example_id, entry_id=kind, industry="food", status=status,
                title="鲜辣上新", created_at=now, updated_at=now,
                scene="new_item" if kind == "poster" else "menu_mono",
                look="Film red, cream lettering, food photography below.",
                source={"title_translations": {"en-US": "Fresh and spicy", "vi-VN": "Món cay mới"}},
                outputs=[{"oss_key": f"samples/{example_id}", "mime": "image/png"}],
            ))
    monkeypatch.setattr(examples, "preview", lambda row: {
        "example_id": row.id, "title_translations": examples.title_translations(row),
    })
    async with _client(owner) as client:
        items = (await client.get("/api/light/examples", params={"entry_id": "poster"})).json()["items"]
        assert [item["example_id"] for item in items] == [active]
        assert items[0]["scene_label"] == "新品上市"
        assert items[0]["title_translations"]["vi-VN"] == "Món cay mới"

        response = await client.post("/api/light/productions", json={"entry_id": "poster"})
        assert response.status_code == 201, response.text
        created = response.json()
        session_id = created["session_id"]
        path = f"/api/light/productions/{session_id}"
        empty = await client.post(f"{path}/actions", json={
            "revision": created["revision"], "action": "render", "request_id": uuid4().hex,
        })
        assert empty.json()["detail"]["code"] == "LIGHT_PRODUCTION_POSTER"
        for reference_id in (disabled, menu):
            invalid = await client.patch(path, json={
                "revision": created["revision"], "reference_example_id": reference_id,
            })
            assert invalid.json()["detail"]["code"] == "LIGHT_PRODUCTION_EXAMPLE_UNAVAILABLE"

        response = await client.patch(path, json={
            "revision": created["revision"], "page": "review", "reference_example_id": active,
            "shop": "兴旺菜馆", "poster_title": "麻辣小龙虾上新", "script": "新鲜现做，欢迎到店",
        })
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["reference_example_title_translations"]["en-US"] == "Fresh and spicy"
        queued = await client.post(f"{path}/actions", json={
            "revision": saved["revision"], "action": "render", "request_id": uuid4().hex,
        })
        assert queued.status_code == 200, queued.text
        assert queued.json()["phase"] == "poster"
        history = (await client.get("/api/light/productions", params={"entry_id": "poster"})).json()
        assert [(item["session_id"], item["poster_title"]) for item in history["items"]] == [
            (session_id, "麻辣小龙虾上新"),
        ]

    session = await get_session(session_id, user_id=owner["id"])
    instructions = await light_mode_fragment(owner["id"], session, lang="vi-VN")
    assert "Film red, cream lettering" in instructions
    assert "麻辣小龙虾上新" in instructions and "新鲜现做，欢迎到店" in instructions
    assert production._media_matches("poster", "image/webp")
    assert not production._media_matches("poster", "video/mp4")
    async with get_db_session() as db:
        item = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == session_id))
        assert item.prompt == "开始生成海报。"


async def test_video_draft_retires_old_choices_and_locks_the_model_for_render(monkeypatch):
    owner = await _user()

    async def facts(_):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(mode, "_workspace_facts", facts)
    monkeypatch.setattr(production, "schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr(video_tiers, "available_tiers", lambda *_: list(video_tiers.ROUTES))
    monkeypatch.setattr(video_tiers, "resolve_tier", video_tiers.ROUTES.__getitem__)
    async with _client(owner) as client:
        for tier, (model, resolution) in video_tiers.ROUTES.items():
            entry = (await client.post("/api/light/productions", json={"entry_id": "video"})).json()
            assert entry["available_video_tiers"] == ["fast", "medium", "high"]
            assert entry["video_tier"] == "fast"
            path = f"/api/light/productions/{entry['session_id']}"
            for retired in ("wan_story", "wok"):
                rejected = await client.patch(path, json={"revision": entry["revision"], "template": retired})
                assert rejected.json()["detail"]["code"] == "LIGHT_PRODUCTION_TEMPLATE_RETIRED"
            saved = (await client.patch(path, json={
                "revision": entry["revision"], "page": "review", "video_tier": tier, "script": "热菜上桌",
            })).json()
            assert saved["video_tier"] == tier
            queued = await client.post(f"{path}/actions", json={
                "revision": saved["revision"], "action": "render", "request_id": uuid4().hex,
            })
            assert queued.status_code == 200, queued.text
            session = await get_session(entry["session_id"], user_id=owner["id"])
            assert (session.video_model, session.video_resolution) == (model, resolution)
            async with get_db_session() as db:
                item = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == session.id))
                assert (item.video_model, item.video_resolution) == (model, resolution)


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

        reference = "刚出锅的麻辣小龙虾，趁热吃。"
        patch = await client.patch(path, json={"revision": 1, "template": "twist", "page": "review",
                                               "shop": "巷口", "reference_copy": reference})
        assert patch.json()["revision"] == 2
        assert patch.json()["reference_copy"] == reference
        assert patch.json()["remake_prepared"] is False
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
        assert items[0].prompt == f"请生成视频文案。\n\n{reference}"


async def test_product_chat_tier_is_pinned_on_the_same_assistant_session(monkeypatch):
    from core.config import OpenBoxConfig

    owner = await _user()
    config = OpenBoxConfig(model_tiers={"product_chat": True})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(production, "schedule_inbox_wake", lambda *_: None)

    async def facts(_):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(mode, "_workspace_facts", facts)
    async with _client(owner) as client:
        entry = (await client.post("/api/light/productions", json={"entry_id": "menu"})).json()
        assert entry["chat_tier"] == "standard"
        assert "available_video_tiers" not in entry
        session_id = entry["session_id"]
        path = f"/api/light/productions/{session_id}"
        saved = await client.patch(path, json={
            "revision": entry["revision"], "chat_tier": "creative", "page": "review",
            "dishes": [{"name": "麻辣小龙虾"}],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["chat_tier"] == "creative"
        queued = await client.post(f"{path}/actions", json={
            "revision": saved.json()["revision"], "action": "render", "request_id": uuid4().hex,
        })
        assert queued.status_code == 200, queued.text
        session = await get_session(session_id, user_id=owner["id"])
        assert (session.model, session.variant) == ("openai/gemini-4-neon-xhigh", "xhigh")
        async with get_db_session() as db:
            item = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == session_id))
            assert (item.model, item.variant) == (session.model, session.variant)


def test_remake_preserves_eyelines_serving_endpoints_and_store_card():
    from tool import video_runninghub
    from video.timeline import Timeline

    adaptation = remake_case.Adaptation(product="麻辣小龙虾", reveal_copy="刚出锅，趁热吃。", end_copy="趁热吃")
    shots = remake_case.storyboard(adaptation)
    ids = [f"asset_shot{index}" for index in range(1, 8)]
    assert [shot["duration"] for shot in shots] == [4, 4, 5, 4, 4, 6, 3]
    assert "四目相对" in shots[1]["first_frame"] and "空隔热垫" in shots[2]["first_frame"]
    assert [shot["shot"] for shot in shots if shot["product_visible"]] == [3, 4, 7]
    assert shots[2]["spoken_line"] == "这顿我请。刚出锅，趁热吃。"
    assert "转正了" in shots[5]["spoken_line"] and "两双筷子" in shots[5]["spoken_line"]
    assert (remake_case.ASSETS / "03-end.jpg").is_file()
    for shot, first in zip(shots, ids, strict=True):
        assert (remake_case.ASSETS / f"{shot['shot']:02d}.jpg").is_file()
        last = "asset_landing" if shot["shot"] == 3 else None
        req = remake.shot_request("light:case", shot, first, adaptation.product, last)
        roles = tuple(ref.role for ref in req.input_assets)
        assert roles == (("first_frame", "last_frame") if last else ("first_frame",))
        assert bool(req.spoken_line) != req.no_speech and req.seed is None
        video_runninghub.validate(remake_case.MODEL, resolution=req.resolution, ratio=req.ratio,
                                  duration=req.duration, input_mimes=["image/jpeg"] * len(roles),
                                  roles=roles, generate_audio=req.generate_audio, prompt=req.prompt)
    from video.timeline import Cue
    dialogue = {shot["shot"]: remake.Dialogue([Cue(text=shot["spoken_line"], from_sec=0.41, to_sec=2)],
                                               max(5, shot["duration"])) for shot in shots if shot["spoken_line"]}
    dialogue[1] = remake.Dialogue([Cue(text=shots[0]["spoken_line"], from_sec=2.03, to_sec=4.59)], 5)
    dialogue[3] = remake.Dialogue([Cue(text=shots[2]["spoken_line"], from_sec=0.41, to_sec=4.57)], 6, "asset_voice")
    cut = Timeline.model_validate(remake.build_timeline(shots, ids, dialogue, lettering_asset="asset_card"))
    assert [track.asset for track in cut.shots] == ids and cut.duration_sec == 30
    assert cut.shots[0].in_sec == 0.71 and cut.captions[0].to_sec == 3.88
    assert cut.audio[0].asset == "asset_voice" and cut.audio[0].from_sec == 8
    assert len(cut.captions) == 6 and all(cue.to_sec <= 27 for cue in cut.captions)
    assert (cut.captions[2].from_sec, cut.captions[2].to_sec) == (8.41, 12.57)
    assert [(layer.asset, layer.from_sec, layer.to_sec) for layer in cut.overlays] == [("asset_card", 27, 30)]
    assert not cut.texts




def test_recognized_dialogue_keeps_script_spelling_and_sentence_pauses():
    spans = [{"text": "你转正了。", "from_sec": 0.7, "to_sec": 1.68},
             {"text": "下回带两双筷子。", "from_sec": 3.23, "to_sec": 5.51}]
    cues = remake._spoken_cues("你转正了！下回，带两双筷子。", spans)
    assert [(cue.text, cue.from_sec, cue.to_sec) for cue in cues] == [
        ("你转正了！", 0.7, 1.68), ("下回，带两双筷子。", 3.23, 5.51)]
    assert remake._spoken_cues("这顿我请。", spans) == []
    assert remake._spoken_cues("这顿我请。", []) == []


def test_old_generated_screenplay_does_not_override_new_template_beats():
    old = {"script": "【方案】约 20 秒，镜头1｜空盘"}
    assert remake_case.reference_copy(old) == ""
    assert "两双筷子" in remake_case.display_script(remake_case.Adaptation())


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
    names = ("light_production_form", "footage_analyze", "video_generate", "image_gen",
             "video_compose", "desktop_publish")
    tools = {name: SimpleNamespace(id=name) for name in names}
    session = SimpleNamespace(light_entry="footage_edit", light_production={"revision": 1})
    assert set(forms.restrict_guided_tools(tools, session, "prepare")) == {"light_production_form", "footage_analyze"}
    editing = {"footage_analyze", "video_compose"}
    assert set(forms.restrict_guided_tools(tools, session, "render")) == editing
    session.light_production["creative_effects"] = True
    assert set(forms.restrict_guided_tools(tools, session, "render")) == editing | {"image_gen"}
    session.light_production["run"] = {"creative_motion": True}
    assert set(forms.restrict_guided_tools(tools, session, "render")) == editing | {"image_gen", "video_generate"}
    assert set(forms.restrict_guided_tools(tools, session, "prepare")) == {"light_production_form", "footage_analyze"}


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


def test_wan_edit_uses_real_audio_and_rejects_a_single_packshot():
    from PIL import Image
    from light import wan_edit, wan_remake, wan_story
    from video.timeline import Timeline
    lines = [wan_story.Line(speaker="女儿", text="第一只给你。"), wan_story.Line(speaker="妈妈", text="趁热吃。")]
    words = [{"text": "第一只给你", "from_sec": 2., "to_sec": 4.}, {"text": "趁热吃", "from_sec": 5., "to_sec": 6.}]
    cues = wan_edit.captions(lines, {"sentences": [], "words": words})
    assert [(c["from_sec"],c["to_sec"],c["text"]) for c in cues] == [(2,4,"第一只给你。"),(5,6,"趁热吃。")]
    ends = [297,330,350,359]
    frames = {i: Image.new("RGB",(20,40),"white" if i==359 else "black") for i in ends}
    assert wan_edit.continuous_ends(ends,frames) == ends[:-1]
    cuts = [{"source":f"asset_{i}","in_sec":.04,"duration_sec":n,"shot":i,"cues":cues if i<3 else [],"heard":""}
            for i,n in enumerate((10.5,11.6,3.2),start=1)]
    timeline=wan_remake.timeline_for(cuts,"art")
    assert len(Timeline.model_validate(timeline).captions)==4
    extra = {"text": "这锅热吃", "from_sec": 4.1, "to_sec": 4.8}
    omit = wan_edit.dialogue_gaps(cues, {"words": [words[0], extra, words[1]]})
    assert omit and wan_edit.dialogue_gaps(cues, {"words": words}) == []
    cleaned = Timeline.model_validate(wan_remake.timeline_for(
        [cuts[0], {**cuts[1], "omit_speech": omit}, cuts[2]], "art"))
    assert cleaned.shots[1].volume == 0
    sound = [clip for clip in cleaned.audio if clip.asset == cuts[1]["source"]]
    assert len(sound) == 3 and sound[1].in_sec > cues[-1]["to_sec"]
    key="light:session:2:wan-story-v1:final:proof"
    run={"wan_final_key":key,"wan_cuts":cuts,"remake_version":wan_story.VERSION}
    job=SimpleNamespace(kind="compose",status="completed",output_asset_id="video",idempotency_key=key,
                        request_data={"duration_sec":25.3,"timeline":timeline})
    assert wan_story.is_complete_cut(run,job) and remake_case.owns_job(job)
    job.request_data={**job.request_data,"timeline":{**timeline,"shots":timeline["shots"][2:]}}
    assert not wan_story.is_complete_cut(run,job)
    job.request_data={"duration_sec":4,"timeline":timeline}
    assert not wan_story.is_complete_cut(run,job)


async def test_wan_intermediate_does_not_attach_or_wake_a_second_director():
    from light import wan_story
    from tool.video_production import _attach_completed
    from tool.tool import ToolContext
    from video.job_recovery import resume_conversation
    row=SimpleNamespace(kind="segment",idempotency_key=f"light:s:5:{wan_story.VERSION}:shot3",
                        session_id="s",user_id="u",output_asset_id="pack")
    assert not await resume_conversation(row)
    assert not await _attach_completed(row, ToolContext(session_id="s",message_id="m"))
    form = {**forms.initial_form("video", "巷口", ""), "template": wan_story.TEMPLATE,
            "run": {"action": "render", "remake_version": wan_story.VERSION}}
    session = SimpleNamespace(light_entry="video", light_production=form, status="error")
    row.status, row.result_data = "in_progress", {}
    assert production._work_view(session, SimpleNamespace(state="settled"), [row], None, False)["status"] == "failed"


def test_wan_copy_guides_the_director_and_only_adapted_lines_go_to_the_camera():
    from PIL import Image
    from light import wan_edit, wan_remake, wan_story

    reference = "给晚归的人留一锅虾。片尾想写「趁热吃」。"
    form = {"shop": "巷口", "brief": "做小龙虾广告", "reference_copy": reference, "asset_ids": ["dish"]}
    assert reference in wan_story.planning_instruction(form, "prepare")
    scene = wan_story.Scene(action="女儿望向妈妈，浮出宣传字「趁热吃」。", lines=[
        wan_story.Line(speaker="女儿", text="妈，我回来了"), wan_story.Line(speaker="妈妈", text="还热着。"),
    ])
    plan = wan_story.Plan(title="留的那一锅", product="小龙虾", appearance="红壳", setting_and_cast="母女在店里",
                          opening_frame="盖好的锅", reveal_frame="揭盖", opening=scene, response=scene, headline="趁热吃")
    first = wan_remake.shot_request("light:test", 1, plan, "opening", "reveal", "母女对望，出现文字「趁热吃」。")
    assert first.spoken_line == "妈，我回来了。还热着。"
    assert reference not in first.prompt and plan.headline not in first.prompt
    pack = wan_remake.shot_request("light:test", 3, plan, "previous-frame", None, "桌上唯一的锅")
    assert pack.no_speech and not pack.spoken_line and plan.headline not in pack.prompt
    assert [r.asset_id for r in pack.input_assets] == ["previous-frame"]
    transcript = {"words": [{"from_sec": 1, "to_sec": 4, "text": "妈我回来了还热着"},
                            {"from_sec": 6, "to_sec": 8, "text": "片尾想写趁热吃"}]}
    assert not wan_edit.captions(scene.lines, transcript)
    previous = Image.new("RGB", (32, 64), "gray")
    frames = {0: previous, 1: Image.new("RGB", (32, 64), "black")}
    assert wan_edit.matched_starts([0, 1], frames, previous)[0] == [0]
    assert wan_edit.held_product_ends([0, 1], frames, previous) == [0]
    shifted = previous.copy()
    shifted.paste("white", (0, 0, shifted.width, shifted.height // 3))
    plate = wan_edit.product_backdrop(previous, {1: previous, 50: shifted})
    assert plate.getpixel((5, 5))[-1] == 255 and plate.getpixel((5, 45))[-1] == 0
    assert wan_edit.product_backdrop(previous, {0: previous}) is None
    pack = wan_remake.shot_request("light:test", 3, plan, "previous-frame", "hold-frame", "镜头向后拉回母女")
    assert [r.role for r in pack.input_assets] == ["first_frame", "last_frame"]
    assert "镜头向后拉回母女" not in pack.prompt
    tools = {name: SimpleNamespace(id=name) for name in ("light_wan_story", "capability_search", "video_generate")}
    session = SimpleNamespace(light_entry="video", light_production={"template": wan_story.TEMPLATE})
    assert list(forms.restrict_guided_tools(tools, session, "prepare")) == ["light_wan_story"]
    assert "light_wan_story" not in forms.restrict_guided_tools(tools, session, None)
    assert wan_story.StoryRequest(action="render").plan is None


@pytest.mark.parametrize("vision_model", ["openai/vision-test", "openai/MiniMax/MiniMax-M3.1-Flash"])
async def test_wan_editor_reads_the_vision_tools_selected_frames(monkeypatch, vision_model):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    import litellm
    from agent import trajectory
    from light import wan_edit
    from core import config
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(video_analysis=SimpleNamespace(model=vision_model)))

    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield SimpleNamespace(chunk=AsyncMock())

    async def completion(**opts):
        function = opts["tools"][0]["function"]
        assert function["name"] == "choose_cut"
        if "MiniMax" in vision_model:
            assert opts["tool_choice"] == "auto"
            assert opts["extra_body"]["reasoning_effort"] == "medium"
        else:
            assert opts["tool_choice"]["function"]["name"] == "choose_cut"
        assert function["parameters"]["properties"]["last"]["enum"] == [344, 351]
        args = '{"first":1,"last":344,"reason":"动作完成","continuity":"锅盖在右侧","retake":false}'
        call = SimpleNamespace(function=SimpleNamespace(name="choose_cut", arguments=args))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))])

    monkeypatch.setattr(wan_edit, "_check", AsyncMock())
    monkeypatch.setattr(trajectory, "service_scope", scope)
    monkeypatch.setattr(trajectory, "capture_service_dispatch", scope)
    monkeypatch.setattr(litellm, "acompletion", completion)
    decision, _ = await wan_edit.decide(None, {}, "image", "asset", [0, 1], [344, 351])
    assert decision.first == 1 and decision.last == 344


async def test_new_review_contract_rechecks_old_cuts_and_preserves_its_own_progress(monkeypatch):
    from unittest.mock import AsyncMock
    import pytest
    from db.models.session import Session
    from light import wan_story
    from light.mode import LightError
    from tool.tool import ToolContext

    owner = await _user()
    monkeypatch.setattr(mode, "_workspace_facts", AsyncMock(return_value={"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}))
    async with _client(owner) as client:
        created = await client.post("/api/light/productions", json={"entry_id": "video"})
        assert created.status_code == 201
        sid = created.json()["session_id"]
    old_cut = {"shot": 1, "source": "original-paid-take", "in_sec": 0, "duration_sec": 3}
    async with get_db_session() as db:
        row = await db.get(Session, sid)
        row.light_production = {**row.light_production, "run": {"job_prefix": "production", "remake_version": wan_story.VERSION,
                                                               "wan_cuts": [old_cut], "wan_final_key": "old-final"}}
    ctx = ToolContext(session_id=sid, user_id=owner["id"], workspace_id=owner["default_workspace_id"])
    new = await production.begin_reference_edit(ctx, "production", "cast-v3")
    assert new["wan_cuts"] == [] and "wan_final_key" not in new
    assert new["wan_edit_history"][0]["cuts"] == [old_cut]
    checked = {**old_cut, "review_version": "cast-v3"}
    await production.save_wan_edit(ctx, "production", cuts=[checked])
    resumed = await production.begin_reference_edit(ctx, "production", "cast-v3")
    assert resumed["wan_cuts"] == [checked] and len(resumed["wan_edit_history"]) == 1
    with pytest.raises(LightError):
        await production.begin_reference_edit(ctx, "other-production", "next-review")


async def test_out_of_order_reference_cuts_survive_reload_and_require_a_complete_final():
    from light import reference_quality, wan_story
    from light.mode import LightError
    from session.session import create_session
    from tool.tool import ToolContext

    owner = await _user()
    run = {"job_prefix": "film", "remake_version": wan_story.VERSION, "wan_review_version": "review",
           "remake_adaptation": {"format": "shot-list-v2", "shots": [{}] * 5}}
    row = await create_session(user_id=owner["id"], workspace_id=owner["default_workspace_id"],
                               light_entry="video", light_production={"run": run})
    ctx = ToolContext(session_id=row.id, user_id=owner["id"], workspace_id=owner["default_workspace_id"])
    cuts = [{"shot": i, "source": f"take{i}", "review_version": "review"} for i in range(1, 6)]
    for i in (4, 2, 4):
        await production.save_reference_cut(ctx, "film", cuts[i])
    snapshot = await production.begin_reference_edit(ctx, "film", "review")
    assert snapshot["wan_cuts"] == [cuts[2], cuts[4]]
    with pytest.raises(LightError):
        await production.save_reference_quality(ctx, "film", {
            "version": reference_quality.VERSION, "input_digest": reference_quality.fingerprint(snapshot["wan_cuts"]),
        })
    with pytest.raises(LightError):
        await production.save_wan_edit(ctx, "film", cuts=snapshot["wan_cuts"], final_key="final")
    with pytest.raises(LightError):
        await production.save_reference_cut(ctx, "film", {**cuts[2], "source": "replacement"})
    with pytest.raises(LightError):
        await production.save_reference_cut(ctx, "film", {**cuts[0], "review_version": "old"})
    with pytest.raises(LightError):
        await production.save_reference_cut(ctx, "another-film", cuts[0])
    for i in (1, 3, 0):
        await production.save_reference_cut(ctx, "film", cuts[i])
    completed = await production.save_wan_edit(ctx, "film", cuts=cuts, final_key="final")
    assert completed["wan_cuts"] == cuts and completed["wan_final_key"] == "final"


def test_shot_progress_counts_only_distinct_shots_in_the_current_render():
    run = {"action": "render", "remake_stage": "wan_shots", "job_prefix": "work",
           "remake_adaptation": {"format": "shot-list-v2", "shots": [{}] * 5},
           "wan_cuts": [{"shot": 3, "source": "saved"}]}
    row = SimpleNamespace(status="busy", light_entry="video", light_production={"run": run})

    def segment(index, key, status="completed"):
        return SimpleNamespace(kind="segment", idempotency_key=key, status=status, request_data={"shot": index},
                               output_asset_id="clip" if status == "completed" else None)

    jobs = [segment(1, "work:shot1"), segment(1, "work:shot1:retry:a"),
            segment(2, "work:shot2"), segment(2, "work:shot2:retake:v", "in_progress"),
            segment(4, "work-old:shot4"), segment(5, "work:shot5", "generating")]
    progress = production._work_view(row, None, jobs, None, False)["shot_progress"]
    assert progress == {"total": 5, "filmed": 3, "reviewed": 1, "retaking": 1, "phase": "wan_shots"}
    run.update(remake_stage="wan_edit", wan_quality={"status": "reviewing"})
    assert production._work_view(row, None, jobs, None, False)["shot_progress"]["phase"] == "wan_quality"
    run.update(remake_stage="compose", wan_final_key="work:final:digest")
    old = SimpleNamespace(kind="compose", idempotency_key="work:final:old", status="completed", output_asset_id="old")
    assert production._work_view(row, None, [old], None, False)["shot_progress"]["phase"] == "compose"
    old.idempotency_key = "work:final:digest:retry:a"
    assert production._work_view(row, None, [old], None, False)["shot_progress"]["phase"] == "wan_delivery"
    row.light_production = {"run": {"action": "render", "remake_stage": "wan_shots"}}
    assert "shot_progress" not in production._work_view(row, None, jobs, None, False)
