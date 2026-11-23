from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from light import production, production_music, remake_lettering, shortcut_plan, shortcut_review, shortcut_story, shot_editor, wan_story
from light.production_form import TEMPLATES
from light.reference_film import timeline_for
from tool.light_remake import wan_tool_for_action
from tool.video_runninghub import MODEL
from video.music_catalog import MusicTrack
from video.timeline import Timeline


def _plan():
    cast = ["蓝围裙短发男", "灰围裙高马尾女", "米白外套短发女"]
    visible = [cast, cast[:2], [cast[1]], cast[:2], [cast[2]], [], cast]
    return {
        "title": "先吃上了", "product": "烤羊腿", "appearance": "焦香羊腿切片",
        "setting_and_cast": "夜市，两人穿围裙，一人穿衬衫", "identity_frame": "三人中景",
        "headline": "趁热上桌",
        "shots": [{"scene": "夜市", "kind": "product" if i == 6 else "scene", "seconds": sec,
                   "frame": "人物或产品中景", "action": "人物转身看向下一镜",
                   "camera": "固定机位，斜侧中近景", "visible_roles": visible[i - 1],
                   "performer": visible[i - 1][-1] if visible[i - 1] else None,
                   "product_in_first_frame": i >= 5,
                   **({"last_frame": "该镜动作结束"} if i in shortcut_plan.ENDPOINT_SHOTS else {})}
                  for i, sec in enumerate(shortcut_plan.DURATIONS, 1)],
    }


def test_shortcut_routes_to_typed_preview_and_validates_reveal_order():
    template = shortcut_plan.TEMPLATE
    assert template in TEMPLATES["video"] and template in wan_story.TEMPLATES
    assert wan_tool_for_action("prepare", template).parameters is shortcut_plan.PrepareRequest
    raw = _plan()
    assert shortcut_plan.PrepareRequest(plan=raw).plan.shots[3].last_frame
    assert wan_story.parse_plan(raw, template=template).product == "烤羊腿"
    raw["shots"][2]["product_in_first_frame"] = True
    with pytest.raises(ValidationError, match="前四镜不露成品"):
        wan_story.parse_plan(raw, template=template)


def test_prepare_disallows_the_handoff_cast_in_the_reveal_and_reads_legacy_drafts():
    raw = _plan()
    raw["shots"][4]["visible_roles"].append(raw["shots"][3]["visible_roles"][0])
    with pytest.raises(ValidationError, match="第5镜只拍从容者"):
        shortcut_plan.PrepareRequest(plan=raw)
    raw["shots"][3].pop("last_frame")
    for shot in raw["shots"]:
        for field in ("camera", "visible_roles", "performer"):
            shot.pop(field)
    assert shortcut_plan.ShortcutPlan.model_validate(raw).shots[3].last_frame is None


def test_shortcut_films_portrait_endpoints_and_retains_ambient_sound():
    plan = shortcut_plan.ShortcutPlan.model_validate(_plan())
    request = shortcut_story.shot_request("new-run", 5, plan.shots[4], ("asset_first", "asset_tail"),
                                          visible_setup="暖色餐桌的单人反打，米白外套短发女在画中")
    assert (request.model, request.resolution, request.ratio, request.duration) == (MODEL, "768p", "adaptive", 5)
    assert [(ref.asset_id, ref.role) for ref in request.input_assets] == [
        ("asset_first", "first_frame"), ("asset_tail", "last_frame"),
    ]
    assert request.generate_audio and request.no_speech
    assert "单人反打" in request.prompt and "只由米白外套短发女完成" in request.prompt
    assert "斜侧中近景" not in request.prompt
    cut = shortcut_story.accepted_cut(plan.shots[3], 4, "asset_video",
                                      {"has_audio": True, "duration_sec": 5.2, "fps": 24}, {})
    assert not cut["mute_audio"]
    assert cut["duration_sec"] == pytest.approx(5)


def test_observed_cut_uses_actual_frames_and_keeps_reveal_and_dialogue():
    cut = {"fps": 24, "cues": [{"from_sec": 0.5, "to_sec": 3.6}]}
    decision = shortcut_review.TakeCheck(ok=True, first=4, last=102, reason="保留抬头前的起势")
    checked = shortcut_review.apply_range(5, cut, decision, {4, 102}, 5.18, 5)
    assert checked["in_sec"] == pytest.approx(4 / 24, abs=0.0001)
    assert checked["duration_sec"] == pytest.approx(99 / 24, abs=0.0001)
    with pytest.raises(ValueError, match="实际存在"):
        shortcut_review.apply_range(5, cut, decision, {0, 102}, 5.18, 5)
    late = decision.model_copy(update={"first": 12, "last": 115})
    with pytest.raises(ValueError, match="揭晓"):
        shortcut_review.apply_range(5, cut, late, {12, 115}, 5.18, 5)


