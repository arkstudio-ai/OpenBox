"""Compile an OpenBox ``Timeline`` into an Aliyun IMS cloud-editing job.

The rules below are not style: each one closes a way the real service
rendered something other than what was asked (docs/VIDEO_RENDER_ENGINE_SELECTION.md
§4.2, measured 2026-09-09):

1. Every video clip carries explicit ``X/Y/Width/Height``. Without them IMS
   letterboxes and ignores ``AdaptMode`` — the "black bars" bug.
2. Text is placed with ``Alignment`` + ``X/Y`` in pixels, and the anchor moves
   with the alignment. Centered text is ``TopCenter`` with ``X: 0``; anything
   else is a guess about a corner. Wrapping needs ``TextWidth`` and
   ``AdaptMode: AutoWrap`` spelled out or the line runs off the canvas.
3. Transitions hang off the clip they lead out of and shorten the total.
4. Enum values (fonts, motion effects, 花字, transitions) are checked against
   ``ims_catalog`` before submission: unknown → error, known-but-unverified →
   warning. A wrong value renders as a silent no-op on IMS, after paying.
5. Text and caption timing is clamped to the timeline; a clip entirely past
   the end is an error, because it means the skill's arithmetic is off.
6. The job's ``ClientToken`` is derived from the compiled request, so the
   same cut submitted twice is one job.
7. Anything beyond one video track is placed by explicit ``TimelineIn`` /
   ``TimelineOut``: the blurred backdrop under a ``contain`` shot, the
   picture-in-picture overlays, a per-shot colour filter, an AI voice-over.
   Speed changes a shot's length on the timeline (``shot.timeline_sec``), and
   every span is computed from that, never from the source length.

Run as a module to compile a file for the CLI spike:
``python -m video.ims_compiler timeline.json --out-url https://…/final.mp4``.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from video import ims_catalog as cat
from video.timeline import AudioClip, Canvas, Cue, Motion, Shot, TextClip, TextStyle, Timeline

_ADAPT = {"cover": "Cover", "contain": "Contain", "fill": "Fill"}
_ALIGN = {"center": "TopCenter", "left": "TopLeft", "right": "TopRight"}
#: Empirical line box for CJK text at a given FontSize.
_LINE_HEIGHT = 1.35
_OSS_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}\.oss-[a-z0-9-]+(-internal)?\.aliyuncs\.com$")


class TimelineCompileError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class CompiledJob:
    timeline: dict[str, Any]
    output_media_config: dict[str, Any]
    client_token: str
    duration_sec: float | None
    warnings: list[str] = field(default_factory=list)

    def as_cli_args(self) -> dict[str, str]:
        """Values for ``aliyun ice submit-media-producing-job``."""
        return {
            "--timeline": json.dumps(self.timeline, ensure_ascii=False, separators=(",", ":")),
            "--output-media-target": "oss-object",
            "--output-media-config": json.dumps(self.output_media_config, separators=(",", ":")),
            "--client-token": self.client_token,
            "--source": "OpenAPI",
        }


def compile_ims(
    timeline: Timeline,
    *,
    output_url: str,
    oss_region: str | None = None,
    bitrate_kbps: int = 2500,
) -> CompiledJob:
    problems: list[str] = []
    warnings: list[str] = []
    canvas = timeline.canvas
    total = timeline.duration_sec

    spans = timeline.shot_spans()
    video_clips = [
        _video_clip(index, shot, canvas, oss_region, problems, warnings)
        for index, shot in enumerate(timeline.shots)
    ]
    # Rule 3: a transition on the final clip has nothing to lead into.
    last = timeline.shots[-1]
    if last.transition_out is not None:
        warnings.append("last shot: transition_out ignored (nothing follows it)")
        video_clips[-1]["Effects"] = [e for e in video_clips[-1].get("Effects", []) if e.get("Type") != "Transition"]
        if not video_clips[-1]["Effects"]:
            video_clips[-1].pop("Effects")

    # Rule 7: the blurred backdrop sits on its own track, below the main one.
    backdrop_clips: list[dict[str, Any]] = []
    for index, shot in enumerate(timeline.shots):
        if shot.backdrop != "blur":
            continue
        span = spans[index]
        if span is None:
            problems.append(f"shots[{index}]: a blurred backdrop needs duration_sec on this and every earlier shot")
            continue
        if shot.fit != "contain":
            warnings.append(f"shots[{index}]: backdrop=blur is meant for fit=contain; with {shot.fit} it stays hidden")
        _check_enum("h_blur", cat.KNOWN_VFX, cat.VERIFIED_VFX, f"shots[{index}].backdrop", "VFX", problems, warnings)
        clip: dict[str, Any] = {
            "MediaURL": video_clips[index]["MediaURL"],
            "X": 0, "Y": 0, "Width": canvas.width, "Height": canvas.height, "AdaptMode": "Cover",
            "TimelineIn": _sec(span[0]), "TimelineOut": _sec(span[1]),
            "Effects": [{"Type": "VFX", "SubType": "h_blur"}, {"Type": "Volume", "Gain": 0}],
        }
        if shot.in_sec:
            clip["In"] = _sec(shot.in_sec)
        if shot.duration_sec is not None:
            clip["Out"] = _sec(shot.in_sec + shot.duration_sec)
        if shot.speed != 1.0:
            clip["Speed"] = round(shot.speed, 3)
        backdrop_clips.append(clip)

    overlay_tracks: list[dict[str, Any]] = []
    for index, overlay in enumerate(timeline.overlays):
        clip = _overlay_clip(index, overlay, canvas, total, oss_region, problems, warnings)
        if clip is not None:
            overlay_tracks.append({"VideoTrackClips": [clip]})

    effect_items: list[dict[str, Any]] = []
    if timeline.filter:
        _check_enum(timeline.filter, cat.KNOWN_FILTERS, cat.VERIFIED_FILTERS, "filter", "filter", problems, warnings)
        if total is None:
            problems.append("filter: a whole-cut filter needs every shot's duration_sec")
        else:
            effect_items.append({"Type": "Filter", "SubType": timeline.filter, "TimelineIn": 0, "TimelineOut": _sec(total)})
    for index, shot in enumerate(timeline.shots):
        if not shot.filter:
            continue
        _check_enum(shot.filter, cat.KNOWN_FILTERS, cat.VERIFIED_FILTERS, f"shots[{index}].filter", "filter", problems, warnings)
        span = spans[index]
        if span is None:
            problems.append(f"shots[{index}].filter needs duration_sec on this and every earlier shot")
            continue
        effect_items.append({"Type": "Filter", "SubType": shot.filter, "TimelineIn": _sec(span[0]), "TimelineOut": _sec(span[1])})

    subtitle_tracks: list[dict[str, Any]] = []
    for index, text in enumerate(timeline.texts):
        clip = _text_clip(f"texts[{index}]", text, canvas, total, problems, warnings)
        if clip is not None:
            subtitle_tracks.append({"SubtitleTrackClips": [clip]})
    if timeline.captions:
        caption_clips = []
        for index, cue in enumerate(timeline.captions):
            pieces = _fit_line(cue, timeline)
            if len(pieces) > 1:
                warnings.append(f"captions[{index}]: split into {len(pieces)} cues to fit one line")
            for piece in pieces:
                clip = _caption_clip(f"captions[{index}]", piece, timeline, total, problems, warnings)
                if clip is not None:
                    caption_clips.append(clip)
        _check_overlaps(timeline.captions, problems)
        if caption_clips:
            subtitle_tracks.append({"SubtitleTrackClips": caption_clips})

    audio_tracks = [
        {"AudioTrackClips": clips}
        for index, item in enumerate(timeline.audio)
        if (clips := _audio_clips(index, item, timeline, total, oss_region, problems, warnings))
    ]

    out_url = _normalise_asset(output_url, oss_region, problems, what="output_url")
    if problems:
        raise TimelineCompileError(problems)

    video_tracks: list[dict[str, Any]] = []
    if backdrop_clips:
        video_tracks.append({"VideoTrackClips": backdrop_clips})
    video_tracks.append({"VideoTrackClips": video_clips, "MainTrack": True})
    video_tracks.extend(overlay_tracks)
    ims_timeline: dict[str, Any] = {"VideoTracks": video_tracks}
    if audio_tracks:
        ims_timeline["AudioTracks"] = audio_tracks
    if subtitle_tracks:
        ims_timeline["SubtitleTracks"] = subtitle_tracks
    if effect_items:
        ims_timeline["EffectTracks"] = [{"EffectTrackItems": effect_items}]
    output = {"MediaURL": out_url, "Width": canvas.width, "Height": canvas.height, "Bitrate": bitrate_kbps}
    return CompiledJob(
        timeline=ims_timeline,
        output_media_config=output,
        client_token=_client_token(ims_timeline, output),
        duration_sec=total,
        warnings=warnings,
    )


# ── clips ────────────────────────────────────────────────────────────────────

def _video_clip(index: int, shot: Shot, canvas: Canvas, region: str | None,
                problems: list[str], warnings: list[str]) -> dict[str, Any]:
    clip: dict[str, Any] = {
        "MediaURL": _normalise_asset(shot.asset, region, problems, what=f"shots[{index}].asset"),
        # Rule 1.
        "X": 0, "Y": 0, "Width": canvas.width, "Height": canvas.height,
        "AdaptMode": _ADAPT[shot.fit],
    }
    if shot.volume != 1.0:
        # The clip's own sound. 0 mutes it (a silent B-roll under a voice-over).
        clip.setdefault("Effects", []).append({"Type": "Volume", "Gain": round(shot.volume, 3)})
    if shot.in_sec:
        clip["In"] = _sec(shot.in_sec)
    if shot.duration_sec is not None:
        clip["Out"] = _sec(shot.in_sec + shot.duration_sec)
    if shot.speed != 1.0:
        clip["Speed"] = round(shot.speed, 3)
    if shot.move is not None:
        if shot.move.preset is not None and shot.move.preset not in cat.VERIFIED_MOVES:
            warnings.append(f"shots[{index}].move: preset {shot.move.preset!r} is documented but not yet verified in a real job")
        start, end = shot.move.windows()
        clip.setdefault("Effects", []).append({
            "Type": "KenBurns", "Start": _window(start), "End": _window(end)})
    if shot.transition_out is not None:
        kind = shot.transition_out.type
        _check_enum(kind, cat.KNOWN_TRANSITIONS, cat.VERIFIED_TRANSITIONS,
                    f"shots[{index}].transition_out.type", "transition", problems, warnings)
        clip.setdefault("Effects", []).append(
            {"Type": "Transition", "SubType": kind, "Duration": _sec(shot.transition_out.seconds)})
    return clip


def _overlay_clip(index: int, overlay, canvas: Canvas, total: float | None, region: str | None,
                  problems: list[str], warnings: list[str]) -> dict[str, Any] | None:
    where = f"overlays[{index}]"
    span = _clamp_span(where, overlay.from_sec, overlay.to_sec, total, problems, warnings)
    if span is None:
        return None
    clip: dict[str, Any] = {
        "MediaURL": _normalise_asset(overlay.asset, region, problems, what=f"{where}.asset"),
        # Fractions of the canvas: IMS reads 0 ≤ v < 1 as a share of the output size.
        "X": _frac(overlay.x), "Y": _frac(overlay.y), "Width": _frac(overlay.width), "Height": _frac(overlay.height),
        "AdaptMode": _ADAPT[overlay.fit],
        "TimelineIn": _sec(span[0]), "TimelineOut": _sec(span[1]),
        "Out": _sec(overlay.in_sec + (span[1] - span[0])),
    }
    if overlay.in_sec:
        clip["In"] = _sec(overlay.in_sec)
    if overlay.opacity != 1.0:
        clip["Opacity"] = round(overlay.opacity, 3)
    if overlay.volume != 1.0:
        clip["Effects"] = [{"Type": "Volume", "Gain": round(overlay.volume, 3)}]
    return clip


def _window(values) -> str:
    return ",".join(f"{float(v):.4g}" for v in values)


def _frac(value: float) -> float:
    # 1.0 would be read as a pixel count by IMS; stay strictly inside the fraction range.
    return min(round(float(value), 4), 0.9999)


#: Ducking: a pause shorter than this keeps the bed down; it dips just before a line and rises after.
_DUCK_JOIN_SEC = 0.8
_DUCK_LEAD_SEC = 0.2
_DUCK_TAIL_SEC = 0.3
#: A repeated track fades out and back in around each seam instead of jumping;
#: it does not start again for less than this, but ends (fading) where it ran out.
_LOOP_FADE_SEC = 0.6
_LOOP_MIN_TAIL_SEC = 4.0
_EPS = 0.05


def _audio_clips(index: int, item: AudioClip, timeline: Timeline, total: float | None, region: str | None,
                 problems: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    """One sound bed as the clips of its track: split where it repeats and where it ducks."""
    where = f"audio[{index}]"
    end = item.to_sec if item.to_sec is not None else total
    if total is not None and item.from_sec >= total:
        problems.append(f"{where}: starts at {item.from_sec}s, after the cut ends ({total}s)")
        return []
    if total is not None and end is not None and end > total:
        warnings.append(f"{where}: to_sec {end}s clamped to the cut's end ({total}s)")
        end = total
    if item.voiceover is not None:
        voice = item.voiceover
        if voice.voice not in cat.KNOWN_VOICES:
            problems.append(f"{where}.voiceover.voice: unknown voice {voice.voice!r}; IMS would refuse or fall back silently")
        elif voice.voice not in cat.VERIFIED_VOICES:
            warnings.append(f"{where}.voiceover.voice: {voice.voice!r} is listed by IMS but not yet verified in a real job")
        clip = {
            "Type": "AI_TTS", "Content": voice.text, "Voice": voice.voice,
            "SpeechRate": voice.speech_rate, "PitchRate": voice.pitch_rate,
            "TimelineIn": _sec(item.from_sec), "TimelineOut": _sec(end),
        }
        effects = _audio_effects(item.volume, item.fade_in_sec, item.fade_out_sec)
        return [{**clip, "Effects": effects} if effects else clip]
    if item.library_id:
        media: dict[str, Any] = {"MediaId": item.library_id}
    else:
        media = {"MediaURL": _normalise_asset(item.asset or "", region, problems, what=f"{where}.asset")}
    if end is None:  # open-ended: nothing to repeat or duck against
        clip = {**media, "TimelineIn": _sec(item.from_sec)}
        if item.in_sec:
            clip["In"] = _sec(item.in_sec)
        effects = _audio_effects(item.volume, item.fade_in_sec, item.fade_out_sec)
        return [{**clip, "Effects": effects} if effects else clip]

    period = (item.source_sec - item.in_sec) if item.source_sec else None
    seams: list[float] = []
    if period:
        at = item.from_sec + period
        while at < end - _EPS:
            seams.append(at)
            at += period
        if seams and end - seams[-1] < _LOOP_MIN_TAIL_SEC:
            end = seams.pop()  # a few seconds of the song again sound like a mistake: end it there
    ducks = _duck_spans(timeline.captions, item.from_sec, end) if item.duck_under_captions else []
    if item.duck_under_voice:
        spoken = [Cue(from_sec=v.from_sec, to_sec=v.to_sec, text="voice") for v in timeline.audio
                  if v.voiceover is not None and v.to_sec is not None]
        ducks = _merge_spans(ducks + _duck_spans(spoken, item.from_sec, end))
    points: list[float] = []
    for point in sorted({item.from_sec, end, *seams, *(p for span in ducks for p in span)}):
        if points and point - points[-1] < _EPS:
            if point == end:
                points[-1] = end
            continue
        points.append(point)

    clips = []
    for a, b in zip(points, points[1:]):
        offset = a - item.from_sec
        if period:
            offset = round(offset % period, 3)
            if offset > period - _EPS:
                offset = 0.0
        src_in = item.in_sec + offset
        ducked = any(da - _EPS <= a and b <= db + _EPS for da, db in ducks)
        fade_in = item.fade_in_sec if a == item.from_sec else (_LOOP_FADE_SEC if _near(a, seams) else 0.0)
        fade_out = item.fade_out_sec if b == end else (_LOOP_FADE_SEC if _near(b, seams) else 0.0)
        half = (b - a) / 2
        clip = {**media, "TimelineIn": _sec(a), "TimelineOut": _sec(b), "Out": _sec(src_in + b - a)}
        if src_in:
            clip["In"] = _sec(src_in)
        effects = _audio_effects(item.duck_volume if ducked else item.volume, min(fade_in, half), min(fade_out, half))
        clips.append({**clip, "Effects": effects} if effects else clip)
    return clips


def _audio_effects(volume: float, fade_in: float, fade_out: float) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    if volume != 1.0:
        effects.append({"Type": "Volume", "Gain": round(volume, 3)})
    if fade_in:
        effects.append({"Type": "AFade", "SubType": "In", "Duration": _sec(fade_in)})
    if fade_out:
        effects.append({"Type": "AFade", "SubType": "Out", "Duration": _sec(fade_out)})
    return effects


def _duck_spans(cues: list[Cue], start: float, end: float) -> list[tuple[float, float]]:
    """Where the bed goes down: each caption, a little wider, nearby ones joined."""
    spans: list[tuple[float, float]] = []
    for cue in sorted(cues, key=lambda c: c.from_sec):
        a, b = max(start, cue.from_sec - _DUCK_LEAD_SEC), min(end, cue.to_sec + _DUCK_TAIL_SEC)
        if b <= a:
            continue
        if spans and a - spans[-1][1] < _DUCK_JOIN_SEC:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    return spans


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if merged and a - merged[-1][1] < _DUCK_JOIN_SEC:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _near(value: float, points: list[float]) -> bool:
    return any(abs(value - p) < _EPS for p in points)


def _text_clip(where: str, text: TextClip, canvas: Canvas, total: float | None,
               problems: list[str], warnings: list[str]) -> dict[str, Any] | None:
    span = _clamp_span(where, text.from_sec, text.to_sec, total, problems, warnings)
    if span is None:
        return None
    # Rule 2: anchor follows Alignment.
    if text.align == "center":
        x = 0
    elif text.align == "left":
        x = _px(text.x, canvas.width)
    else:
        x = _px(text.x, canvas.width)
        warnings.append(f"{where}: align=right uses the TopRight anchor, which is documented but not yet verified")
    clip = {
        "Type": "Text",
        "Content": text.text,
        "TimelineIn": _sec(span[0]),
        "TimelineOut": _sec(span[1]),
        "Alignment": _ALIGN[text.align],
        "X": x,
        "Y": _px(text.y, canvas.height),
        "TextWidth": round(text.max_width, 4),
        "AdaptMode": "AutoWrap",
    }
    clip.update(_style(where, text.style, problems, warnings))
    clip.update(_motion(where, text.motion, problems, warnings))
    return clip


#: Width of a character as a share of the font size: CJK fills it, Latin and digits take about half.
_NARROW = 0.55
_PHRASE_END = re.compile(r"(?<=[，,。！!？?；;、：:])")


def _fit_line(cue: Cue, timeline: Timeline) -> list[Cue]:
    """A caption that fits one line, or several that do, each timed by its share of the text.

    IMS's AutoWrap does not wrap Chinese: a long cue runs off the frame
    (light-test, 2026-11-03, a 17-character line on a 720-px canvas). So a cue
    wider than the line splits at its punctuation, and a phrase still too wide
    splits evenly.
    """
    cs = timeline.caption_style
    room = timeline.canvas.width * cs.max_width / cs.style.size
    if _width(cue.text) <= room:
        return [cue]
    lines: list[str] = []
    for phrase in (p for p in _PHRASE_END.split(cue.text) if p.strip()):
        if lines and _width(lines[-1] + phrase) <= room:
            lines[-1] += phrase
        elif _width(phrase) <= room:
            lines.append(phrase)
        else:
            parts = math.ceil(_width(phrase) / room)
            size = math.ceil(len(phrase) / parts)
            lines.extend(phrase[i:i + size] for i in range(0, len(phrase), size))
    lines = [line.rstrip("，,、") or line for line in lines]
    total = sum(_width(line) for line in lines)
    pieces, at = [], cue.from_sec
    for index, line in enumerate(lines):
        end = cue.to_sec if index == len(lines) - 1 else round(at + (cue.to_sec - cue.from_sec) * _width(line) / total, 2)
        pieces.append(Cue(from_sec=at, to_sec=end, text=line))
        at = end
    return pieces


def _width(text: str) -> float:
    return sum(_NARROW if ord(ch) < 0x2E80 else 1.0 for ch in text)


def _caption_clip(where: str, cue: Cue, timeline: Timeline, total: float | None,
                  problems: list[str], warnings: list[str]) -> dict[str, Any] | None:
    span = _clamp_span(where, cue.from_sec, cue.to_sec, total, problems, warnings)
    if span is None:
        return None
    canvas, cs = timeline.canvas, timeline.caption_style
    # Y is the top edge; leave one line box above the bottom margin so a
    # single-line caption sits where a burnt ASS subtitle would.
    top = canvas.height * (1 - cs.bottom_ratio) - cs.style.size * _LINE_HEIGHT
    clip = {
        "Type": "Text",
        "Content": cue.text,
        "TimelineIn": _sec(span[0]),
        "TimelineOut": _sec(span[1]),
        "Alignment": "TopCenter",
        "X": 0,
        "Y": max(0, int(round(top))),
        "TextWidth": round(cs.max_width, 4),
        "AdaptMode": "AutoWrap",
    }
    clip.update(_style(where, cs.style, problems, warnings))
    clip.update(_motion(where, cs.motion, problems, warnings))
    return clip


def _style(where: str, style: TextStyle, problems: list[str], warnings: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "FontSize": style.size,
        "FontColor": style.color.upper(),
        "Outline": style.outline,
        "OutlineColour": style.outline_color.upper(),
    }
    if style.font_url:
        out["FontURL"] = style.font_url
    else:
        _check_enum(style.font, cat.KNOWN_FONTS, cat.VERIFIED_FONTS, f"{where}.style.font", "font", problems, warnings)
        out["Font"] = style.font
    if style.color_style:
        _check_enum(style.color_style, cat.KNOWN_COLOR_STYLES, cat.VERIFIED_COLOR_STYLES,
                    f"{where}.style.color_style", "花字 style", problems, warnings)
        out["EffectColorStyle"] = style.color_style
    return out


def _motion(where: str, motion: Motion, problems: list[str], warnings: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if motion.in_effect:
        _check_enum(motion.in_effect, cat.KNOWN_MOTION_IN, cat.VERIFIED_MOTION_IN,
                    f"{where}.motion.in_effect", "motion-in effect", problems, warnings)
        out["AaiMotionInEffect"] = motion.in_effect
        out["AaiMotionIn"] = _sec(motion.in_sec)
    if motion.out_effect:
        _check_enum(motion.out_effect, cat.KNOWN_MOTION_OUT, cat.VERIFIED_MOTION_OUT,
                    f"{where}.motion.out_effect", "motion-out effect", problems, warnings)
        out["AaiMotionOutEffect"] = motion.out_effect
        out["AaiMotionOut"] = _sec(motion.out_sec)
    return out


# ── helpers ──────────────────────────────────────────────────────────────────

def _check_enum(value: str, known: frozenset[str], verified: frozenset[str],
                where: str, kind: str, problems: list[str], warnings: list[str]) -> None:
    if value not in known:
        problems.append(f"{where}: unknown {kind} {value!r}; IMS would render it as a no-op")
    elif value not in verified:
        warnings.append(f"{where}: {kind} {value!r} is documented but not yet verified in a real job")


def _clamp_span(where: str, start: float, end: float, total: float | None,
                problems: list[str], warnings: list[str]) -> tuple[float, float] | None:
    if total is None:
        return (start, end)
    if start >= total:
        problems.append(f"{where}: starts at {start}s but the timeline ends at {total}s")
        return None
    if end > total:
        warnings.append(f"{where}: clamped end {end}s to the timeline end {total}s")
        end = total
    return (start, end)


def _check_overlaps(cues: list[Cue], problems: list[str]) -> None:
    ordered = sorted(cues, key=lambda c: c.from_sec)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.from_sec < prev.to_sec:
            problems.append(
                f"captions overlap: {prev.text!r} ends {prev.to_sec}s, {nxt.text!r} starts {nxt.from_sec}s"
            )


def _normalise_asset(ref: str, region: str | None, problems: list[str], *, what: str) -> str:
    """Accept ``oss://bucket/key`` or an OSS https URL; return the https form IMS reads."""
    if ref.startswith("oss://"):
        bucket, _, key = ref[6:].partition("/")
        if not bucket or not key:
            problems.append(f"{what}: {ref!r} needs both bucket and key")
            return ref
        if not region:
            problems.append(f"{what}: {ref!r} is an oss:// reference but no oss_region was given")
            return ref
        return f"https://{bucket}.oss-{region}.aliyuncs.com/{key}"
    parts = urlsplit(ref)
    if parts.scheme != "https" or not _OSS_HOST.match(parts.netloc or "") or not parts.path.strip("/"):
        problems.append(f"{what}: {ref!r} must be an OSS https URL or oss://bucket/key (assets never leave the account)")
    return ref


