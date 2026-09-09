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

Run as a module to compile a file for the CLI spike:
``python -m video.ims_compiler timeline.json --out-url https://…/final.mp4``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from video import ims_catalog as cat
from video.timeline import Canvas, Cue, Motion, Shot, TextClip, TextStyle, Timeline

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

    video_clips = [
        _video_clip(index, shot, canvas, oss_region, problems, warnings)
        for index, shot in enumerate(timeline.shots)
    ]
    # Rule 3: a transition on the final clip has nothing to lead into.
    last = timeline.shots[-1]
    if last.transition_out is not None:
        warnings.append("last shot: transition_out ignored (nothing follows it)")
        video_clips[-1].pop("Effects", None)

    subtitle_tracks: list[dict[str, Any]] = []
    for index, text in enumerate(timeline.texts):
        clip = _text_clip(f"texts[{index}]", text, canvas, total, problems, warnings)
        if clip is not None:
            subtitle_tracks.append({"SubtitleTrackClips": [clip]})
    if timeline.captions:
        caption_clips = []
        for index, cue in enumerate(timeline.captions):
            clip = _caption_clip(f"captions[{index}]", cue, timeline, total, problems, warnings)
            if clip is not None:
                caption_clips.append(clip)
        _check_overlaps(timeline.captions, problems)
        if caption_clips:
            subtitle_tracks.append({"SubtitleTrackClips": caption_clips})

    out_url = _normalise_asset(output_url, oss_region, problems, what="output_url")
    if problems:
        raise TimelineCompileError(problems)

    ims_timeline: dict[str, Any] = {"VideoTracks": [{"VideoTrackClips": video_clips}]}
    if subtitle_tracks:
        ims_timeline["SubtitleTracks"] = subtitle_tracks
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
    if shot.in_sec:
        clip["In"] = _sec(shot.in_sec)
    if shot.duration_sec is not None:
        clip["Out"] = _sec(shot.in_sec + shot.duration_sec)
    if shot.transition_out is not None:
        kind = shot.transition_out.type
        _check_enum(kind, cat.KNOWN_TRANSITIONS, cat.VERIFIED_TRANSITIONS,
                    f"shots[{index}].transition_out.type", "transition", problems, warnings)
        clip["Effects"] = [{"Type": "Transition", "SubType": kind, "Duration": _sec(shot.transition_out.seconds)}]
    return clip


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