def test_opening_requires_an_on_screen_listener_and_can_cut_before_the_late_turn():
    raw = _plan()
    first = raw["shots"][0]
    speaker, listener, _ = first["visible_roles"]
    first.update(speaker=speaker, line="想吃羊腿？我俩自己烤。", performer=speaker)
    with pytest.raises(ValidationError, match="画内 speaker 和 listener"):
        shortcut_plan.PrepareRequest(plan=raw)
    first["listener"] = listener
    assert shortcut_plan.PrepareRequest(plan=raw).plan.shots[0].listener == listener

    cut = {"fps": 24, "cues": [{"from_sec": 0.52, "to_sec": 3.08}]}
    natural = shortcut_review.TakeCheck(ok=True, first=4, last=77, reason="说完就接下一镜")
    trimmed = shortcut_review.apply_range(1, cut, natural, {4, 77}, 5.18, 5)
    assert trimmed["duration_sec"] == pytest.approx(74 / 24, abs=0.0001)
    lingering = natural.model_copy(update={"last": 111})
    with pytest.raises(ValueError, match="多余等待或转头"):
        shortcut_review.apply_range(1, cut, lingering, {4, 111}, 5.18, 5)


async def test_opening_end_frame_uses_the_conversation_instead_of_legacy_departure(monkeypatch):
    raw = _plan()
    first = raw["shots"][0]
    speaker, listener, _ = first["visible_roles"]
    first.update(speaker=speaker, listener=listener, performer=speaker, line="咱俩自己烤。",
                 last_frame="两人朝左路站定。")
    plan = shortcut_plan.ShortcutPlan.model_validate(raw)
    create = AsyncMock(side_effect=["head", "tail"])
    monkeypatch.setattr(shortcut_story, "_still", create)
    assert await shortcut_story._frame_pair(None, "prefix", plan, 1, plan.shots[0], "cast", []) == ("head", "tail")
    opening, ending = [arguments.args[3] for arguments in create.await_args_list]
    assert f"{speaker}面对{listener}说话" in opening and f"{speaker}面对{listener}说话" in ending
    assert "两人朝左路站定" not in ending


async def test_setup_review_repairs_only_rejected_opening_and_reuses_approval(monkeypatch):
    plan = shortcut_plan.ShortcutPlan.model_validate(_plan())
    approved = {"ok": True, "reason": "人物和视线正确", "visible_setup": "可拍机位、角色和道具"}
    rejected = {"ok": False, "reason": "两人一起看向空白画外", "correction": "正对画内接话人的斜侧中景"}
    review = AsyncMock(side_effect=[
        {"report": {"opening": rejected, "handoff": approved, "reveal": approved}},
        {"report": {"opening": approved, "handoff": approved, "reveal": approved}},
    ])
    reframe = AsyncMock(return_value=("1-new-first", "1-new-last"))

    async def save(ctx, prefix, index, key, decision):
        return decision

    monkeypatch.setattr(shortcut_review, "key_frames", review)
    monkeypatch.setattr(shortcut_story, "_frame_pair", reframe)
    monkeypatch.setattr(production, "save_director_decision", save)
    frames = {1: ("1-first", "1-last"), 4: ("4-first", "4-last"), 5: ("5-first", "5-last")}
    result = await shortcut_story._confirm_setups(None, "prefix", {}, plan, frames, "cast", ["material"])
    assert result["frames"] == [("1-new-first", "1-new-last"), ("4-first", "4-last"), ("5-first", "5-last")]
    assert reframe.await_count == 1 and reframe.await_args.args[3] == 1
    assert "画内接话人" in reframe.await_args.kwargs["correction"]
    saved = {"director_decisions": {shortcut_story.APPROVED_SETUPS: result}}
    assert await shortcut_story._confirm_setups(None, "prefix", saved, plan, {}, "cast", []) == result
    assert review.await_count == 2


