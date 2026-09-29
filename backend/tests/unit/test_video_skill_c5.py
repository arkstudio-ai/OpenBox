"""C5 contract tests for the advisory spoken-video skill."""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parents[2] / ".openbox/skills/video-production"
SCRIPTS = SKILL / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_c5_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lint_prompt = _load("lint_prompt")
split_script = _load("split_script")
state = _load("state")


def test_split_script_respects_semantic_boundaries_and_limit():
    samples = (
        "你是不是也觉得小户型只能不断买柜子？先别急。真正的问题往往不是柜子少，而是台面上的东西没有分区。",
        "第一步，先保留每天都会用的东西；第二步，把低频用品收到同一个柜子里；最后统一容器颜色。",
        "产品好不好用，不能只看参数。今天就从手感、续航和清洁成本三个方面说清楚。",
    )
    for sample in samples:
        result = split_script.split_script(sample, max_chars=40)
        assert "".join(row["scriptText"] for row in result["segments"]) == sample
        assert all(0 < row["chars"] <= 40 for row in result["segments"])
        assert result["plan_shots_args"] == [
            value
            for row in result["segments"]
            for value in ("--line", row["scriptText"])
        ]


def test_split_script_reports_invalid_input_but_exits_zero():
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "split_script.py"), "--text", ""],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["error"]["code"] == "INVALID_INPUT"


def _lint(prompt: str, script_text: str = "今天看三个细节") -> dict:
    return lint_prompt.lint_prompt(
        script_text=script_text,
        prompt=prompt,
        visual_anchor="同一人物、同一街道",
        image_count=0,
        video_count=0,
    )


def test_lint_allows_deliberate_tracking_camera():
    prompt = (
        "全片一致的画面基底：同一人物、同一街道\n"
        "镜头跟随人物走动，保持半身构图。自然肢体动作：轻轻抬手。\n"
        "语气：轻快自然。\n口播台词：今天看三个细节\n无字幕。"
    )
    assert _lint(prompt)["ok"] is True


def test_lint_warns_instead_of_blocking_when_dialogue_differs():
    prompt = (
        "全片一致的画面基底：同一人物、同一街道\n"
        "固定镜头半身。自然肢体动作：轻轻抬手。语气：自然。\n"
        "口播台词：今天看四个细节\n无字幕。"
    )
    report = _lint(prompt)
    assert report["ok"] is True
    assert any("dialogue_mismatch" in warning for warning in report["warnings"])


def test_every_recipe_prompt_passes_lint_without_an_at_sign():
    """The recipes are what the agent copies; an `@` in one is read aloud as "艾特"."""
    text = (SKILL / "references" / "prompt-recipes.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)```", text, flags=re.S)
    spoken = [block for block in blocks if "口播台词：" in block]

    assert len(spoken) >= 6
    assert not [block for block in blocks if "@" in block]
    for block in spoken:
        line = block.split("口播台词：", 1)[1].splitlines()[0]
        report = lint_prompt.lint_prompt(
            script_text=line, prompt=block, visual_anchor="", image_count=2, video_count=1,
        )
        assert report["ok"] is True, (block, report["failures"])


def _confirmed_state() -> dict:
    data = {
        "slug": "demo",
        "script": "第一段。第二段。",
        "model": "selected-model",
        "resolution": "1080p",
        "confirmations": {},
        "shots": [
            {
                "index": 1,
                "script": "第一段。",
                "prompt": "prompt-1",
                "planned_seconds": "4",
                "assets": "参考视频1",
                "model": "selected-model",
                "resolution": "1080p",
                "job": "job-1",
                "path": "shot-1.mp4",
                "seconds": "4.2",
            },
            {
                "index": 2,
                "script": "第二段。",
                "prompt": "prompt-2",
                "planned_seconds": "6",
                "assets": "参考视频1",
                "model": "selected-model",
                "resolution": "1080p",
                "job": "job-2",
                "path": "shot-2.mp4",
                "seconds": "6.1",
            },
        ],
    }
    state.confirm(data, "script", "script approved")
    state.confirm(data, "shots", "shots and spend approved")
    return data


