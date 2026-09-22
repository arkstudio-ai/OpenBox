"""C5 contract tests for the advisory spoken-video skill."""
import importlib.util
import json
import re
import subprocess
import sys
from skill.builtin import builtin_directory


SKILL = builtin_directory("video-production")
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