async def test_film_uses_approved_cut_and_only_retakes_a_visibly_failed_take(monkeypatch):
    plan = shortcut_plan.ShortcutPlan.model_validate(_plan())
    publish = AsyncMock()
    job = AsyncMock(side_effect=["first-video", "second-video"])
    meta = AsyncMock(return_value={"duration_sec": 5.18, "fps": 24, "has_audio": True})
    review = AsyncMock(side_effect=[
        {"cut": None, "report": {"reason": "道具未动", "correction": "用右手一次清楚抬杯"}},
        {"cut": {"source": "second-video", "duration_sec": 4.25}},
    ])

    async def save(ctx, prefix, index, key, decision):
        return decision

    monkeypatch.setattr(shot_editor, "publish_request", publish)
    monkeypatch.setattr(shortcut_story, "_job", job)
    monkeypatch.setattr(shortcut_story, "ensure_meta", meta)
    monkeypatch.setattr(shortcut_review, "filmed_cut", review)
    monkeypatch.setattr(production, "save_director_decision", save)
    result = await shortcut_story._film(SimpleNamespace(user_id="u"), "prefix", {}, plan, 5,
                                        ("head", "tail"), visible_setup="单人反打")
    assert (result["source"], result["take"], result["duration_sec"]) == ("second-video", 2, 4.25)
    assert "一次清楚抬杯" in publish.await_args.args[2].prompt
    assert "单人反打" in publish.await_args.args[2].prompt


@pytest.mark.parametrize("shop, expected", [
    ("Lane Night Kitchen", ["Lane Night Kitchen"]),
    ("Lane Night Kitchen and Smokehouse", ["Lane Night Kitchen and", "Smokehouse"]),
])
def test_product_card_preserves_shop_name_words(monkeypatch, shop, expected):
    paint = remake_lettering._label
    signature = []

    def record(draw, xy, text, size, ink):
        if xy[0] == 45 and xy[1] >= 60:
            signature.append(text)
        paint(draw, xy, text, size, ink)

    monkeypatch.setattr(remake_lettering, "_label", record)
    assert shortcut_story.end_card("孜然烤羊腿", shop).startswith(b"\x89PNG")
    assert signature == expected


def test_shortcut_composition_preserves_callback_and_cues_music_at_reveal():
    cuts = [{"shot": i, "source": f"asset_{i}", "in_sec": 0, "duration_sec": sec, "cues": []}
            for i, sec in enumerate(shortcut_plan.DURATIONS, 1)]
    timeline = timeline_for(cuts, "asset_title", hero_shot=6, music_start=17)
    assert len(timeline["shots"]) == 7
    assert timeline["overlays"] == [{"asset": "asset_title", "kind": "image", "x": 0, "y": 0,
                                    "width": 1, "height": 1, "fit": "contain",
                                    "from_sec": 22.12, "to_sec": 25}]
    bed = timeline["audio"][0]
    assert bed["from_sec"] == 17 and bed["to_sec"] == 30 and bed["duck_under_captions"]


async def test_shortcut_uses_netease_at_reveal_and_honors_off(monkeypatch):
    track = MusicTrack(id=production_music.CAREFREE, title="Carefree", artist="Kevin MacLeod",
                       sec=205, bpm=96, beats=[], moods=[],
                       oss_key="music/catalog/33190124/" + "a" * 64 + ".mp3",
                       source_url="https://music.163.com/song?id=33190124", license_url="", attribution="Carefree")
    find = AsyncMock(return_value=track)
    monkeypatch.setattr(production_music, "_netease_track", find)
    timeline = Timeline.model_validate({"shots": [
        {"asset": f"asset_{i}", "duration_sec": sec} for i, sec in enumerate(shortcut_plan.DURATIONS)
    ]})
    ctx = object()
    mixed, meta = await production_music.mix(ctx, timeline, "auto", shortcut_plan.TEMPLATE, "assets.example")
    find.assert_awaited_once_with(ctx, "Carefree Kevin MacLeod", 30, exact_id=production_music.CAREFREE)
    assert len(mixed.audio) == 1
    assert (mixed.audio[0].from_sec, mixed.audio[0].to_sec) == (17, 30)
    assert mixed.audio[0].duck_under_captions
    assert meta["music_source"] == "netease" and meta["soundtrack"]["license_url"]
    off, _ = await production_music.mix(ctx, timeline, "off", shortcut_plan.TEMPLATE, "assets.example")
    assert not off.audio