def _probe(*, shot_audio=True, shot_duration=None, final_audio=True, final_duration=10.3):
    def probe(path: str) -> dict:
        if path == "final.mp4":
            return {
                "ok": True,
                "has_audio": final_audio,
                "duration": final_duration,
                "error": "" if final_audio else "no audio",
            }
        duration = shot_duration if shot_duration is not None else (
            4.2 if "1" in path else 6.1
        )
        return {
            "ok": True,
            "has_audio": shot_audio,
            "duration": duration,
            "error": "" if shot_audio else "no audio",
        }
    return probe


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_state_check_names_only_the_edited_shot_and_invalidates_spend():
    data = _confirmed_state()
    data["script"] = "第一段。修改后的第二段。"
    data["shots"][1]["script"] = "修改后的第二段。"
    data["shots"][1]["prompt"] = "changed-prompt-2"

    report = state.check_state(data, probe=_probe())
    assert {"script_drift", "shots_drift"} <= _codes(report)
    drift = next(issue for issue in report["issues"] if issue["code"] == "shots_drift")
    assert "受影响段：2" in drift["message"]
    assert "费用确认同时作废" in drift["message"]


def test_state_check_treats_model_change_as_all_shots_affected():
    data = _confirmed_state()
    data["model"] = "another-model"
    report = state.check_state(data, probe=_probe())
    drift = next(issue for issue in report["issues"] if issue["code"] == "shots_drift")
    assert "受影响段：1,2" in drift["message"]


def _confirmed_frame_state() -> dict:
    data = {
        "slug": "frame-plan",
        "script": "第一段。第二段。第三段。",
        "model": "MiniMax-H3-Max-Turbo",
        "resolution": "768p",
        "shots": [],
    }
    for index, (first, last) in enumerate((("a", "b"), ("b", "c"), ("c", "d")), 1):
        data["shots"].append({
            "index": index,
            "script": f"第{index}段。",
            "prompt": f"口播台词：第{index}段。",
            "planned_seconds": "10",
            "seconds": "10.2",
            "assets": json.dumps({
                "input_assets": [
                    {"asset_id": first, "role": "first_frame"},
                    {"asset_id": last, "role": "last_frame"},
                ],
                "ratio": "adaptive",
            }, sort_keys=True),
            "model": data["model"],
            "resolution": data["resolution"],
            "job": f"job-{index}",
            "path": f"shot-{index}.mp4",
        })
    state.confirm(data, "script", "讲稿已确认")
    state.confirm(data, "shots", "首尾帧、分段及费用已确认")
    return data


@pytest.mark.parametrize("frame, affected", [
    ("a", "1"), ("b", "1,2"), ("c", "2,3"), ("d", "3"),
])
def test_frame_replacement_invalidates_only_shots_using_that_asset(frame, affected):
    """Shared endpoints are paid inputs to both neighbours, not just prep notes."""
    data = _confirmed_frame_state()
    jobs_before = [shot["job"] for shot in data["shots"]]
    for shot in data["shots"]:
        bindings = json.loads(shot["assets"])
        for ref in bindings["input_assets"]:
            if ref["asset_id"] == frame:
                ref["asset_id"] = f"{frame}-v2"
        shot["assets"] = json.dumps(bindings, sort_keys=True)

    report = state.check_state(data, probe=lambda _: {
        "ok": True, "has_audio": True, "duration": 10.2, "error": "",
    })

    assert _codes(report) == {"shots_drift"}
    assert f"受影响段：{affected}，原费用确认同时作废" in report["issues"][0]["message"]
    assert [shot["job"] for shot in data["shots"]] == jobs_before


def test_switching_to_text_video_records_empty_inputs_and_invalidates_quote():
    data = _confirmed_frame_state()
    for shot in data["shots"]:
        shot["assets"] = json.dumps({"input_assets": [], "ratio": "9:16"})

    report = state.check_state(data, probe=lambda _: {
        "ok": True, "has_audio": True, "duration": 10.2, "error": "",
    })

    assert _codes(report) == {"shots_drift"}
    assert "受影响段：1,2,3，原费用确认同时作废" in report["issues"][0]["message"]
    assert all(json.loads(shot["assets"])["input_assets"] == [] for shot in data["shots"])


