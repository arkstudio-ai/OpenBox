from types import SimpleNamespace

import pytest

from light import product_story as director
from light import product_story_plan as recipe
from light import wan_story
from light.production_form import TEMPLATES
from light.remake import RemakeStopped
from tool.light_remake import wan_tool_for_action
from tool.video_runninghub import MODEL


def test_brand_story_preview_and_frame_bound_delivery():
    story = recipe.Plan()
    shots = recipe.shots(story)
    assert len(shots) == 21
    assert sum(s["frames"] for s in shots) == 1296
    assert [s["id"] for s in shots if s["spoken_line"]] == ["01", "02", "14"]
    assert all((recipe.ASSETS / f"{s['id']}.jpg").is_file() for s in shots if s["id"] != "17")
    assert recipe.TEMPLATE in TEMPLATES["video"]
    assert wan_tool_for_action("prepare", recipe.TEMPLATE).parameters is recipe.PrepareRequest
    assert wan_story.parse_plan(story.model_dump(), template=recipe.TEMPLATE) == story
    assert "在手机里回看同一张合照" in recipe.display_script(story)
    assert wan_story.shot_count({"remake_adaptation": story.model_dump()}) == recipe.FILMED_COUNT


def test_synced_take_moves_the_cut_and_checks_the_approved_words():
    shot = recipe.shots(recipe.Plan())[13]
    media = {"width": 1344, "height": 768, "has_audio": True, "duration_sec": 5.1667}
    transcript = {"sentences": [{"text": "爸 往我这边一点", "from_sec": 1.35, "to_sec": 4.95}]}
    take = director.shot_request("review", 14, shot, "asset_14", 1)
    assert take.model == MODEL and take.generate_audio and take.spoken_line == shot["spoken_line"]
    assert take.input_assets[0].role == "first_frame"
    cut = director.choose_cut(shot, 14, "motion", media, transcript)
    assert cut["start"] > 0 and cut["output_frames"] == 106
    assert (cut["start"] + cut["frames"]) / 24 >= 4.95
    transcript["sentences"][0]["text"] = "看这边"
    with pytest.raises(RemakeStopped, match="指定对白"):
        director.choose_cut(shot, 14, "motion", media, transcript)


def test_final_cannot_surface_with_missing_dialogue_or_an_old_cut():
    cuts = [{"shot": i, "source": str(i), "frames": s["frames"], "start": 0, "speed": 1}
            for i, s in enumerate(recipe.shots(recipe.Plan())[:16], 1)]
    run = {"wan_cuts": cuts, "wan_final_key": "final", "remake_adaptation": recipe.Plan().model_dump()}
    job = SimpleNamespace(idempotency_key="final", kind="compose", status="completed", output_asset_id="new",
                          request_data={**recipe.final_request(cuts), "_trajectory_context": {}},
                          result_data={"frames": 1296, "fps": 24, "duration_sec": 54,
                                       "has_audio": True, "dialogue": 3})
    assert wan_story.is_complete_cut(run, job)
    job.result_data["dialogue"] = 2
    assert not wan_story.is_complete_cut(run, job)
    job.result_data["dialogue"] = 3
    job.request_data["timeline"]["shots"][7]["asset"] = "stale"
    assert not wan_story.is_complete_cut(run, job)


def test_vietnamese_copy_overrides_a_chinese_interface_and_keeps_verbatim_words():
    import unicodedata

    from light import video_language

    form = {"reference_copy": "Quảng cáo Xiaomi 18 bằng tiếng Việt."}
    assert video_language.for_form(form, "zh-CN") == "vi"
    assert video_language.for_form({"brief": "请用越南语制作这条广告。"}, "zh-CN") == "vi"
    assert video_language.for_form({"brief": "Xiaomi 18"}, "vi-VN") == "vi"
    assert video_language.for_form({"brief": "Xiaomi 18"}, "zh-CN") == "zh"
    plan = recipe.Plan(answer="Ba đang chụp mà.").localized("vi")
    assert plan.language == "vi" and plan.answer == "Ba đang chụp mà."
    assert plan.title == "Có ba trong ảnh" and "54 giây · 21 cảnh" in recipe.display_script(plan)
    shot = recipe.shots(plan)[13]
    request = director.shot_request("review", 14, shot, "frame", 1)
    assert request.spoken_line == "Ba ơi, lại gần con chút."
    assert "Vietnamese" in request.prompt and "Mandarin" not in request.prompt
    transcript = {"sentences": [{"text": unicodedata.normalize("NFD", shot["spoken_line"]),
                                 "from_sec": 0.4, "to_sec": 2.1}]}
    cut = director.choose_cut(shot, 14, "asset", {
        "width": 1344, "height": 768, "has_audio": True, "duration_sec": 5.2,
    }, transcript)
    assert cut["cues"][0]["text"] == shot["spoken_line"]
    assert video_language.MEDIA_TAGS[plan.language] == "vie"


@pytest.mark.asyncio
async def test_vietnamese_voice_replacement_keeps_the_approved_line_and_edit_identity(monkeypatch):
    from light import production_voice

    async def render(**args):
        assert args["text"] == "Ba chụp mà." and args["locale"] == "vi"
        return "test-voice.wav"

    async def get_object(key):
        assert key == "test-voice.wav"
        return b"sample"

    monkeypatch.setattr(production_voice, "render_clip", render)
    monkeypatch.setattr(production_voice, "_canonical_wav", lambda data: (data, 1.0))
    monkeypatch.setattr(director, "get_oss", lambda: SimpleNamespace(get_object=get_object))
    ctx = SimpleNamespace(user_id="user", workspace_id="workspace", session_id="session")
    shot = recipe.shots(recipe.Plan().localized("vi"))[1]
    media = {"width": 1344, "height": 768, "has_audio": True, "duration_sec": 5.17}
    transcript = {"sentences": [{"text": "Ba chập mà!", "from_sec": 0.7, "to_sec": 1.9}]}
    with pytest.raises(RemakeStopped, match="指定对白"):
        director.choose_cut(shot, 2, "take", media, transcript)
    cut = await director._dub(ctx, 2, shot, "take", media, transcript)
    assert cut["cues"] == [{"text": "Ba chụp mà.", "from_sec": 0.7, "to_sec": 1.9}]
    assert recipe.final_request([cut], "vi")["timeline"]["shots"][0]["dub"]["key"] == "test-voice.wav"
