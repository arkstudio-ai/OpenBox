import io
import math
import wave
from array import array
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from light import commercial_plan, commercial_post as post, soda_story
from light.remake import RemakeStopped


def test_asr_source_offsets_are_measured_after_retiming():
    cut = {"start": 36, "output_frames": 32, "speed": 1.25}
    assert post._align_range(cut, [1.75, 2.5]) == pytest.approx((.2, .8))
    assert post._align_range(cut, [1.4, 2.5]) is None
    assert post._align_range(cut, [1.75, 4.5]) is None


def _sound(duration):
    rate = 24000
    samples = array("h", (0 if t < .17 or t > duration - .17 else round(9000 * math.sin(t * 1400))
                          for t in (i / rate for i in range(round(rate * duration)))))
    saved = io.BytesIO()
    with wave.open(saved, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(samples.tobytes())
    return saved.getvalue()


def _beat_strength(pcm, bpm):
    # Five-millisecond energy changes, correlated with four successive beats.
    envelope = [sum(map(abs, pcm[i:i + 20])) / 20 for i in range(0, len(pcm) - 19, 20)]
    onset = [max(0., value - previous) for previous, value in zip(envelope, envelope[1:])]
    mean = sum(onset) / len(onset)
    centered = [value - mean for value in onset]
    energy = sum(value * value for value in centered)
    strength = 0.
    for multiple in range(1, 5):
        delay = round(multiple * 60 * 200 / bpm)
        strength += sum(a * b for a, b in zip(centered, centered[delay:])) / (energy * multiple)
    return strength


@pytest.mark.asyncio
async def test_28_cuts_export_native_and_dubbed_lines_and_readable_vietnamese(tmp_path, monkeypatch):
    # Identical flat-color fixture source makes actual burn-in pixels and music/ambience changes measurable.
    source = tmp_path / "camera.mp4"
    await post.video.ff("-f", "lavfi", "-i", "color=c=0x14363e:s=160x90:r=24:d=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6:sample_rate=48000",
                        "-c:v", "libx264", "-threads:v", 2, "-c:a", "aac", source)
    spoken = {23: "Tôi tới ngay.", 25: "Sao giờ này cậu mới đến?"}
    shots = [{"id": i, "frames": beat.frames, "spoken_line": spoken.get(i, "")}
             for i, beat in enumerate(commercial_plan.BEATS, 1)]
    monkeypatch.setattr(commercial_plan, "shots", lambda plan: shots)
    cuts = []
    for shot in shots:
        i = shot["id"]
        (tmp_path / f"source-{i:02}.mp4").symlink_to(source)
        cuts.append({"shot": i, "start": 6, "frames": shot["frames"], "output_frames": shot["frames"],
                     "speed": 1, "has_audio": i == 23, "cues": []})
    cuts[22]["cues"] = [{"text": spoken[23], "from_sec": .50, "to_sec": 1.43}]
    ctx = SimpleNamespace(user_id="user", workspace_id="work", session_id="session")
    render = AsyncMock(return_value="speech.wav")
    monkeypatch.setattr(post.production_voice, "render_clip", render)
    register = AsyncMock(return_value="asr-input")
    monkeypatch.setattr(post, "_reference_asset", register)
    transcribe = AsyncMock(return_value={"text": spoken[25]})
    monkeypatch.setattr(soda_story, "_transcript", transcribe)
    monkeypatch.setattr(post, "get_oss", lambda: SimpleNamespace(get_object=AsyncMock(return_value=_sound(2.25))))
    plan = SimpleNamespace(language="vi", product="Bình nước", title="Nhịp phố", end_copy="", score_revision=2)
    film, measured = await post.assemble(tmp_path, plan, cuts, ctx=ctx)
    render.assert_awaited_once_with(user="user", workspace="work", session="session", model=post.production_voice.MODEL,
                                    voice="xunanchuan_v3.1", text=spoken[25], locale="vi")
    assert register.await_args.args[2].startswith(b"ID3") and register.await_args.kwargs["mime"] == "audio/mpeg"
    transcribe.assert_awaited_once_with(ctx, "asr-input", language="vi")
    assert measured["frames"] == 1277 and measured["fps"] == 24 and measured["has_audio"]
    assert measured["shot_frames"] == [beat.frames for beat in commercial_plan.BEATS]
    score = measured["score"]
    assert score == post.score_for(plan) and (score["master_bpm"], score["bpm"]) == (101, 128)
    assert (score["music_in_frame"], score["music_duck_frame"], score["music_return_frame"]) == (138, 786, 1118)
    assert score["match_frames"] == [365, 528, 836]
    assert post.score_for(None)["bpm"] == 101
    assert "CC BY 4.0" in measured["attribution"]
    a, b = measured["dialogue_sources"]
    assert (a["shot"], a["method"], a["from_sec"]) == (23, "native_asr", 33.75)
    assert (b["shot"], b["method"]) == (25, "tts")
    assert b["asr_text"] == spoken[25] and b["voice_id"] == "xunanchuan_v3.1"
    assert 1.7 < b["to_sec"] - b["from_sec"] < 2.15
    subtitle = await post.video.ff("-i", film, "-map", "0:s:0", "-f", "srt", "-")
    assert all(text in subtitle.decode() for text in spoken.values()) and subtitle.count(b"-->") == 2
    assert "00:00:33,750" in measured["subtitles_srt"]

    async def bright_subtitle_pixels(time):
        data = await post.video.ff("-ss", time, "-i", film, "-vf", "crop=1100:95:90:590,format=gray",
                                   "-frames:v", 1, "-f", "rawvideo", "-")
        return sum(pixel > 190 for pixel in data)

    assert await bright_subtitle_pixels("34") > 150
    assert await bright_subtitle_pixels("47") == 0

    async def rms(time):
        pcm = array("h")
        pcm.frombytes(await post.video.ff("-ss", time, "-i", film, "-map", "0:a", "-t", "0.35",
                                          "-ac", 1, "-f", "s16le", "-"))
        return math.sqrt(sum(v * v for v in pcm) / len(pcm))

    quiet = await rms("35.9")
    assert await rms("10") > 3 * quiet and await rms("49") > 3 * quiet
    for start, duration in ((8, 23), (48, 5)):
        pcm = array("h")
        pcm.frombytes(await post.video.ff("-ss", start, "-i", film, "-map", "0:a", "-t", duration,
                                          "-af", "highpass=f=500", "-ac", 1, "-ar", 4000, "-f", "s16le", "-"))
        assert _beat_strength(pcm, 128) > _beat_strength(pcm, 101) + .04


@pytest.mark.asyncio
async def test_rejects_short_frame_bank_before_provider_or_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(commercial_plan, "shots", lambda plan: [{"frames": beat.frames} for beat in commercial_plan.BEATS])
    with pytest.raises(RemakeStopped, match="khung hình"):
        await post.assemble(tmp_path, SimpleNamespace(language="vi"), [], ctx=None)


@pytest.mark.asyncio
async def test_overlong_dub_is_never_truncated_to_make_a_finished_video(tmp_path, monkeypatch):
    monkeypatch.setattr(post, "get_oss", lambda: SimpleNamespace(get_object=AsyncMock(return_value=_sound(4.5))))
    monkeypatch.setattr(post, "_synthesize", AsyncMock(return_value=None))
    cut = {"shot": 23, "start": 0, "output_frames": 32, "speed": 1,
           "has_audio": False, "cues": [], "dub": {"key": "voice.wav"}}
    with pytest.raises(RemakeStopped, match="trọn câu thoại"):
        await post._voice(tmp_path, SimpleNamespace(language="vi"), cut, "Tôi tới ngay.", 804, None)


def test_dub_cast_follows_the_actual_actors_including_a_female_authority():
    plan = SimpleNamespace(language="vi", call_voice_gender="male", rebuke_voice_gender="female")
    assert post._voice_choices(plan, 23) == ["xunanchuan_v3.1", "qwen-audio-3.1-tts-flash-may_vi"]
    assert post._voice_choices(plan, 25) == ["longanlingxin_v3.1", "longanhuan_v3.1", "longanfengyue_v3.1"]
    plan.language = "en"
    assert post._voice_choices(plan, 23) == ["Brian_v3.1", "Eric_v3.1"]
    assert post._voice_choices(plan, 25) == ["Emily_v3.1", "Luna_v3.1"]


@pytest.mark.asyncio
async def test_silent_native_and_wrong_vietnamese_tone_use_another_voice_and_fit_on_camera(tmp_path, monkeypatch):
    await post.video.ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2", "-c:a", "aac",
                        tmp_path / "source-23.mp4")
    render = AsyncMock(return_value="replacement.wav")
    monkeypatch.setattr(post.production_voice, "render_clip", render)
    monkeypatch.setattr(post, "get_oss", lambda: SimpleNamespace(get_object=AsyncMock(return_value=_sound(1.57))))
    monkeypatch.setattr(post, "_hear_dub", AsyncMock(side_effect=["Tôi tới ngày.", "Tôi tới ngay."]))
    plan = SimpleNamespace(language="vi", call_voice_gender="female")
    ctx = SimpleNamespace(user_id="u", workspace_id="w", session_id="s")
    cut = {"shot": 23, "start": 0, "output_frames": 32, "speed": 1, "has_audio": True,
           "cues": [{"text": "Tôi tới ngay.", "from_sec": .25, "to_sec": 1.25}]}
    line = await post._voice(tmp_path, plan, cut, "Tôi tới ngay.", 804, ctx)
    assert [c.kwargs["voice"] for c in render.await_args_list] == ["longanlingxin_v3.1", "longanhuan_v3.1"]
    assert line.method == "tts" and line.heard == "Tôi tới ngay." and line.end <= 836 / 24
