"""The reference card of a library example (light/example_card.py): what the reader is shown, what is kept
of its reply, and when the console has it written again."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db.base import get_db_session
from db.models.light_example import LightExample
from light import example_card
from tests.unit.test_light_examples import _asset, _client, _example, _user, empty_library  # noqa: F401

CARD = "版式：竖版三段，标题区在上占三分之一，中间两栏排菜名和价格，底部留一条放行动号召。" * 6
REPLY = CARD


REQUEST = {
    "duration_sec": 6.0,
    "soundtrack": {"track_id": "netease:123", "attribution": "Night Market - Someone", "beat_cuts": [1, 2]},
    "timeline": {
        "canvas": {"width": 720, "height": 1280, "fps": 24},
        "shots": [
            {"asset": "oss://bossip/assets/u/a/one.mp4", "duration_sec": 3, "speed": 1.0, "volume": 1.0,
             "move": {"preset": "zoom_in"}, "transition_out": {"type": "fade", "seconds": 0.5}},
            {"asset": "oss://bossip/assets/u/a/two.mp4", "duration_sec": 7, "speed": 2.0, "volume": 0, "filter": "warm"},
        ],
        "captions": [{"from_sec": 0.2, "to_sec": 2, "text": "今晚吃什么"}, {"from_sec": 3, "to_sec": 5, "text": "招牌牛肉面来了"}],
        "caption_style": {"bottom_ratio": 0.095, "style": {"font": "AlibabaPuHuiTi", "size": 44, "color": "#FFFFFF",
                                                           "outline": 3, "outline_color": "#000000"},
                          "motion": {"in_effect": "zoomin_in"}},
        "texts": [{"text": "招牌牛肉面", "from_sec": 0, "to_sec": 2, "x": 0.5, "y": 0.12, "align": "center",
                   "style": {"font": "AlibabaPuHuiTi", "size": 96, "color": "#FFD400", "color_style": "gold_1"},
                   "motion": {"in_effect": "slide_down_in"}}],
        "audio": [
            {"voiceover": {"text": "今晚吃什么", "voice": "zhixiang", "speech_rate": 120}, "from_sec": 0.2, "to_sec": 2, "volume": 1.0},
            {"asset": "oss://bossip/music/catalog/123/x.mp3", "from_sec": 0, "to_sec": 6, "volume": 0.28,
             "fade_out_sec": 1, "duck_under_voice": True, "duck_volume": 0.1},
        ],
    },
}


def _stream(reply, seen):
    async def stream_llm(agent_def, system, messages, tools, model_id, ctx, **kwargs):
        seen.update(system=system, message=messages[0], model=model_id, kwargs=kwargs)
        yield {"type": "text_delta", "text": reply}
        yield {"type": "finish", "reason": "stop"}
    return stream_llm


def test_the_shops_own_words_are_taken_out_of_the_reply():
    reply = "版式：顶部是好味来烧腊的店名，下面「脆皮烧鹅」卖 ¥38，另一款 25元。\n" + CARD + "「招牌」徽章放右上。"
    card = example_card.parse(reply, ("好味来烧腊", ""))
    assert card.startswith("版式：顶部是店名的店名，下面文字卖 价格，另一款 价格。")
    assert "好味来" not in card and "38" not in card and card.endswith("「招牌」徽章放右上。")
    assert len(example_card.parse("字" * 9000)) == example_card.CARD_LIMIT
    with pytest.raises(ValueError):
        example_card.parse("太短")


def test_a_clip_is_sampled_every_second_and_once_inside_every_shot():
    assert len(example_card.frame_times(3)) == example_card.MIN_FRAMES
    assert len(example_card.frame_times(20)) == 20
    long = example_card.frame_times(300)
    assert len(long) == example_card.MAX_FRAMES and 0 < long[0] and long[-1] < 300
    # The middle of each shot is always among them, and no even frame lands on top of one.
    marked = example_card.frame_times(12, [0.4, 5.5, 11.8, 40])
    assert {0.4, 11.8} <= set(marked) and marked.count(5.5) == 1 and 40 not in marked and len(marked) == 12
    assert example_card._heard({"text": "整段", "sentences": [{"from_sec": 0.2, "to_sec": 1.5, "text": "第一句"}]}) == "[0.2–1.5 秒] 第一句"
    assert example_card._heard({"text": "整段"}) == "整段"


def test_a_reading_that_never_reported_back_shows_as_failed():
    fresh = {"state": "reading", "at": datetime.now(timezone.utc).isoformat(), "token": "t"}
    assert example_card.shown_state(fresh) == {"state": "reading", "at": fresh["at"]}
    old = (datetime.now(timezone.utc) - timedelta(seconds=example_card.STALE_SECONDS + 5)).isoformat()
    assert example_card.shown_state({"state": "reading", "at": old})["state"] == "failed"
    assert example_card.shown_state(None) == {}


async def test_a_picture_is_read_with_the_recipe_beside_it(monkeypatch):
    seen = {}

    async def picture(key):
        return "data:image/jpeg;base64,AAAA"
    monkeypatch.setattr("light.example_look._picture", picture)
    monkeypatch.setattr("agent.llm.stream_llm", _stream(REPLY, seen))
    monkeypatch.setattr(example_card, "card_model", lambda config: "openai/gpt-6.2-sol")
    row = await _example(entry_id="menu", notes="版式干净", recipe="Style: 暖色手绘\nLayout: 三栏",
                         outputs=[{"asset_id": "a1", "oss_key": "k/menu.png", "name": "menu.png", "mime": "image/png"}])
    result = await example_card.read(row, SimpleNamespace(user_id="u"))
    assert result["card"] == example_card.clean(CARD, example_card.CARD_LIMIT)
    assert result["meta"] == {"model": "openai/gpt-6.2-sol", "kind": "image"}
    assert seen["system"] == [example_card.IMAGE_SYSTEM] and seen["message"]["_images"] == ["data:image/jpeg;base64,AAAA"]
    assert "Layout: 三栏" in seen["message"]["content"] and "版式干净" in seen["message"]["content"]
    assert seen["kwargs"]["billing_kind"] == "example_card" and seen["kwargs"]["variant"] == "high"


async def test_a_clip_is_read_from_its_frames_and_speech(monkeypatch):
    seen = {}

    async def clip(output, marks=()):
        seen["marks"] = list(marks)
        return {"duration": 18.0, "orientation": "portrait", "transcript": "今天带你吃一家老店",
                "frames": [(0.75, "data:f1"), (2.25, "data:f2"), (3.75, "data:f3")]}
    monkeypatch.setattr(example_card, "_clip", clip)
    monkeypatch.setattr("agent.llm.stream_llm", _stream(REPLY, seen))
    monkeypatch.setattr(example_card, "card_model", lambda config: "openai/gpt-6.2-sol")
    row = await _example(entry_id="video", recipe="镜头一：门头",
                         outputs=[{"asset_id": "a1", "oss_key": "k/final.mp4", "name": "final.mp4", "mime": "video/mp4"}])
    result = await example_card.read(row, SimpleNamespace(user_id="u"))
    assert result["meta"] == {"model": "openai/gpt-6.2-sol", "kind": "video", "frames": 3, "heard": True, "duration": 18.0}
    content = seen["message"]["content"]
    assert seen["system"] == [example_card.VIDEO_SYSTEM] and seen["message"]["_images"] == ["data:f1", "data:f2", "data:f3"]
    assert "成片：18.0 秒，竖屏" in content and "第 0.75、2.25、3.75 秒" in content and "今天带你吃一家老店" in content
    assert "剪辑清单：\n（没有。" in content and seen["marks"] == []  # no compose job behind this piece

    # With the compose job on record, the reader gets the cut as it was made, minus the shop's wording.
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        db.add(VideoJob(id="video_" + row.id, user_id="u", kind="compose", status="completed", output_asset_id="a1", idempotency_key=row.id,
                        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
                        request_data=REQUEST))
        await db.commit()
    monkeypatch.setattr("agent.llm.stream_llm", _stream(REPLY, seen))
    result = await example_card.read(row, SimpleNamespace(user_id="u"))
    content = seen["message"]["content"]
    assert seen["marks"] == [1.5, 4.25]
    assert "画布 720×1280，24 帧/秒，全长 6 秒，2 个镜头" in content
    assert "1. 0–3 秒，运镜 缓推（zoom_in），出点转场 fade 0.5 秒" in content
    assert "2. 2.5–6 秒，变速 2 倍，滤镜 warm，原声静音，硬切" in content
    assert "字幕：2 条，0.2–5 秒，底边距 9.5%，最长一条 7 字，字体 AlibabaPuHuiTi，字号 44" in content
    assert "入场动画 slide_down_in" in content and "花字预设 gold_1" in content
    assert "配乐：曲目 Night Market - Someone，0–6 秒，音量 0.28，曲目编号 netease:123，淡出 1 秒，有人声或字幕时压到 0.1，2 个切点对了拍" in content
    assert "配音：合成音色 zhixiang，1 句，0.2–2 秒，语速 120，音量 1" in content
    assert "招牌牛肉面" not in content and "今晚吃什么" not in content and "oss://" not in content
    from light import example_edit
    laid = example_edit.lines(example_edit.sheet({"timeline": {"shots": [{"asset": "a", "duration_sec": 2}], "overlays": [
        {"asset": "b", "kind": "image", "from_sec": 0, "to_sec": 2, "x": 0, "y": 0, "width": 1, "height": 1},
        {"asset": "c", "kind": "video", "from_sec": 0, "to_sec": 1, "x": 0.6, "y": 0.1, "width": 0.3, "height": 0.2}]}}))
    assert "整屏贴图：0–2 秒" in laid[-2] and laid[-1].startswith("画中画：视频，0–1 秒") and "配乐：无" in laid
    assert example_edit.sheet({"timeline": {"shots": []}}) is None and example_edit.lines(None) == []
    assert result["meta"]["edit"]["shot_count"] == 2 and result["meta"]["edit_lines"][0].startswith("画布")

    async def cut_short(agent_def, system, messages, tools, model_id, ctx, **kwargs):
        yield {"type": "text_delta", "text": REPLY}
        yield {"type": "finish", "reason": "length"}
    monkeypatch.setattr("agent.llm.stream_llm", cut_short)
    with pytest.raises(ValueError):
        await example_card.read(row, SimpleNamespace(user_id="u"))
    with pytest.raises(ValueError):  # nothing to look at
        await example_card.read(await _example(entry_id="video"), SimpleNamespace(user_id="u"))


async def _row(example_id):
    async with get_db_session() as db:
        return await db.get(LightExample, example_id)


async def test_one_reading_at_a_time_and_a_late_one_is_dropped():
    piece = [{"asset_id": "a1", "oss_key": "k/p.png", "name": "p.png", "mime": "image/png"}]
    row = await _example(entry_id="poster", outputs=piece)
    assert await example_card.begin((await _example(entry_id="poster")).id) is None  # no piece
    token = await example_card.begin(row.id)
    assert token and await example_card.begin(row.id) is None and await example_card.begin(row.id, force=True) is None
    await example_card.finish(row.id, "someone-else", {"card": "x", "meta": {}})
    assert (await _row(row.id)).card == ""
    await example_card.finish(row.id, token, {"card": CARD, "meta": {"model": "m", "kind": "image"}})
    saved = await _row(row.id)
    assert saved.card == CARD and saved.card_meta["state"] == "ready" and saved.card_meta["model"] == "m"
    assert await example_card.begin(row.id) is None  # already there
    again = await example_card.begin(row.id, force=True)
    await example_card.finish(row.id, again, None, "TimeoutError")
    failed = await _row(row.id)
    assert failed.card == CARD and failed.card_meta == {"state": "failed", "at": failed.card_meta["at"], "error": "TimeoutError"}


async def test_the_console_keeps_the_teams_text_and_rereads_when_the_recipe_changes(monkeypatch):
    started = []
    monkeypatch.setattr("api.admin_examples._read_card_later",
                        lambda example_id, user, force=False: started.append((example_id, force)))
    admin = await _user(role="admin")
    picture = await _asset(admin["id"], admin["default_workspace_id"], "poster.png", "image/png")
    body = {"entry_id": "poster", "industry": "food", "shop_type": "noodle", "title": "海报样例", "recipe": "Style: 红金",
            "outputs": [{"asset_id": picture}]}
    async with _client(admin) as client:
        translations = {"en-US": "Poster example", "vi-VN": "Mẫu áp phích"}
        created = (await client.post("/api/admin/examples", json={
            **body, "title_translations": translations,
        })).json()
        assert created["source"]["title_translations"] == translations
        assert created["card"] == "" and created["card_state"] == {} and started == [(created["id"], False)]
        url = f"/api/admin/examples/{created['id']}"
        token = await example_card.begin(created["id"])
        await example_card.finish(created["id"], token, {"card": CARD, "meta": {"model": "m", "kind": "image"}})
        # Another field saved: the card the model wrote stays; the recipe changed: it goes and is read again.
        kept = (await client.put(url, json={**body, "rating": 5})).json()
        assert kept["card"] == CARD and kept["card_state"]["state"] == "ready"
        assert kept["source"]["title_translations"] == translations
        assert (await client.put(url, json={**body, "recipe": "Style: 蓝白"})).json()["card"] == ""
        # The team's own text survives a recipe change, and only emptying it asks for a new reading.
        mine = (await client.put(url, json={**body, "card": "我写的参考卡"})).json()
        assert mine["card"] == "我写的参考卡" and mine["card_state"]["state"] == "edited"
        assert await example_card.begin(created["id"]) is None
        assert (await client.put(url, json={**body, "recipe": "Style: 黑金"})).json()["card"] == "我写的参考卡"
        emptied = (await client.put(url, json={**body, "recipe": "Style: 黑金", "card": ""})).json()
        assert emptied["card"] == "" and emptied["card_state"] == {}
        started.clear()
        asked = await client.post(f"{url}/card")
        assert asked.status_code == 202 and started == [(created["id"], True)]
        bare = (await client.post("/api/admin/examples", json={**body, "outputs": []})).json()
        assert (await client.post(f"/api/admin/examples/{bare['id']}/card")).status_code == 422
        assert (await client.post("/api/admin/examples/nope/card")).status_code == 404
