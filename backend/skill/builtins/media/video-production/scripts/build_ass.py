#!/usr/bin/env python3
"""Build an ASS subtitle file for a spoken video.

Ported from the retired WUYING media worker so the styling stays where it can
be changed without a release: fork the skill, edit this file. The numbers below
are the ones that were measured to work — 48px at 1080, 60px side margins,
explicit greedy wrapping — not defaults anyone should re-derive by eye.

    build_ass.py --out captions.ass --width 720 --height 1280 \
        --channel "频道名" --segment 5.2 "第一句" --segment 4.8 "第二句"

Captions must be what the video ACTUALLY says (the transcript), never the line
that was requested — otherwise the words on screen drift from the audio.
"""
from __future__ import annotations

import argparse
import re
import sys

#: Caption baseline, as a fraction of frame height. The 28% preset from the
#: source renderer is deliberately absent: that position exists to cover old
#: burned-in captions, and using it here just floats the text mid-frame.
SUBTITLE_BOTTOM_RATIO = 0.095


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def ass_escape(value: str) -> str:
    # Preserve explicit \N line-break tokens rather than doubling the slash:
    # libass reads \\N as literal text and collapses the caption into one long
    # line, which is the exact overflow the wrapping below exists to prevent.
    text = str(value).replace("\r", "").replace("\n", r"\N")
    text = text.replace(r"\N", "\x00N").replace(r"\n", "\x00n")
    text = text.replace("\\", "")
    text = text.replace("{", r"\{").replace("}", r"\}")
    return text.replace("\x00N", r"\N").replace("\x00n", r"\n")


def caption_language(value: str) -> str:
    text = str(value or "")
    chinese = sum(
        1 for char in text if "㐀" <= char <= "䶿" or "一" <= char <= "鿿"
    )
    alpha = sum(1 for char in text if char.isalpha())
    if alpha == 0:
        return "zh"
    return "zh" if chinese / alpha > 0.3 else "en"


def wrap_subtitle_text(value: str, *, width: int, font_size: int, side_margin: int) -> str:
    """Wrap before libass, because unspaced CJK has nothing for it to break on."""
    text = re.sub(r"[ \t\f\v]+", " ", str(value or "").replace("\r", "")).strip()
    if not text:
        return ""
    language = caption_language(text)
    usable_width = max(1, int(width) - 2 * int(side_margin))
    if language == "zh":
        max_chars = max(8, int(usable_width / max(1, int(font_size))))
    else:
        max_chars = max(16, int(usable_width / max(1.0, float(font_size) * 0.55)))

    wrapped: list[str] = []
    for paragraph in text.split("\n"):
        remaining = paragraph.strip()
        if not remaining:
            continue
        if language == "en":
            current = ""
            for word in remaining.split():
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > max_chars:
                    wrapped.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                wrapped.append(current)
            continue

        punctuation = " ,，。、；：！？·"
        while len(remaining) > max_chars:
            chunk = remaining[:max_chars]
            break_pos = max_chars
            if remaining[max_chars] in punctuation:
                break_pos = max_chars + 1
            else:
                for index in range(len(chunk) - 1, 0, -1):
                    if chunk[index] in punctuation:
                        break_pos = index + 1
                        break
            # A Chinese line often carries an English product name; do not
            # split one just to satisfy the CJK character grid.
            while (
                0 < break_pos < len(remaining)
                and remaining[break_pos - 1].isascii()
                and remaining[break_pos - 1].isalpha()
                and remaining[break_pos].isascii()
                and remaining[break_pos].isalpha()
            ):
                break_pos -= 1
            if break_pos <= 0:
                break_pos = max_chars
            wrapped.append(remaining[:break_pos].rstrip())
            remaining = remaining[break_pos:].lstrip()
        if remaining:
            wrapped.append(remaining)
    return r"\N".join(wrapped)


def build_document(
    *, durations: list[float], captions: list[str], channel_name: str, width: int, height: int
) -> str:
    subtitle_size = max(16, round(min(width, height) * (48 / 1080)))
    channel_size = max(20, round(width * 0.027))
    subtitle_margin = max(24, round(height * SUBTITLE_BOTTOM_RATIO))
    channel_margin_v = max(18, round(height * 0.04))
    side_margin = max(24, round(min(width, height) * (60 / 1080)))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,Noto Sans CJK SC,{subtitle_size},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,3,1,2,{side_margin},{side_margin},{subtitle_margin},1
Style: Channel,Noto Sans CJK SC,{channel_size},&H20FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,2,1,1,{side_margin},{side_margin},{channel_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    current = 0.0
    for duration, caption in zip(durations, captions):
        end = current + float(duration)
        if str(caption).strip():
            wrapped = wrap_subtitle_text(
                str(caption).strip(),
                width=width,
                font_size=subtitle_size,
                side_margin=side_margin,
            )
            events.append(
                "Dialogue: 0,"
                f"{ass_timestamp(current)},{ass_timestamp(end)},"
                f"Subtitle,,0,0,0,,{ass_escape(wrapped)}"
            )
        current = end
    if channel_name.strip() and current > 0:
        events.append(
            "Dialogue: 1,"
            f"0:00:00.00,{ass_timestamp(current)},Channel,,0,0,0,,"
            f"● {ass_escape(channel_name.strip())}"
        )
    return header + "\n".join(events) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--channel", default="")
    parser.add_argument(
        "--segment",
        nargs=2,
        action="append",
        metavar=("SECONDS", "CAPTION"),
        default=[],
        help="Repeat once per clip, in order. Use the transcript, not the script.",
    )
    args = parser.parse_args()
    if not args.segment:
        parser.error("pass at least one --segment SECONDS CAPTION")

    durations = [float(seconds) for seconds, _ in args.segment]
    captions = [caption for _, caption in args.segment]
    document = build_document(
        durations=durations,
        captions=captions,
        channel_name=args.channel,
        width=args.width,
        height=args.height,
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(document)
    print(f"wrote {args.out} ({len(durations)} caption slots)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
