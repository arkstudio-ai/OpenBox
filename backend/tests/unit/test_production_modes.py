from types import SimpleNamespace

import pytest

from light import production_form as forms, video_language
from models.message import TextPart
from video import shot_review


def session(form):
    return SimpleNamespace(light_production=form, light_entry="video", is_deleted=False)


def test_language_is_frozen_for_render_and_overrides_reference_copy():
    form = forms.initial_form("video", "", "中文参考文案。")
    assert not form["perfect_film"]
    assert video_language.for_form(form, "vi-VN") == "vi"
    form["output_language"] = "巴西葡萄牙语"
    form["run"] = {"output_language": video_language.output_name(form, "en-US")}
    assert '巴西葡萄牙语' in video_language.task_direction(form, "vi-VN")
    assert "spoken_line" in video_language.task_direction(form)
    assert "字幕" in video_language.task_direction(form)


@pytest.mark.parametrize("perfect", [False, True])
def test_tool_access_and_background_worker_follow_confirmed_mode(perfect):
    form = forms.initial_form("video", "", "")
    form.update(builtin_h3=True, template_recipe={}, run={"action": "render",
                "job_prefix": "light:s:2", "perfect_film": perfect})
    task = session(form)
    tools = {name: SimpleNamespace(id=name) for name in (
        "video_generate", "video_compose", "video_analyze", "footage_analyze", "video_transcribe")}
    accessible = forms.restrict_guided_tools(tools, task, "render")
    assert {"video_generate", "video_compose", "video_transcribe"} <= accessible.keys()
    assert ("video_analyze" in accessible) is perfect
    assert ("footage_analyze" in accessible) is perfect
    assert bool(shot_review._group(task)) is perfect
    instruction = forms.turn_instruction(task, "render")
    assert ("跳过所有生成片段" in instruction) is not perfect
    assert ("仅修复不合格镜头" in instruction) is perfect
    recovery = TextPart(text="provider receipt", origin="system_recovery", origin_ref={
        "entrypoint": "light_video_resume", "job_prefix": "light:s:2"})
    assert forms.turn_action(task, SimpleNamespace(parts=[recovery])) == "render"
    recovery.origin_ref["job_prefix"] = "light:s:1"
    assert forms.turn_action(task, SimpleNamespace(parts=[recovery])) is None
