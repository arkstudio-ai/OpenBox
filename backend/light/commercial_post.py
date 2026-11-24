"""Cut and mix the five-scene reference commercial on its measured 24 fps timeline."""

import asyncio
import hashlib
import json
import math
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from core.oss import get_oss
from light import commercial_plan, product_story_post as video, production_voice
from light.commercial_plan import DURATION, FPS, FRAME_COUNT, MATCH_FRAMES
from light.media_fonts import latin_font, mixed_font
from light.mode import LightError
from light.remake import RemakeStopped, _parallel, _reference_asset
from light.remake_lettering import _has_cjk
from light.video_language import MEDIA_TAGS
from light.wan_edit import _words

ASSETS = Path(__file__).with_name("remake_assets") / "commercial-reference" / "audio"
SCORE = {"track": "Funkorama", "bpm": 101, "music_in_frame": 138, "music_duck_frame": 786,
         "music_return_frame": 1118, "match_frames": list(MATCH_FRAMES)}
# The original soundtrack has a ~128 BPM pulse in both dance sections, with a new phase
# at the reprise. Times are transient peaks on the original film and the licensed recording.
REFERENCE_SCORE = {**SCORE, "bpm": 128, "master_bpm": 101, "first_beat_seconds": 5.705,
                   "reprise_beat_seconds": 48.0805, "master_first_beat_seconds": 9.49489,
                   "master_reprise_beat_seconds": 38.00974}
CREDITS = ("Funkorama by Kevin MacLeod (incompetech.com), "
           "https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1100474 . "
           "CC BY 4.0 https://creativecommons.org/licenses/by/4.0/ . Excerpted, arranged and mixed.")
SAMPLES_PER_FRAME = video.RATE // FPS
_FAILURES = {
    "timing": {"zh": "有镜头未达到参考片的帧数或剪点。", "en": "A shot does not match the reference cut.",
               "vi": "Một cảnh chưa khớp số khung hình hoặc điểm cắt của phim mẫu."},
    "voice": {"zh": "对白无法在原片时窗内完整清楚地播放。", "en": "A complete, intelligible line does not fit its cue.",
              "vi": "Chưa thể phát trọn câu thoại rõ ràng trong thời lượng của cảnh."},
    "export": {"zh": "成片未通过画面、音轨和字幕核对。", "en": "The picture, soundtrack or captions failed verification.",
               "vi": "Hình ảnh, âm thanh hoặc phụ đề của bản xuất chưa đạt kiểm tra."},
}


@dataclass
class Dialogue:
    shot: int
    text: str
    start: float
    end: float
    path: Path
    gain: float
    method: str
    heard: str
    voice_id: str = ""


def score_for(plan):
    return REFERENCE_SCORE if getattr(plan, "score_revision", 1) == 2 else SCORE


def _music_graph(index, score):
    entry = score["music_in_frame"] / FPS
    drop = score["music_duck_frame"] / FPS
    resume = score["music_return_frame"] / FPS
    change_tempo = ""
    entry_offset = 9.514
    reprise_offset = 38.026 - (1152 / FPS - resume)
    if "master_bpm" in score:
        speed = score["bpm"] / score["master_bpm"]
        change_tempo = f",atempo={speed:.9f}"
        entry_offset = score["master_first_beat_seconds"] / speed + entry - score["first_beat_seconds"]
        reprise_offset = score["master_reprise_beat_seconds"] / speed + resume - score["reprise_beat_seconds"]
    return [
        f"[{index}:a]aresample={video.RATE}{change_tempo},asplit=2[m1][m2]",
        f"[m1]atrim=start={entry_offset:.9f}:duration={drop-entry+.42:.9f},asetpts=PTS-STARTPTS,volume=0.59,"
        f"afade=t=in:d=0.035,afade=t=out:st={drop-entry:.9f}:d=0.42,"
        f"adelay={score['music_in_frame']*SAMPLES_PER_FRAME}S:all=1[groove]",
        f"[m2]atrim=start={reprise_offset:.9f}:duration={DURATION-resume:.9f},asetpts=PTS-STARTPTS,volume=0.64,"
        f"afade=t=in:d=0.13,afade=t=out:st={DURATION-resume-.23:.9f}:d=0.23,"
        f"adelay={score['music_return_frame']*SAMPLES_PER_FRAME}S:all=1[reprise]",
    ]


