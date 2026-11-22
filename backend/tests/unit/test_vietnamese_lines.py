"""Vietnamese (and other spaced-script) lines through the video skill, the caption fitter and the default address."""
import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / ".openbox" / "skills" / "video-production" / "scripts"
LINE = "Chào mọi người! Hôm nay quán mình có món phở bò đặc biệt, mời cả nhà ghé thử nhé!"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_vi_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_vietnamese_syllable_counts_once_not_per_letter():
    plan = _load("plan_shots")
    # 19 syllables at 1.2: "người" is one word, where the ASCII-only pattern saw "ng" and "i".
    assert plan.spoken_length(LINE) == 19 * 1.2
    seconds, note = plan.shot_duration(LINE, rate=4.0, max_seconds=15)
    assert seconds == 7 and note is None
    assert plan.spoken_length("早上赶时间，也别随便对付早餐。") == 13
    assert plan.spoken_length("Welcome to our shop") == 8.0


def test_splitter_keeps_words_and_spaces_whole():
    split = _load("split_script")
    text = LINE + " Cuối tuần này giảm giá ba mươi phần trăm cho tất cả các món. Đừng bỏ lỡ nha!"
    result = split.split_script(text, max_chars=24)
    lines = [row["line"] for row in result["segments"]]
    assert " ".join(lines).split() == text.split()
    assert all(row["chars"] <= 24 for row in result["segments"])
    assert "  " not in "".join(lines)
    long_run = split.split_script("một hai ba bốn năm sáu bảy tám chín mười mười một mười hai", max_chars=8)
    assert all(not row["line"].startswith(" ") and row["chars"] <= 8 for row in long_run["segments"])


def test_chinese_script_splits_as_before():
    split = _load("split_script")
    result = split.split_script("早上赶时间，也别随便对付早餐。第一个办法，提前准备：晚上把鸡蛋煮好。", max_chars=20)
    assert [row["line"] for row in result["segments"]] == ["早上赶时间，也别随便对付早餐。", "第一个办法，提前准备：晚上把鸡蛋煮好。"]
    assert [row["chars"] for row in result["segments"]] == [15, 19]


def test_heard_line_matches_across_case_and_unicode_form():
    import unicodedata
    compare = _load("compare_transcript")
    heard = unicodedata.normalize("NFD", "Chào mọi người, hôm nay quán mình có món phở bò đặc biệt, mời cả nhà ghé thử nhé!")
    report = compare.compare(LINE, heard)
    assert report["similarity"] == 1.0 and report["verdict"] == "ok"
    wrong = compare.compare(LINE, LINE.replace("phở bò", "phở bỏ"))
    assert wrong["verdict"] == "suspect"

    from tool.video_production import normalize_spoken_text
    assert normalize_spoken_text("Hôm nay!") == normalize_spoken_text(unicodedata.normalize("NFD", "hôm nay"))
    assert normalize_spoken_text("今天，嗯，上新了。") == "今天上新了"


def test_lint_length_advice_counts_speech_not_letters():
    lint = _load("lint_prompt")
    assert lint._spoken_units(LINE) == 23
    assert lint._spoken_units("早上赶时间，也别随便对付早餐。") == 13


def test_caption_wraps_between_vietnamese_words():
    from video.ims_compiler import _fit_line
    from video.timeline import Cue, Timeline

    timeline = Timeline.model_validate({"shots": [{"asset": "oss://bucket/a.mp4"}]})
    cue = Cue(from_sec=0, to_sec=6, text="Hôm nay quán mình có món phở bò đặc biệt mời cả nhà ghé thử nhé và đừng quên rủ bạn bè")
    pieces = _fit_line(cue, timeline)
    assert len(pieces) > 1
    assert " ".join(piece.text for piece in pieces).split() == cue.text.split()
    assert pieces[0].from_sec == 0 and pieces[-1].to_sec == 6


def test_default_address_follows_the_chosen_interface_language():
    from assistant.identity import default_address, identity_lines
    from assistant.profile import Profile, prompt_section
    from memory.packet import _settings_from_extra

    assert default_address(None) == "老板" and default_address("zh-CN") == "老板"
    assert default_address("vi-VN") == "anh/chị" and default_address("en-US") == "boss"
    assert "Address the user as「anh/chị」" in "\n".join(identity_lines("", "", ui_language="vi-VN"))
    assert "Address the user as「王老师」" in "\n".join(identity_lines("", "王老师", ui_language="vi-VN"))
    assert "Address the user as「老板」" in prompt_section(Profile())
    assert "Address the user as「anh/chị」" in prompt_section(Profile(), ui_language="vi-VN")
    assert _settings_from_extra({"ui_language": "vi-VN"})["address"] == "anh/chị"
    assert _settings_from_extra({})["address"] == "老板"
