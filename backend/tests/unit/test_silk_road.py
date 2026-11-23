from types import SimpleNamespace

import pytest

from light import production_form, silk_road, wan_story
from tool.light_remake import wan_tool_for_action
from video.ims_compiler import compile_ims
from video.timeline import Timeline


def test_silk_road_plan_uses_materials_and_video_reference():
    plan = silk_road.Plan(
        title="丝路留香", product="塞外朵莱娜·丝路香馕烤羊腿", appearance="焦金羊肉、紫洋葱",
        bite="一块去骨羊肉；沙中试吃硬馕", direction="完整故事供 App 预览",
        take_notes=[{"shot": 6, "detail": "留住同伴的笑"}],
    ).for_shop("塞外朵莱娜")
    assert plan.product == "丝路香馕烤羊腿"
    form = production_form.initial_form("video", "塞外朵莱娜", "")
    form.update(template=silk_road.TEMPLATE, asset_ids=["dish"], reference_copy="一路回味")
    assert "一路回味" in wan_story.planning_instruction(form, "prepare")
    tool = wan_tool_for_action("prepare", silk_road.TEMPLATE)
    assert tool.id == "light_wan_story"
    refs = []
    for index, shot in enumerate(silk_road.case()["shots"], 1):
        req = silk_road.shot_request("light:one", index, shot, plan, "camera", ["dish"])
        assert req.model == "wan3.0-video"
        assert (req.ratio, req.resolution, req.generate_audio) == ("21:9", "720p", False)
        assert req.input_assets[0].role == "reference_video"
        if len(req.input_assets) == 2:
            assert req.input_assets[1].asset_id == "dish"
            refs.append(index)
        assert ("留住同伴的笑" in req.prompt) == (index == 6)
        assert "完整故事供 App 预览" not in req.prompt
        assert "沙中试吃硬馕" not in req.prompt
    assert refs == [1, 6, 7]


def test_silk_road_requires_the_full_ims_cut_and_measured_duration():
    def asset(name):
        return f"https://bucket.oss-cn-hangzhou.aliyuncs.com/assets/user/{name}/{name}.mp4"

    cuts = [{"shot": i, "source": f"take{i}", "duration_sec": duration}
            for i, duration in enumerate([8.1, 7.667, 9.567, 9.6, 10.967, 10.1, 4.4, 2, 2], 1)]
    black = asset(f"{silk_road.FORMAT}-black")
    shots = [shot if shot.asset == black else shot.model_copy(update={"asset": asset(shot.asset)})
             for shot in silk_road.cut_specs(cuts, black)]
    timeline = Timeline(
        canvas=silk_road.CANVAS, shots=shots, audio=[{"asset": asset("music"), "to_sec": 62.125}],
        overlays=[{"asset": asset(f"title{i}"), "kind": "image", "from_sec": i, "to_sec": i + 0.5}
                  for i in range(9)],
    )
    compiled = compile_ims(timeline, output_url=asset("output"), oss_region="cn-hangzhou")
    assert compiled.duration_sec == pytest.approx(62.125, abs=0.002)
    assert len(compiled.timeline["VideoTracks"][0]["VideoTrackClips"]) == 23
    job = SimpleNamespace(
        kind="compose", status="completed", output_asset_id="output", idempotency_key="light:one:final:abc",
        result_data={"duration_sec": 62.125},
        request_data={"timeline": timeline.model_dump(), "duration_sec": compiled.duration_sec},
    )
    run = {"remake_adaptation": {"format": silk_road.FORMAT}, "wan_cuts": cuts,
           "wan_final_key": job.idempotency_key}
    assert wan_story.is_complete_cut(run, job)
    run["silk_controls"] = {"beat_music": True}
    assert not wan_story.is_complete_cut(run, job)
    mix = timeline.model_copy(update={"audio": Timeline(shots=shots, audio=silk_road.beat_score()).audio})
    job.request_data["timeline"] = mix.model_dump()
    assert wan_story.is_complete_cut(run, job)
    assert mix.audio[0].in_sec == pytest.approx(50.62 - 50.56)
    assert mix.audio[1].volume < mix.audio[0].volume
    assert mix.audio[-1].to_sec - mix.audio[-1].fade_out_sec == pytest.approx(60.80)
    job.request_data["timeline"]["audio"][0]["in_sec"] = 0.7
    assert not wan_story.is_complete_cut(run, job)
    run["music_style"] = "upbeat"
    song = f"https://bucket.oss-cn-hangzhou.aliyuncs.com/music/catalog/12/{'a' * 64}.mp3"
    job.request_data["timeline"]["audio"] = [{"asset": song, "to_sec": 62.125}]
    job.request_data["soundtrack"] = {"track_id": "netease:12"}
    assert wan_story.is_complete_cut(run, job)
    job.request_data["soundtrack"]["track_id"] = "netease:13"
    assert not wan_story.is_complete_cut(run, job)
    del run["music_style"]
    del job.request_data["soundtrack"]
    run["silk_controls"] = {"beat_music": False}
    job.request_data["timeline"]["audio"] = []
    assert wan_story.is_complete_cut(run, job)
    del run["silk_controls"]
    job.request_data["timeline"] = timeline.model_dump()
    job.result_data["duration_sec"] = 4
    assert not wan_story.is_complete_cut(run, job)
    job.result_data["duration_sec"] = 62.125
    job.request_data["timeline"]["shots"] = timeline.model_dump()["shots"][-1:]
    assert not wan_story.is_complete_cut(run, job)


