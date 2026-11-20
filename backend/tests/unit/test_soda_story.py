import io

from PIL import Image, ImageDraw

from light import soda_story, wan_story
from light.production_form import TEMPLATES
from light.reference_plan import Shot
from tool.video_runninghub import MODEL


def test_soda_recipe_keeps_source_analysis_out_of_the_timeline():
    assert wan_story.SODA_TEMPLATE in TEMPLATES["video"]
    assert "无需分析样片" in soda_story.DIRECTING
    shot = Shot(scene="餐厅", frame="桌边低机位", action="手把手机推到桌中央", seconds=3)
    request = soda_story.shot_request("soda-case", 1, shot, "asset_still")
    assert (request.model, request.ratio, request.resolution, request.duration) == (MODEL, "adaptive", "768p", 5)
    assert request.input_assets[0].role == "first_frame"
    assert request.generate_audio is True
    assert request.idempotency_key == "soda-case:shot1"


def test_board_tiles_preserve_row_order_and_omit_unused_cell():
    image = Image.new("RGB", (480, 600))
    colors = [(128, 0, 0), (0, 128, 0), (0, 0, 128), (128, 128, 0), (0, 128, 128)]
    for i, color in enumerate(colors):
        x, y = i % 3 * 160, i // 3 * 300
        ImageDraw.Draw(image).rectangle((x, y, x + 159, y + 299), fill=color)
    raw = io.BytesIO()
    image.save(raw, "PNG")
    cells = soda_story.split_board(raw.getvalue(), 3, 5)
    assert len(cells) == 5
    for item, color in zip(cells, colors, strict=True):
        with Image.open(io.BytesIO(item)) as cell:
            assert cell.size == (720, 1280)
            assert all(abs(a - b) <= 3 for a, b in zip(cell.getpixel((360, 640)), color))


def test_cut_window_uses_actual_dialogue_and_keeps_audible_bounds():
    shot = Shot(scene="餐厅", frame="餐桌中景", action="女主管看向同事",
                seconds=3, speaker="主管", line="人先吃饭")
    duration = 5.12
    meta = {"duration_sec": duration, "fps": 25}
    transcript = {"sentences": [
        {"text": "人先吃饭。", "from_sec": 3.44, "to_sec": 4.95},
    ]}
    cut = soda_story.cut_for(shot, 2, "asset_take", meta, transcript)
    assert cut["mute_audio"] is False
    assert cut["in_sec"] <= 3.44
    assert 4.95 <= cut["in_sec"] + cut["duration_sec"] <= duration
    assert cut["cues"] == [{"text": "人先吃饭", "from_sec": 3.44, "to_sec": 4.95}]


def test_soda_shots_use_the_selected_wan_model_without_losing_dialogue():
    shot = Shot(scene="餐厅", frame="桌边低机位", action="主管把手机按下",
                seconds=3, speaker="主管", line="人先吃饭")
    request = soda_story.shot_request(
        "soda-case", 1, shot, "asset_still", model=wan_story.MODEL, resolution="720p",
    )
    assert (request.model, request.ratio, request.resolution) == (wan_story.MODEL, "9:16", "720p")
    assert request.generate_audio is True
    assert request.spoken_line == shot.line
    assert request.idempotency_key == "soda-case:shot1"


def test_visual_edit_rejects_trim_over_dialogue_and_preserves_hero_time():
    from light.soda_visual_edit import Decision, apply_decision

    cuts = [
        {"shot": index, "source": f"take-{index}", "in_sec": 0.12, "duration_sec": 4.0,
         "fps": 25, "cues": ([{"from_sec": 0.6, "to_sec": 2.8}] if index == 1 else [])}
        for index in range(1, 7)
    ]
    decision = Decision.model_validate({
        "edits": [
            {"shot": 1, "start": 1.0, "end": 4.0, "reason": "this would cut dialogue"},
            {"shot": 2, "start": 0.12, "end": 3.4, "reason": "trim the dish before it changes"},
            {"shot": 3, "start": 0.0, "end": 7.0, "reason": "out of range"},
            {"shot": 6, "start": 2.0, "end": 3.2, "reason": "too short for the title"},
        ]
    })
    revised, adjusted = apply_decision(cuts, dict.fromkeys(range(1, 7), 5.0), decision)
    assert adjusted == [2]
    assert revised[0] == cuts[0] and revised[-1] == cuts[-1]
    assert revised[1]["duration_sec"] == 3.28
    assert [cut["source"] for cut in revised] == [cut["source"] for cut in cuts]
