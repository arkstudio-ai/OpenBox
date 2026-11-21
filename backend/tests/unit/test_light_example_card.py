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


def test_a_clip_is_sampled_every_second_and_a_half_within_bounds():
    assert len(example_card.frame_times(3)) == example_card.MIN_FRAMES
    assert len(example_card.frame_times(30)) == 20
    long = example_card.frame_times(300)
    assert len(long) == example_card.MAX_FRAMES and 0 < long[0] and long[-1] < 300


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

    async def clip(output):
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
        created = (await client.post("/api/admin/examples", json=body)).json()
        assert created["card"] == "" and created["card_state"] == {} and started == [(created["id"], False)]
        url = f"/api/admin/examples/{created['id']}"
        token = await example_card.begin(created["id"])
        await example_card.finish(created["id"], token, {"card": CARD, "meta": {"model": "m", "kind": "image"}})
        # Another field saved: the card the model wrote stays; the recipe changed: it goes and is read again.
        kept = (await client.put(url, json={**body, "rating": 5})).json()
        assert kept["card"] == CARD and kept["card_state"]["state"] == "ready"
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
