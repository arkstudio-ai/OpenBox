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
    job.result_data["duration_sec"] = 4
    assert not wan_story.is_complete_cut(run, job)
    job.result_data["duration_sec"] = 62.125
    job.request_data["timeline"]["shots"] = timeline.model_dump()["shots"][-1:]
    assert not wan_story.is_complete_cut(run, job)