def test_silk_road_adapts_the_world_without_leaking_between_shots():
    from light.silk_road_lettering import artwork

    shots = silk_road.case()["shots"]
    takes = [{"setting": "餐厅石材弧墙" if i == 0 else "现代城市",
              "location_material": 2 if i == 0 else None, "action": "表演该段的连续动作"}
             for i in range(len(shots))]
    takes[5].update(setting="站台玻璃雨棚", action="两人打开纸包，递出盘子")
    plan = silk_road.Plan(
        title="给夜班带一份", product="丝路香馕烤羊腿", appearance="金褐色带骨羊肉",
        bite="一小块去骨羊肉",
        world={
            "premise": "在餐厅收工后给值班的同事送饭，途中错拿干馕",
            "look": "现代商业区，暖灯和冷青暮色", "cast": "阿哲短发、浅米夹克，岚姐铁锈棕外套",
            "takes": takes, "chapters": [{"title": "站台补送", "subtitle": "还热着"}] * 7,
        },
    )
    opening = silk_road.shot_request("test", 1, shots[0], plan, "motion1", ["food"],
                                     cast_ref="cast", location_ref="room")
    reveal = silk_road.shot_request("test", 6, shots[5], plan, "motion6", ["food"], cast_ref="cast")
    assert [ref.asset_id for ref in opening.input_assets] == ["motion1", "cast", "room", "food"]
    assert [ref.asset_id for ref in reveal.input_assets] == ["motion6", "cast", "food"]
    assert "灰度运镜板" in reveal.prompt
    assert "运镜板中的轮廓属于旧画面" in reveal.prompt
    assert "近景和反打也保持一致" in reveal.prompt
    assert "站台玻璃雨棚" in reveal.prompt
    assert "餐厅石材弧墙" not in reveal.prompt
    assert plan.world.premise not in reveal.prompt
    assert plan.world.look not in reveal.prompt
    assert "站台玻璃雨棚" in silk_road.display_script(plan)
    assert len(artwork(plan, "塞外朵莱娜")) == 9


@pytest.mark.parametrize("product,setting", [
    ("麻辣小龙虾", "雨夜玻璃公交站"),
    ("奶油海盐蛋糕", "晴天现代图书馆出口"),
])
def test_native_frame_plan_preserves_the_dish_and_separates_the_reveal(product, setting):
    from light.silk_road_world import endpoint_prompt
    from tool.light_remake import SilkRoadPrepareRequest

    plan = SilkRoadPrepareRequest(plan={
        "title": "打包留一份", "product": product, "appearance": "按实拍菜品的颜色和容器",
        "bite": "一小口", "world": {
            "premise": "搭档递给收工的同事一份", "cast": "阿宁短发蓝外套，阿岚白围裙",
            "look": "当代城市",
            "takes": [{"setting": setting, "action": "阿岚把餐盒递给阿宁",
                       "first_frame": "阿岚拿着盖好的餐盒", "last_frame": "阿宁接过掀开的餐盒"}] * 9,
            "chapters": [{"title": "一口留香", "subtitle": "还热着"}] * 7,
        },
    }).plan
    first = endpoint_prompt(plan, 6, "first_frame", has_cast=True, has_location=False, has_product=False)
    last = endpoint_prompt(plan, 6, "last_frame", has_cast=True, has_location=False, has_product=True)
    assert "合严" in first and product not in first
    assert product in last and setting in first and setting in last
    assert "阿宁接过掀开的餐盒" in last
    requests = [silk_road.framed_shot_request("light:test", index, plan, {
        "first_frame": f"asset_{index}_first", "last_frame": f"asset_{index}_last",
    }) for index in range(1, 10)]
    for req in requests:
        assert [ref.role for ref in req.input_assets] == ["first_frame", "last_frame"]
        assert req.ratio == "adaptive"
        assert setting in req.prompt
        assert "参考视频" not in req.prompt
    assert "指定的反应" in requests[4].prompt
    assert "动物" not in requests[4].prompt
    cuts = [{"shot": i, "source": f"take{i}", "duration_sec": req.duration, "frame_anchored": True}
            for i, req in enumerate(requests, 1)]
    timeline = silk_road.cut_specs(cuts, "black")
    aligned = silk_road.cut_specs(cuts, "black", align_handoff=True)
    reaction = silk_road.reaction_cut_index()
    assert [clip.asset for clip in timeline[reaction:reaction + 2]] == ["take9", "take6"]
    assert aligned == silk_road.reaction_after_reveal(timeline)
    assert [clip.asset for clip in aligned[reaction:reaction + 2]] == ["take6", "take9"]
    assert max(clip.in_sec + clip.duration_sec for clip in timeline if clip.asset == "take6") == pytest.approx(
        requests[5].duration, abs=0.001,
    )


def test_chapter_one_keeps_the_typefaces_stroke_proportions():
    from light.silk_road_lettering import glyph

    for width, height in [(119, 127), (187, 267)]:
        stroke = glyph("一", width, height).point(lambda level: 255 if level > 96 else 0)
        left, top, right, bottom = stroke.getbbox()
        assert (right - left) / (bottom - top) > 4