def _client_token(timeline: dict[str, Any], output: dict[str, Any]) -> str:
    # Rule 6. ClientToken: ASCII, ≤64 chars.
    payload = json.dumps({"t": timeline, "o": output}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "obx-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:56]


def _sec(value: float) -> float:
    return round(float(value), 4)


def _px(fraction: float, extent: int) -> int:
    return int(round(fraction * extent))


# ── CLI for the spike ────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="compile an OpenBox timeline JSON to an IMS Timeline")
    parser.add_argument("timeline", help="path to OpenBox timeline JSON")
    parser.add_argument("--out-url", required=True, help="OSS https URL or oss://bucket/key for the MP4")
    parser.add_argument("--oss-region", default=None)
    parser.add_argument("--bitrate", type=int, default=2500)
    parser.add_argument("--cli-args", action="store_true", help="print aliyun CLI arguments instead of the Timeline")
    args = parser.parse_args(argv)

    with open(args.timeline, encoding="utf-8") as fh:
        timeline = Timeline.model_validate(json.load(fh))
    try:
        job = compile_ims(timeline, output_url=args.out_url, oss_region=args.oss_region, bitrate_kbps=args.bitrate)
    except TimelineCompileError as exc:
        for problem in exc.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2
    for warning in job.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.cli_args:
        print(json.dumps(job.as_cli_args(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(job.timeline, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
