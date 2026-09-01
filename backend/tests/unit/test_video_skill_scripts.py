"""The video skill's bundled scripts.

They carry craft that used to be enforced server-side — caption wrapping, the
STT comparison, the prompt rules. Moving them into the skill made them
editable by anyone who forks it, so they need to keep working on their own.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / ".openbox/skills/video-production/scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_skill_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_ass = _load("build_ass")
compare_transcript = _load("compare_transcript")
lint_prompt = _load("lint_prompt")


def test_every_bundled_script_exists_and_is_executable():
    expected = {
        "state.py",
        "lint_prompt.py",
        "compare_transcript.py",
        "build_ass.py",
        "compose.sh",
        "extract_audio.sh",
    }
    present = {item.name for item in SCRIPTS.iterdir() if item.is_file()}

    assert expected <= present
    for name in expected:
        assert (SCRIPTS / name).stat().st_mode & 0o111, name


def test_shell_scripts_parse():
    for name in ("compose.sh", "extract_audio.sh"):
        subprocess.run(["bash", "-n", str(SCRIPTS / name)], check=True)


# ── caption rendering ───────────────────────────────────────────────────────

def test_unspaced_chinese_is_wrapped_before_libass():
    """libass breaks on whitespace, and a Chinese line has none to break on."""
    wrapped = build_ass.wrap_subtitle_text(
        "今天教你三招让你的短视频一次就出片不用反复重拍也不用买课",
        width=720,
        font_size=32,
        side_margin=40,
    )

    assert r"\N" in wrapped


def test_an_english_product_name_is_not_split_mid_word():
    wrapped = build_ass.wrap_subtitle_text(
        "这个功能叫做 SuperResolution 非常好用啊真的很好用你一定要试试看",
        width=480,
        font_size=32,
        side_margin=24,
    )

    assert "SuperResolution" in wrapped.replace(r"\N", "")
    for line in wrapped.split(r"\N"):
        assert not line.endswith("Super")


def test_escaping_keeps_line_break_tokens_but_drops_stray_backslashes():
    escaped = build_ass.ass_escape("第一行\\N第二行 C:\\path {brace}")

    assert escaped.count(r"\N") == 1
    assert r"\\N" not in escaped
    assert r"\{" in escaped and r"\}" in escaped


def test_timestamps_are_ass_formatted():
    assert build_ass.ass_timestamp(0) == "0:00:00.00"
    assert build_ass.ass_timestamp(5.2) == "0:00:05.20"
    assert build_ass.ass_timestamp(3661.5) == "1:01:01.50"


def test_document_lays_captions_end_to_end_and_adds_the_channel_once():
    document = build_ass.build_document(
        durations=[5.0, 4.0],
        captions=["第一句", "第二句"],
        channel_name="频道",
        width=720,
        height=1280,
    )

    assert "0:00:00.00,0:00:05.00" in document
    assert "0:00:05.00,0:00:09.00" in document
    assert document.count("Style: Channel") == 1
    assert "● 频道" in document


def test_empty_caption_leaves_a_silent_gap_without_shifting_later_shots():
    document = build_ass.build_document(
        durations=[3.0, 2.0],
        captions=["", "第二句"],
        channel_name="",
        width=720,
        height=1280,
    )

    assert "0:00:03.00,0:00:05.00" in document
    assert document.count("Dialogue: 0") == 1


# ── transcript comparison ───────────────────────────────────────────────────

def test_punctuation_and_fillers_never_count_against_a_take():
    result = compare_transcript.compare("今天，教你三招。", "嗯今天教你三招")

    assert result["similarity"] == 1.0
    assert result["verdict"] == "ok"


def test_a_single_character_swap_stays_suspect_despite_a_high_ratio():
    """出片 → 出花 keeps the ratio up and changes the meaning completely."""
    result = compare_transcript.compare("今天教你三招出片", "今天教你三招出花")

    assert result["similarity"] > 0.85
    assert result["verdict"] == "suspect"
    assert any("片→花" in note for note in result["notes"])


def test_a_dropped_phrase_is_reported():
    result = compare_transcript.compare("第一步先打开设置页面", "第一步先打开页面")

    assert result["verdict"] == "suspect"
    assert any("漏念" in note for note in result["notes"])


# ── prompt advice ───────────────────────────────────────────────────────────

def _lint(prompt: str, script: str = "今天教你三招", **kw):
    return lint_prompt.lint_prompt(
        script_text=script,
        prompt=prompt,
        visual_anchor="同一主播、同一背景",
        image_count=kw.pop("images", 0),
        video_count=kw.pop("videos", 0),
        **kw,
    )


GOOD = (
    "全片一致的画面基底：同一主播、同一背景\n"
    "固定镜头。构图：竖屏 9:16 半身中景。\n"
    "自然肢体动作：抬手示意。\n"
    "语气：自然清晰。\n"
    "面对镜头说出@今天教你三招\n"
    "无字幕，字幕只能后期合成。"
)


def test_a_complete_spoken_prompt_passes():
    assert _lint(GOOD)["ok"] is True


def test_a_missing_verbatim_line_is_caught():
    report = _lint(GOOD.replace("@今天教你三招", "说点什么"))

    assert report["ok"] is False
    assert any(issue["code"] == "dialogue_exact" for issue in report["issues"])


def test_broll_drops_the_performance_rules_but_keeps_continuity():
    """A shot with nobody in it has no line, framing or tone to check."""
    bare = "全片一致的画面基底：同一主播、同一背景\n城市清晨的空镜。\n无字幕。"

    assert _lint(bare, script="城市空镜", speech=False)["ok"] is True
    assert _lint(bare)["ok"] is False


def test_a_reference_number_with_no_such_asset_is_caught():
    report = _lint(GOOD + "\n参考图片2 是产品图。", images=1)

    assert any(issue["code"] == "invalid_image_reference" for issue in report["issues"])


def test_urls_and_asset_ids_are_kept_out_of_prompt_text():
    report = _lint(GOOD + "\n参考 https://oss.example/a.png")

    assert any(issue["code"] == "unsafe_asset_reference" for issue in report["issues"])


def test_the_line_length_rule_warns_before_it_fails():
    warned = _lint(GOOD.replace("今天教你三招", "今" * 44), script="今" * 44)
    failed = _lint(GOOD.replace("今天教你三招", "今" * 60), script="今" * 60)

    assert warned["ok"] is True and warned["warnings"]
    assert failed["ok"] is False
    assert any(issue["code"] == "dialogue_too_long" for issue in failed["issues"])


# ── the loose notebook ──────────────────────────────────────────────────────

def test_state_records_shots_without_ordering_them(tmp_path):
    env = {"VIDEO_STATE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run(
        [sys.executable, str(SCRIPTS / "state.py"), *a], check=True, env=env,
        capture_output=True, text=True,
    )
    run("init", "--slug", "s", "--title", "t")
    run("shot", "--slug", "s", "--index", "3", "--job", "video_c")
    run("shot", "--slug", "s", "--index", "1", "--job", "video_a")
    shown = run("show", "--slug", "s").stdout

    data = json.loads(shown)
    assert [item["index"] for item in data["shots"]] == [1, 3]
    assert data["title"] == "t"


def test_skill_points_at_the_sandbox_path_not_a_relative_one():
    """The scripts run where bash runs, which is not where SKILL.md is served.

    SKILL.md is read from the backend host and wins over any sandbox copy, so
    a relative `scripts/...` path would resolve against the wrong machine and
    every invocation would fail with "no such file".
    """
    text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")

    assert "S=/opt/openbox/skills/video-production/scripts" in text
    for name in ("lint_prompt.py", "compare_transcript.py", "build_ass.py", "compose.sh"):
        assert f"$S/{name}" in text, name

    # The references are read in the sandbox too, so a relative path there
    # fails exactly the same way.
    for doc in (SCRIPTS.parent / "references").iterdir():
        body = doc.read_text(encoding="utf-8")
        assert "`scripts/" not in body, doc.name
        assert "\nscripts/" not in body, doc.name


def test_the_skill_shows_the_flags_each_script_actually_takes():
    """A usage-error round trip is avoidable: argparse needs the flag names."""
    text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")

    assert "--intended" in text and "--heard" in text
    assert "--prompt-file" in text and "--anchor" in text


def test_the_skill_says_to_fan_out_shots_rather_than_walk_them():
    """One-at-a-time submission multiplies the wait by the number of shots.

    A run on 2026-09-01 submitted four paid generations six minutes apart, and
    one of them was a whole-script single take the agent then discarded after
    splitting — a duplicate of shot 1, paid for twice over.
    """
    flowed = " ".join((SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8").split())

    assert "All in the same response" in flowed
    assert "Split first, then generate" in flowed
    # Concurrent shots finish out of order, so each needs its own number.
    assert "`shot=<N>`" in flowed


def test_the_skill_says_one_current_take_per_shot():
    """A retry left both takes of the same line in the delivered segment list.

    Two takes of one sentence reads as a broken video, not as a retry, so the
    rule is explicit: :v2 supersedes :v1 and only current takes are composed.
    """
    text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
    quality = (SCRIPTS.parent / "references/quality.md").read_text(encoding="utf-8")

    assert "One shot has exactly one current take" in text
    assert "supersedes" in text and "supersedes" in quality


def _plan(*lines, target=0, max_seconds=30):
    argv = [sys.executable, str(SCRIPTS / "plan_shots.py"),
            "--json", "--max-shot-seconds", str(max_seconds)]
    for line in lines:
        argv += ["--line", line]
    if target:
        argv += ["--target", str(target)]
    done = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_duration_follows_the_line_not_a_divided_total():
    """The model fills whatever time it is given.

    Measured 2026-09-01: a 13-character line asked to fill 5s came back with
    audible padding (2.6 chars/s), while 32 characters in 6s raced at 5.3.
    Both are the same mistake — a duration picked before the text was written.
    """
    plan = _plan("早上赶时间，也别随便对付早餐。",
                 "第三个办法，优先保证蛋白质：鸡蛋、牛奶、豆浆任选一样，再配主食和水果，更顶饱。")

    short, long = plan["shots"]
    assert short["seconds"] < long["seconds"]
    for shot in plan["shots"]:
        assert 3.0 <= shot["rate"] <= 5.0, shot


def test_a_short_line_never_gets_a_shot_that_is_mostly_silence():
    plan = _plan("好的。")

    assert plan["shots"][0]["seconds"] >= 3


def test_the_total_is_reported_not_forced_to_the_target():
    """Squeezing the script to hit the asked-for total is what caused padding."""
    plan = _plan(
        "早上赶时间，也别随便对付早餐。",
        "第一个办法，提前准备：晚上把鸡蛋煮好，搭配全麦面包和水果，拿了就走。",
        "第二个办法，选择免开火组合：无糖酸奶加燕麦，再放一把坚果，三分钟就能吃。",
        "第三个办法，优先保证蛋白质：鸡蛋、牛奶、豆浆任选一样，再配主食和水果，更顶饱。",
        "记住，早餐不用复杂，营养搭配好，快速也能吃得健康！",
        target=30,
    )

    assert plan["total_seconds"] > 30
    assert plan["drift_seconds"] > 0
    assert "Cut about" in plan["advice"]


def test_a_line_past_the_model_ceiling_says_to_split_it():
    plan = _plan("这是一句非常长的台词。" * 12, max_seconds=10)

    shot = plan["shots"][0]
    assert shot["seconds"] == 10
    assert "split this line" in shot["note"]


def test_latin_words_are_read_as_words_not_letters():
    """"SuperResolution" is one spoken word, not fifteen characters of speech."""
    plan = _plan("试试 SuperResolution 这个功能。")

    assert plan["shots"][0]["spoken_chars"] < 15


def test_the_skill_plans_shot_length_from_the_text():
    """Prose wraps, so match the sentence with its line breaks collapsed."""
    text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
    flowed = " ".join(text.split())

    assert "plan_shots.py" in flowed
    assert "Never divide the requested total by the shot count" in flowed


def test_pace_is_the_callers_choice_not_a_baked_in_constant():
    """A meditation script and a promo do not read at the same speed.

    The agent knows which one it just wrote; the script cannot infer it, so
    --rate is how that judgement reaches the arithmetic.
    """
    line = "下班回家，先别刷手机，试试这两个放松的方法，效果很好。"

    calm = _plan(line)["shots"][0]["seconds"]
    energetic = _plan(line)["shots"][0]["seconds"]
    assert calm == energetic  # same default

    import json as _json
    def at(rate):
        argv = [sys.executable, str(SCRIPTS / "plan_shots.py"), "--json",
                "--rate", str(rate), "--line", line]
        return _json.loads(subprocess.run(argv, capture_output=True, text=True,
                                          check=True).stdout)["total_seconds"]

    assert at(3.4) > at(4.6)


def test_the_skill_tells_the_agent_to_pick_a_pace():
    flowed = " ".join((SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8").split())

    assert "--rate" in flowed
    assert "Choose `--rate` from the piece you just wrote" in flowed