def test_frame_preparation_and_roles_round_trip_through_existing_state_cli(tmp_path):
    """The documented JSON strings must survive the actual set/shot/show commands."""
    env = {"VIDEO_STATE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    command = [sys.executable, str(SCRIPTS / "state.py")]
    prep = {
        "model": "MiniMax-H3-Max-Turbo",
        "mode": "first_last",
        "status": "ready",
        "frames": {"A": "asset-a", "B": "asset-b", "C": "asset-c"},
        "confirmation_note": "用户确认三张图片及其片段用途",
    }
    subprocess.run(command + [
        "set", "--slug", "frames", "--key", "turbo_frame_prep",
        "--value", json.dumps(prep, ensure_ascii=False),
    ], env=env, capture_output=True, text=True, check=True)
    expected = []
    for index, (first, last) in enumerate((("asset-a", "asset-b"), ("asset-b", "asset-c")), 1):
        bindings = {
            "input_assets": [
                {"asset_id": first, "role": "first_frame"},
                {"asset_id": last, "role": "last_frame"},
            ],
            "ratio": "adaptive",
        }
        expected.append(bindings)
        subprocess.run(command + [
            "shot", "--slug", "frames", "--index", str(index),
            "--assets", json.dumps(bindings),
        ], env=env, capture_output=True, text=True, check=True)

    shown = subprocess.run(command + ["show", "--slug", "frames"], env=env,
                           capture_output=True, text=True, check=True)
    data = json.loads(shown.stdout)
    assert json.loads(data["turbo_frame_prep"]) == prep
    assert [json.loads(shot["assets"]) for shot in data["shots"]] == expected
    assert data["confirmations"] == {}  # image acceptance never approves video spend


def test_replacement_job_archives_old_result_without_inheriting_acceptance(tmp_path):
    env = {"VIDEO_STATE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    command = [sys.executable, str(SCRIPTS / "state.py")]

    def run(*args):
        return subprocess.run(command + list(args), env=env,
                              capture_output=True, text=True, check=True).stdout

    def current():
        return json.loads(run("show", "--slug", "retake"))["shots"][0]

    bindings = {
        "input_assets": [
            {"asset_id": "asset-a", "role": "first_frame"},
            {"asset_id": "asset-b", "role": "last_frame"},
        ],
        "ratio": "adaptive",
    }
    run("shot", "--slug", "retake", "--index", "1", "--job", "job-v1",
        "--path", "v1.mp4", "--asset", "video-v1", "--transcript", "旧台词",
        "--seconds", "15.2", "--accept", "接受旧片段的停顿",
        "--script", "计划台词", "--planned-seconds", "15",
        "--assets", json.dumps(bindings), "--prompt", "保持人物一致。口播台词：计划台词",
        "--model", "MiniMax-H3-Max-Turbo", "--resolution", "768p")
    original = current()
    # Resuming the same paid job must preserve its finished result exactly.
    run("shot", "--slug", "retake", "--index", "1", "--job", "job-v1")
    assert current() == original

    bindings["input_assets"][1]["asset_id"] = "asset-b-v2"
    run("shot", "--slug", "retake", "--index", "1", "--assets", json.dumps(bindings),
        "--prompt", "按新尾帧调整收尾手势。口播台词：计划台词")
    revised_plan = state.shot_plan(current())
    run("shot", "--slug", "retake", "--index", "1", "--job", "job-v2")
    pending = current()
    assert pending["job"] == "job-v2"
    assert not {"path", "asset", "transcript", "seconds", "accept"} & pending.keys()
    assert state.shot_plan(pending) == revised_plan
    assert json.loads(pending["assets"]) == bindings
    previous = pending["previous_takes"]
    assert len(previous) == 1
    assert previous[0]["replaced_at"]
    for field in ("job", "path", "asset", "transcript", "seconds", "accept"):
        assert previous[0][field] == original[field]

    run("shot", "--slug", "retake", "--index", "1", "--job", "job-v2",
        "--path", "v2.mp4", "--asset", "video-v2", "--transcript", "新台词",
        "--seconds", "15.1")
    finished = current()
    assert finished["previous_takes"] == previous
    assert finished["path"] == "v2.mp4" and finished["transcript"] == "新台词"
    assert "accept" not in finished


def test_state_check_reports_missing_job():
    data = _confirmed_state()
    data["shots"][0]["job"] = ""
    assert "shot_job_missing" in _codes(state.check_state(data, probe=_probe()))


def test_state_check_reports_shot_without_audio():
    assert "shot_audio_missing" in _codes(
        state.check_state(_confirmed_state(), probe=_probe(shot_audio=False))
    )


def test_state_check_reports_actual_duration_drift():
    data = _confirmed_state()
    data["shots"][0]["seconds"] = "8"
    assert "shot_duration_drift" in _codes(state.check_state(data, probe=_probe()))


def test_state_check_reports_final_without_audio():
    report = state.check_state(
        _confirmed_state(), final_path="final.mp4", probe=_probe(final_audio=False)
    )
    assert "final_audio_missing" in _codes(report)


def test_state_check_reports_final_duration_mismatch():
    report = state.check_state(
        _confirmed_state(), final_path="final.mp4", probe=_probe(final_duration=20)
    )
    assert "final_duration_mismatch" in _codes(report)


def test_state_check_cli_is_advisory_and_exits_zero(tmp_path):
    env = {"VIDEO_STATE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "state.py"), "check", "--slug", "empty"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["status"] == "attention"


def test_shot_accept_records_reason(tmp_path):
    env = {"VIDEO_STATE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    command = [sys.executable, str(SCRIPTS / "state.py")]
    subprocess.run(command + ["init", "--slug", "accept"], env=env, check=True)
    subprocess.run(
        command + [
            "shot", "--slug", "accept", "--index", "2",
            "--accept", "口头停顿可以接受",
        ],
        env=env,
        check=True,
    )
    shown = subprocess.run(
        command + ["show", "--slug", "accept"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    accepted = json.loads(shown.stdout)["shots"][0]["accept"]
    assert accepted["reason"] == "口头停顿可以接受"
    assert accepted["accepted_at"]


def test_skill_contains_all_u1_to_u9_user_experience_contracts():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    flowed = " ".join(text.split())

    assert "短到约 30 秒" in text and "长到 60–75 秒" in text
    assert "complete model prompt, character for character" in flowed
    assert "Until the person chooses “可以”, make zero paid submits" in flowed
    assert "Call `image_gen` only when the person explicitly asks" in flowed
    assert "受影响段" in text and "费用确认同时作废" in (SCRIPTS / "state.py").read_text()
    assert "max(2s, planned × 25%)" in text
    assert "Delivery means `share_file`" in text
    assert "禁图床 / 网盘" in text and "ssh -R" in text and "禁对外监听" in text
    assert "401/403" in text and "stop and explain" in flowed
    assert "HyperFrames is retired; never reintroduce it" in flowed
    assert "person_selected_model" in text and "Never silently change the model or tier" in flowed


def test_skill_can_invoke_real_question_cards_and_never_fakes_a_quote():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]

    assert "  - question\n" in frontmatter
    assert "A Markdown heading, table, or request for confirmation is not a card" in text
    assert "预计费用暂不可得（estimate 未返回金额）" in text
    assert 'never substitute "已产生费用 0 元" for the planned quote' in text
    assert "tool-call details do not count as display" in text


def test_lint_still_flags_uri_but_cli_exits_zero(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("参考 https://example.com/a.png", encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "lint_prompt.py"),
            "--prompt-file", str(prompt),
            "--script", "测试",
            "--anchor", "基底",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    report = json.loads(done.stdout)
    assert any(issue["code"] == "unsafe_asset_reference" for issue in report["issues"])
