"""A shot in which someone speaks carries the exact words, or it is refused.

Measured on gw2 (2026-10-10..11-09): shots under the video-production skill
always carried 口播台词： (116/116); 532 of 1210 shots overall had audio on and
no line, from ordinary chats describing someone talking. The model then invents
the words or keeps the mouth shut, and a dub can never match. This is the one
prompt rule the tool enforces itself rather than leaving to skill text.
"""
from types import SimpleNamespace

import pytest

import tool.video_production as video_mod
from tool.video_production import VideoGenerateArgs, resolve_spoken_line
from tool.video_providers import VideoRequestError


def test_spoken_line_is_appended_as_the_label_line():
    prompt, line = resolve_spoken_line(
        "人物面对镜头开口说话，语气亲切", spoken_line="今天教你三招", no_speech=False, generate_audio=True,
    )
    assert prompt.endswith("\n口播台词：今天教你三招")
    assert line == "今天教你三招"


def test_prompt_that_already_carries_the_label_is_accepted_as_is():
    text = "全片一致的画面基底：同一人物\n人物面对镜头开口说话。\n口播台词：今天教你三招"
    prompt, line = resolve_spoken_line(text, spoken_line=None, no_speech=False, generate_audio=True)
    assert prompt == text
    assert line == "今天教你三招"
    # The same line passed twice (punctuation aside) is not a conflict.
    prompt, line = resolve_spoken_line("口播台词：你好呀！", spoken_line="你好呀", no_speech=False, generate_audio=True)
    assert prompt == "口播台词：你好呀！" and line == "你好呀！"


@pytest.mark.parametrize("prompt", [
    "3D anime. Girl A's eyes sparkle as she speaks in clear Mandarin",
    "主播对镜头讲解这款面霜的三个卖点",
    "两个女孩在教室对话，气氛轻松",
    "画面+旁白：镜头缓慢推进草原上的烤全羊",
])
def test_speech_without_words_is_refused_before_paying(prompt):
    with pytest.raises(VideoRequestError, match="spoken_line"):
        resolve_spoken_line(prompt, spoken_line=None, no_speech=False, generate_audio=True)


@pytest.mark.parametrize("prompt", [
    "2D anime animation. The character raises hands up cheering. 画面中不出现任何文字、字幕、标题、水印；字幕只能后期合成。",
    "竖屏美食广告，师傅片下羊肉，宾客举杯",
    "每段一个画面 prompt，无人物开口，旁白后期加",
    "两个女孩在教室对话，不让模型开口，对白做字幕",
    "A girl talks to her friend, no dialogue is heard, ambient sound only",
])
def test_silent_shots_and_declared_silence_pass(prompt):
    out, line = resolve_spoken_line(prompt, spoken_line=None, no_speech=False, generate_audio=True)
    assert out == prompt and line is None


def test_no_speech_flag_and_audio_off_skip_the_cue_check():
    assert resolve_spoken_line("主播对镜头讲解", spoken_line=None, no_speech=True, generate_audio=True)[1] is None
    assert resolve_spoken_line("主播对镜头讲解", spoken_line=None, no_speech=False, generate_audio=False)[1] is None


def test_contradictions_and_bad_lines_are_refused():
    with pytest.raises(VideoRequestError, match="contradict"):
        resolve_spoken_line("x", spoken_line="你好", no_speech=True, generate_audio=True)
    with pytest.raises(VideoRequestError, match="followed by nothing"):
        resolve_spoken_line("口播台词：", spoken_line=None, no_speech=False, generate_audio=True)
    with pytest.raises(VideoRequestError, match="differs"):
        resolve_spoken_line("口播台词：你好呀", spoken_line="再见", no_speech=False, generate_audio=True)
    with pytest.raises(VideoRequestError, match="no_speech=true"):
        resolve_spoken_line("口播台词：你好", spoken_line=None, no_speech=True, generate_audio=True)
    # The model reads @ aloud (production STT heard 艾特 before the line).
    with pytest.raises(VideoRequestError, match="@"):
        resolve_spoken_line("主播讲解 @南宁的姐妹", spoken_line="姐妹们看过来", no_speech=False, generate_audio=True)
    # A spoken shot with audio off would be a silent mouth.
    with pytest.raises(VideoRequestError, match="generate_audio"):
        resolve_spoken_line("人物开口说话", spoken_line="你好", no_speech=False, generate_audio=False)


@pytest.mark.asyncio
async def test_open_submission_carries_the_resolved_prompt_and_line(monkeypatch):
    async def none(_ctx):
        return ""

    monkeypatch.setattr(video_mod, "_session_video_model_id", none)
    monkeypatch.setattr(video_mod, "_session_video_resolution", none)
    settings = SimpleNamespace(model="doubao-seedance-2-0-260128", default_resolution="720p", default_ratio="9:16",
                               default_duration=8, default_generate_audio=True, default_watermark=False)
    monkeypatch.setattr("core.config.get_config", lambda: SimpleNamespace(video_generation=settings))
    monkeypatch.setattr("tool.video_providers.declared_model", lambda *_a, **_k: None)
    ctx = SimpleNamespace(session_id="s1", user_id="u1")

    approved = await video_mod._resolve_open_submission(
        VideoGenerateArgs(action="submit", idempotency_key="k:shot1:v1", prompt="人物面对镜头开口说话",
                          spoken_line="今天教你三招"), ctx)
    assert approved["prompt"].endswith("口播台词：今天教你三招")
    assert approved["spoken_line"] == "今天教你三招"

    with pytest.raises(VideoRequestError, match="spoken_line"):
        await video_mod._resolve_open_submission(
            VideoGenerateArgs(action="validate", prompt="主播对镜头讲解这款面霜"), ctx)


def test_schema_exposes_the_two_fields_within_budget():
    schema = VideoGenerateArgs.model_json_schema()
    assert "spoken_line" in schema["properties"] and "no_speech" in schema["properties"]
    assert "spoken_line" not in schema.get("required", [])
    import json
    assert len(json.dumps(schema, ensure_ascii=False)) + len(video_mod.VIDEO_GENERATE_DESCRIPTION) < 10_000
