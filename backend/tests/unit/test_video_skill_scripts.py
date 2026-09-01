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