def _wav_level(path):
    with wave.open(str(path), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
        pcm = array("h")
        pcm.frombytes(wav.readframes(wav.getnframes()))
    peak = max(abs(min(pcm, default=0)), max(pcm, default=0)) / 32768
    return duration, peak


def _align_range(cut, interval):
    if not interval or len(interval) != 2:
        return None
    start = cut["start"] / FPS
    a, b = ((float(value) - start) / cut["speed"] for value in interval)
    limit = cut["output_frames"] / FPS
    if a < -.025 or b > limit + .025 or b - a < .12:
        return None
    return max(0, a), min(limit, b)


async def _fit_dub(folder, cut, first, observed, rendered, key):
    index = cut["shot"]
    raw = folder / f"voice-{index:02}-source.wav"
    raw.write_bytes(await get_oss().get_object(key))
    trimmed = folder / f"voice-{index:02}-trimmed.wav"
    trim = "silenceremove=start_periods=1:start_duration=0.012:start_threshold=-52dB:start_silence=0.025"
    await video.ff("-i", raw, "-vn", "-af", f"highpass=f=75,{trim},areverse,{trim},areverse",
                   "-ar", video.RATE, "-ac", 2, "-c:a", "pcm_s16le", trimmed)
    natural, peak = _wav_level(trimmed)
    if natural < .12 or peak < .025:
        return None
    # The call can bridge the cut into the office. The reprimand can run to the start of the handoff.
    cutoff = (858 if index == 23 else 1007) / FPS
    at = first + (observed[0] if observed else (7 if index == 23 else 36) / FPS)
    max_tempo = 1.42
    at = max(first, min(at, cutoff - natural / max_tempo - .04))
    if observed:
        preferred = natural / (observed[1] - observed[0])
    elif index == 23:
        preferred = max(1., natural / (first + cut["output_frames"] / FPS - at - .025))
    else:
        preferred = 1.
    tempo = max(.85, min(max_tempo, preferred), natural / (cutoff - at - .02))
    if tempo > max_tempo + .005:
        return None
    await video.ff("-i", trimmed, "-af", f"atempo={tempo:.9f}", "-c:a", "pcm_s16le", rendered)
    duration, peak = _wav_level(rendered)
    if duration < .12 or at + duration > cutoff + .001 or peak < .025:
        return None
    return at, at + duration, min(4., .67 / peak)


async def _hear_dub(ctx, rendered, language):
    from light.soda_story import _transcript

    # Register the exact retimed performance as a transient asset. The normal ASR job owns
    # credentials, settlement, and idempotent reuse if this edit resumes after an interruption.
    sound = await video.ff("-i", rendered, "-vn", "-map", "0:a", "-ac", 1, "-ar", 24000,
                           "-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "-")
    digest = hashlib.sha256(sound).hexdigest()[:24]
    asset = await _reference_asset(ctx, f"commercial-voice-{digest}", sound, mime="audio/mpeg")
    result = await _transcript(ctx, asset, language=language)
    heard = str(result.get("text") or " ".join(row["text"] for row in result.get("sentences", []) if row.get("text"))).strip()
    if heard:
        return heard
    return await _hear_short_dub(rendered)


async def _hear_short_dub(rendered):
    from assistant.policy import AssistantError
    from assistant.voice_messages import enabled, recognize

    if not enabled():
        return ""
    # File ASR sometimes reports no valid fragment for a short, trimmed utterance. Keep the output audio intact.
    wav = rendered.with_name(rendered.stem + "-asr.wav")
    await video.ff("-i", rendered, "-vn", "-af", "adelay=350:all=1,apad=pad_dur=0.5",
                   "-ac", 1, "-ar", 16000, "-c:a", "pcm_s16le", wav)
    try:
        return (await recognize(wav.read_bytes(), "audio/wav"))["text"]
    except AssistantError:
        return ""


def _voice_choices(plan, index):
    gender = getattr(plan, "call_voice_gender" if index == 23 else "rebuke_voice_gender", "unspecified")
    if gender == "unspecified":
        gender = "female" if index == 23 else "male"
    presets = dict(zip(production_voice.STYLE, production_voice.PRESETS[plan.language], strict=True))
    if gender == "female":
        roles = ["bright", "documentary", "energetic"] if plan.language == "vi" else ["bright", "energetic"]
        return [presets[role] for role in roles]
    if plan.language == "vi":
        return [presets["steady"], "qwen-audio-3.1-tts-flash-may_vi"]
    roles = ["documentary", "steady"] if index == 23 else ["steady", "documentary"]
    return [presets[role] for role in roles]


async def _synthesize(ctx, voice, text, language):
    for attempt in range(2):
        try:
            key = await production_voice.render_clip(user=ctx.user_id, workspace=ctx.workspace_id,
                                                    session=ctx.session_id, model=production_voice.MODEL, voice=voice,
                                                    text=text, locale=language)
            return key
        except LightError as exc:
            if exc.code not in ("PRODUCTION_VOICE_PROVIDER", "PRODUCTION_VOICE_UNAVAILABLE"):
                raise
            if exc.status < 500 or attempt:
                return None
            await asyncio.sleep(1)


async def _voice(folder, plan, cut, text, first_frame, ctx):
    index = cut["shot"]
    rendered = folder / f"line-{index:02}.wav"
    first = first_frame / FPS
    cue = next((c for c in cut.get("cues", []) if c.get("text") == text), None)
    interval = (cue["from_sec"], cue["to_sec"]) if cue else cut.get("speech_range")
    observed = _align_range(cut, interval)
    if cue and observed and cut["has_audio"] and not cut.get("dub") and .78 <= cut["speed"] <= 1.35:
        await video.ff("-i", folder / f"source-{index:02}.mp4", "-vn", "-af",
                       f"atrim=start={interval[0]:.8f}:end={interval[1]:.8f},asetpts=PTS-STARTPTS,"
                       f"atempo={cut['speed']:.9f},highpass=f=75", "-ar", video.RATE, "-ac", 2,
                       "-c:a", "pcm_s16le", rendered)
        duration, peak = _wav_level(rendered)
        if duration >= .12 and peak >= .025:
            at = first + observed[0]
            return Dialogue(index, text, at, at + duration, rendered, min(4., .67 / peak), "native_asr", text)

    voices = _voice_choices(plan, index)
    dub = cut.get("dub") or {}
    if dub.get("key"):
        voices.insert(0, "")
    for voice in voices:
        key = await _synthesize(ctx, voice, text, plan.language) if voice else dub["key"]
        if not key:
            continue
        fit = await _fit_dub(folder, cut, first, observed, rendered, key)
        if fit:
            heard = await _hear_dub(ctx, rendered, plan.language)
            if _words(heard) == _words(text):
                at, end, gain = fit
                return Dialogue(index, text, at, end, rendered, gain, "tts", heard, voice or dub.get("voice_id", ""))
    raise RemakeStopped(_FAILURES["voice"][plan.language])


def _lettering(folder, name, text, language):
    frame = Image.new("RGBA", (video.WIDTH, video.HEIGHT))
    draw = ImageDraw.Draw(frame)
    font = (mixed_font if language == "zh" or _has_cjk(text) else latin_font)(35)
    words = text.split()
    tokens, separator = (list(text), "") if language == "zh" or len(words) < 2 else (words, " ")
    lines = [""]
    for token in tokens:
        candidate = separator.join((lines[-1], token)).strip()
        if lines[-1] and draw.textlength(candidate, font=font) > 1090:
            lines.append(token)
        else:
            lines[-1] = candidate
    for line_index, line in enumerate(lines):
        y = video.HEIGHT - 57 - (len(lines) - line_index - 1) * 46
        draw.text((video.WIDTH // 2, y), line, fill="white", font=font, anchor="ms",
                  stroke_width=2, stroke_fill=(15, 16, 18, 240))
    path = folder / f"{name}.png"
    frame.save(path)
    return path


def _stamp(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


async def assemble(folder, plan, cuts, *, ctx):
    folder = Path(folder)
    shots = commercial_plan.shots(plan)
    if len(cuts) != len(shots) or sum(s["frames"] for s in shots) != FRAME_COUNT:
        raise RemakeStopped(_FAILURES["timing"][plan.language])
    frame = 0
    spoken = []
    for shot, cut in zip(shots, cuts, strict=True):
        if (cut["shot"] != shot["id"] or cut["output_frames"] != shot["frames"] or cut["start"] < 0
                or cut["frames"] < 1 or abs(cut["speed"] * shot["frames"] - cut["frames"]) > .001):
            raise RemakeStopped(_FAILURES["timing"][plan.language])
        if shot["spoken_line"]:
            spoken.append((cut, shot["spoken_line"], frame))
        frame += shot["frames"]
    if [row[0]["shot"] for row in spoken] != [23, 25]:
        raise RemakeStopped(_FAILURES["voice"][plan.language])

    async def clip(cut):
        index = cut["shot"]
        # Dialogue has its own verified bus. Untimed ASR or a discarded pronunciation must never bleed through.
        source_audio = cut["has_audio"] and index not in (23, 25) and .5 <= cut["speed"] <= 2.
        path = await video._clip(folder, index, {**cut, "dub": None, "has_audio": source_audio},
                                 {"adaptive": True, "spoken_line": ""})
        count = await video._run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=nb_frames", "-of", "default=nk=1:nw=1", path)
        if count.strip() != str(cut["output_frames"]).encode():
            raise RemakeStopped(_FAILURES["timing"][plan.language])
        return path

    async def line(row):
        return await _voice(folder, plan, *row, ctx)

    clips, dialogue = await asyncio.gather(_parallel(cuts, clip, concurrency=3),
                                          _parallel(spoken, line, concurrency=2), return_exceptions=True)
    for result in (clips, dialogue):
        if isinstance(result, BaseException):
            raise result

    count = len(clips)
    inputs = [item for path in clips for item in ("-threads", "1", "-i", path)]
    inputs.extend(("-i", ASSETS / "funkorama.mp3", "-i", ASSETS / "reference-foley.flac"))
    graph = ["".join(f"[{i}:v]" for i in range(count)) + f"concat=n={count}:v=1:a=0,settb=1/24,setpts=N[v0]"]
    for i, cut in enumerate(cuts):
        graph.append(f"[{i}:a]aresample={video.RATE},apad,atrim=end_sample={cut['output_frames'] * SAMPLES_PER_FRAME},"
                     f"asetpts=PTS-STARTPTS[a{i}]")
    graph.append("".join(f"[a{i}]" for i in range(count)) + f"concat=n={count}:v=0:a=1[location]")
    score = score_for(plan)
    graph.extend(_music_graph(count, score))
    graph.append(f"[{count+1}:a]aresample={video.RATE},aformat=channel_layouts=stereo[foley]")
    audio_layers = ["[location]", "[groove]", "[reprise]", "[foley]"]
    captions = []
    entries = []
    for i, voice in enumerate(dialogue):
        inputs.extend(("-i", voice.path))
        graph.append(f"[{count+2+i}:a]volume={voice.gain:.6f},afade=t=in:d=0.008,"
                     f"afade=t=out:st={voice.end-voice.start-.012:.8f}:d=0.012,"
                     f"adelay={round(voice.start*video.RATE)}S:all=1[voice{i}]")
        audio_layers.append(f"[voice{i}]")
        start = math.floor(voice.start * FPS)
        end = max(start + 15, math.ceil(voice.end * FPS))
        captions.append((_lettering(folder, f"subtitle-{i}", voice.text, plan.language), start, end))
        entries.append(f"{i+1}\n{_stamp(start / FPS)} --> {_stamp(end / FPS)}\n{voice.text}\n\n")
    if plan.end_copy:
        captions.append((_lettering(folder, "closing-copy", plan.end_copy, plan.language), FRAME_COUNT - 58, FRAME_COUNT))
    png_index = count + 2 + len(dialogue)
    for i, (png, start, end) in enumerate(captions):
        inputs.extend(("-i", png))
        graph.append(f"[v{i}][{png_index+i}:v]overlay=enable='gte(n,{start})*lt(n,{end})'[v{i+1}]")
    graph.append("".join(audio_layers) + f"amix=inputs={len(audio_layers)}:duration=first:normalize=0,"
                 f"alimiter=limit=0.89:level=0:latency=1,apad,atrim=end_sample={FRAME_COUNT*SAMPLES_PER_FRAME}[mix]")
    srt = folder / "dialogue.srt"
    srt.write_text("".join(entries), encoding="utf-8")
    inputs.extend(("-i", srt))
    tag = MEDIA_TAGS[plan.language]
    output = folder / "final.mp4"
    await video.ff(*inputs, "-filter_complex", ";".join(graph), "-map", f"[v{len(captions)}]", "-map", "[mix]",
                   "-map", f"{png_index + len(captions)}:s:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-threads:v", "4", "-preset", "fast", "-crf", "16", "-r", FPS, "-video_track_timescale", "24000",
                   "-c:a", "aac", "-ar", video.RATE, "-ac", "2", "-b:a", "192k", "-c:s", "mov_text",
                   "-metadata:s:a:0", f"language={tag}", "-metadata:s:s:0", f"language={tag}", "-disposition:s:0", "0",
                   "-metadata", f"title={plan.product} — {plan.title}", "-metadata", f"comment={CREDITS}",
                   "-movflags", "+faststart", output)
    probe = json.loads(await video._run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", output))
    picture, audio, sub = probe["streams"]
    if (picture.get("nb_frames") != str(FRAME_COUNT) or picture["avg_frame_rate"] != "24/1"
            or (picture["width"], picture["height"]) != (video.WIDTH, video.HEIGHT)
            or any(abs(float(item["duration"]) - DURATION) > .002 for item in (picture, audio, probe["format"]))
            or audio["codec_name"] != "aac" or audio["channels"] != 2 or sub["codec_name"] != "mov_text"
            or any(s.get("tags", {}).get("language") != tag for s in (audio, sub))):
        raise RemakeStopped(_FAILURES["export"][plan.language])
    await video.ff("-xerror", "-i", output, "-map", "0:v", "-map", "0:a", "-f", "null", "-")
    return output, {"duration_sec": DURATION, "frames": FRAME_COUNT, "fps": FPS, "has_audio": True,
                    "language": plan.language, "dialogue": len(dialogue), "subtitles_srt": srt.read_text(encoding="utf-8"),
                    "shot_frames": [c["output_frames"] for c in cuts], "score": dict(score), "attribution": CREDITS,
                    "dialogue_sources": [{"shot": v.shot, "method": v.method, "from_sec": v.start, "to_sec": v.end,
                                          "asr_text": v.heard, **({"voice_id": v.voice_id} if v.voice_id else {})}
                                         for v in dialogue]}
