"""Material choices stay distinct from picture preparation and paid plans."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.config import VideoModelConfig
from tool.video_production import _model_capability_lines
from sandbox import skill_runtime

SCRIPTS = Path(__file__).resolve().parents[2] / ".openbox/skills/video-production/scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location("material_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state = load_script("state")
lint = load_script("lint_prompt")


def project(mode="prepare_frames", *, continuous=True):
    shots = []
    for index in range(1, 4):
        refs = ([{"asset_id": "original-person", "role": "reference_image"}]
                if mode == "reference" else [] if mode == "text" else
                [{"asset_id": f"frame-{index if continuous else index * 2}", "role": "first_frame"},
                 {"asset_id": f"frame-{index + 1 if continuous else index * 2 + 1}", "role": "last_frame"}])
        shots.append({"index": index, "script": "一句台词", "prompt": "批准的提示词",
                      "planned_seconds": 10, "assets": json.dumps({"input_assets": refs}),
                      "job": f"paid-{index}", "path": f"shot{index}.mp4", "seconds": 10})
    data = {"model": "selected", "resolution": "720p", "script": "批准的讲稿", "shots": shots,
            "material_plan": {"mode": mode, "model": "selected", "resolution": "720p",
                              "source_uses": [{"asset_id": "original-person", "purpose": "人物身份"}],
                              "new_image_count": 4 if mode == "prepare_frames" and continuous else 6 if mode == "prepare_frames" else 0,
                              "joins": [{"left": n, "right": n + 1, "kind": "shared_frame" if continuous and mode == "prepare_frames" else "cut"} for n in (1, 2)]}}
    for kind in ("script", "materials", "shots"):
        state.confirm(data, kind, "真实用户答复的记录")
    return data


def report(data):
    return state.check_state(data, probe=lambda _: {"ok": True, "has_audio": True, "duration": 10})


def codes(data):
    return {issue["code"] for issue in report(data)["issues"]}


@pytest.mark.parametrize("mode", ["reference", "existing_frames", "prepare_frames", "text"])
def test_confirmed_routes_can_complete_without_other_material_modes(mode):
    data = project(mode)
    assert report(data)["status"] == "clear"


def test_continuous_three_shots_reuse_four_actual_frames():
    data = project()
    ids = {ref["asset_id"] for shot in data["shots"] for ref in json.loads(shot["assets"])["input_assets"]}
    assert len(ids) == 4
    assert report(data)["status"] == "clear"
    refs = json.loads(data["shots"][1]["assets"])
    refs["input_assets"][0]["asset_id"] = "similar-looking-but-different-frame"
    data["shots"][1]["assets"] = json.dumps(refs)
    assert {"materials_boundary_mismatch", "shots_drift"} <= codes(data)


def test_scene_cuts_can_use_six_independent_frames():
    data = project(continuous=False)
    ids = {ref["asset_id"] for shot in data["shots"] for ref in json.loads(shot["assets"])["input_assets"]}
    assert len(ids) == 6
    assert report(data)["status"] == "clear"


def test_first_only_does_not_require_a_last_frame():
    data = project("existing_frames", continuous=False)
    for shot in data["shots"]:
        refs = json.loads(shot["assets"])
        refs["input_assets"] = refs["input_assets"][:1]
        shot["assets"] = json.dumps(refs)
    state.confirm(data, "shots", "用户确认只用首帧")
    assert report(data)["status"] == "clear"


def test_upload_and_quote_do_not_substitute_for_material_choice():
    data = project("reference")
    del data["confirmations"]["materials"]
    assert "materials_unconfirmed" in codes(data)


@pytest.mark.parametrize("change", ["scope", "model", "purpose"])
def test_preparation_scope_model_or_intent_changes_invalidate_both_decisions(change):
    data = project()
    if change == "scope":
        data["material_plan"]["new_image_count"] += 1
    elif change == "model":
        data["model"] = "new-route"
    else:
        data["material_plan"]["source_uses"][0]["purpose"] = "整幅画面"
    assert {"materials_drift", "shots_drift"} <= codes(data)
    assert "受影响段：1,2,3" in next(item["message"] for item in report(data)["issues"] if item["code"] == "shots_drift")


def test_changing_shared_boundary_marks_both_neighbors_only():
    data = project()
    for index, role in ((0, "last_frame"), (1, "first_frame")):
        refs = json.loads(data["shots"][index]["assets"])
        for ref in refs["input_assets"]:
            if ref["role"] == role:
                ref["asset_id"] = "new-approved-B"
        data["shots"][index]["assets"] = json.dumps(refs)
    drift = next(item for item in report(data)["issues"] if item["code"] == "shots_drift")
    assert "受影响段：1,2，" in drift["message"]
    assert "materials_boundary_mismatch" not in codes(data)


def test_reference_mode_cannot_silently_become_frame_or_image_preparation():
    data = project("reference")
    data["material_plan"]["new_image_count"] = 1
    data["shots"][0]["assets"] = json.dumps({"input_assets": [{"asset_id": "original-person", "role": "first_frame"}]})
    assert {"materials_role_mismatch", "materials_unapproved_preparation"} <= codes(data)


def test_json_string_plan_and_legacy_paid_state_remain_supported():
    data = project()
    data["material_plan"] = json.dumps(data["material_plan"], ensure_ascii=False)
    assert report(data)["status"] == "clear"
    del data["material_plan"]
    del data["confirmations"]["materials"]
    state.confirm(data, "shots", "已有已付费任务")
    assert report(data)["status"] == "clear"


@pytest.mark.parametrize("value", ["{bad-json", [], None, {"mode": []}])
def test_invalid_plan_is_advisory_instead_of_crashing(value):
    data = project()
    data["material_plan"] = value
    assert any(code.startswith("materials_") for code in codes(data))


def test_cli_records_material_choice_and_checks_without_submitting(tmp_path):
    env = dict(os.environ, VIDEO_STATE_ROOT=str(tmp_path))
    base = [sys.executable, str(SCRIPTS / "state.py")]
    for args in (["set", "--key", "material_plan", "--value", json.dumps(project()["material_plan"])],
                 ["confirm", "--kind", "materials", "--note", "用户确认素材用途"]):
        subprocess.run([*base, *args, "--slug", "demo"], env=env, check=True, capture_output=True)
    done = subprocess.run([*base, "check", "--slug", "demo"], env=env, check=True, capture_output=True, text=True)
    assert "materials_unconfirmed" not in {item["code"] for item in json.loads(done.stdout)["issues"]}


@pytest.mark.parametrize("camera,framing", [("镜头环绕人物", "全景"), ("镜头推进", "特写"), ("camera follows", "wide shot"), ("镜头设计：低机位跟拍", "远景")])
def test_identity_reference_allows_new_camera_and_scene(camera, framing):
    prompt = f"全片一致的画面基底：参考图片1中的同一人物\n画面中不出现任何文字、字幕、标题、水印\n{camera}，{framing}，人物走进新的门店，自然肢体动作：推门。语气：自然。\n口播台词：欢迎进店看看"
    assert lint.lint_prompt(script_text="欢迎进店看看", prompt=prompt, visual_anchor="同一人物", image_count=1, video_count=0)["ok"]


@pytest.mark.parametrize("model,channel,shape,video,frames", [
    ("MiniMax-H3", "sd2", "size", False, False),
    ("MiniMax-H3", "sd2", None, False, False),
    ("video-sd-720p-proⅠ", "sd2", "flat", False, False),
    ("doubao-seedance-2-0-260128", "sd2", "metadata", True, True),
    ("wan3.0-video", "dashscope", None, True, True),
    ("MiniMax-H3-Max-Turbo", "runninghub", "flat", False, True),
])
def test_menu_describes_this_adapter_instead_of_other_vendor_endpoints(model, channel, shape, video, frames):
    entry = VideoModelConfig(id=model, channel=channel, supports_reference_video=True, supports_reference_audio=True, supports_first_last_frame=True)
    entry = SimpleNamespace(**{**entry.model_dump(), "wire_shape": shape})
    config = SimpleNamespace(video_generation=SimpleNamespace(model=model, models=[entry]))
    line = _model_capability_lines(config)[-1].split("routing=")[0]
    assert ("reference video" in line) == video
    assert ("first/last frame" in line) == frames


def _fixture_commands(root):
    # Exercise the actual transport in an isolated unprivileged filesystem;
    # model the root ownership of production without changing runtime guards.
    for command in skill_runtime.video_skill_commands(remote_root=str(root)):
        source = shlex.split(command)[-1]
        source = source.replace("if os.geteuid()!=0:", "if False:").replace("info.st_uid!=0", "info.st_uid!=os.geteuid()")
        source = source.replace("for item in (path,*path.parents):", "for item in (path,):")
        yield source


def test_skill_publication_updates_old_guests_and_preserves_user_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("new workflow")
    (source / "scripts/state.py").write_text("print('new runtime')")
    monkeypatch.setattr(skill_runtime, "SKILL_ROOT", source)
    root = tmp_path / "skills/video-production"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("old workflow")
    personal = tmp_path / "skills/personal/SKILL.md"
    personal.parent.mkdir()
    personal.write_text("user-owned instructions")
    commands = list(_fixture_commands(root))
    for _ in range(2):  # A repeated delivery cannot replace its old backup.
        for code in commands:
            exec(code, {})
    assert (root / "SKILL.md").read_text() == "new workflow"
    assert personal.read_text() == "user-owned instructions"
    backups = list((root.parent / ".video-production-releases").rglob("original/SKILL.md"))
    assert len(backups) == 1 and backups[0].read_text() == "old workflow"
    (root / "SKILL.md").write_text("third-party-change")
    with pytest.raises(RuntimeError, match="preimage changed"):
        exec(commands[-1], {})
    assert (root / "SKILL.md").read_text() == "third-party-change"


async def test_new_desktop_sync_rechecks_authority_for_each_command(monkeypatch):
    run = AsyncMock()
    authority = AsyncMock()
    monkeypatch.setattr("sandbox.channel.run_desktop_command", run)
    monkeypatch.setattr(skill_runtime, "video_skill_commands", lambda: ["chunk", "publish"])
    await skill_runtime.ensure_desktop_video_skill("ecd-new", authority_check=authority)
    assert authority.await_count == run.await_count == 2
    authority.side_effect = RuntimeError("binding changed")
    with pytest.raises(RuntimeError, match="binding changed"):
        await skill_runtime.ensure_desktop_video_skill("ecd-new", authority_check=authority)
    assert run.await_count == 2


def test_real_bundle_transport_fits_ecd_payload_limits():
    commands = skill_runtime.video_skill_commands()
    assert commands
    for command in commands:
        compile(shlex.split(command)[-1], "video-skill-transport", "exec")
