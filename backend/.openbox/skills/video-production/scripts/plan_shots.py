#!/usr/bin/env python3
"""Give each shot the length its own line needs.

A shot's duration is not a free choice: the model fills whatever time it is
given. Ask for 5s with a 13-character line and it invents words to fill the
gap; ask for 6s with a 38-character line and the delivery is rushed. Both were
measured on 2026-09-01 — shot 1 came back with audible padding at 2.6 chars/s,
shots 3 and 4 raced at 5.2-5.3.

So the duration follows the text, not a target total divided by the shot count.
Natural Mandarin narration runs about 4 characters per second (broadcast pace
is 280-300 per minute), and a shot also needs a breath at each end, which is
why the industry advice is never to pack the line in tight.

The total is an OUTPUT of that arithmetic, not an input. A "30-second" request
whose script needs 39 seconds gets 39 — or a shorter script. Silently forcing
the arithmetic to hit 30 is what produced the padding in the first place.

    python3 plan_shots.py --target 30 \
        --line "早上赶时间，也别随便对付早餐。" \
        --line "第一个办法，提前准备：晚上把鸡蛋煮好……"
"""
from __future__ import annotations

import argparse
import json
import re
import sys

#: Characters per second of comfortable Mandarin narration. Broadcast pace is
#: 280-300 per minute; short-form 口播 sits a little under that.
NARRATION_RATE = 4.0

#: Lead-in and tail-out. A shot cut flush to the first and last syllable has
#: no room for the breath a person actually takes, and concatenation then
#: sounds clipped.
BREATH_SECONDS = 1.2

#: Below this a shot is mostly silence, and the model starts inventing words
#: to fill it. Merge such a line into a neighbour instead.
MIN_SHOT_SECONDS = 3

#: Only spoken characters count. Punctuation is not pronounced, and Latin
#: words are read as words rather than as their letter count.
_LATIN_RUN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_SPOKEN = re.compile(r"[一-鿿㐀-䶿0-9]")


def spoken_length(line: str) -> float:
    """Characters-equivalent of one line, as a narrator would say it."""
    latin_words = _LATIN_RUN.findall(line)
    without_latin = _LATIN_RUN.sub("", line)
    # A short English word takes roughly as long as two Chinese characters.
    return len(_SPOKEN.findall(without_latin)) + 2.0 * len(latin_words)


def shot_duration(line: str, *, rate: float, max_seconds: int) -> tuple[int, str | None]:
    """Seconds this line needs, and a note when the text has to change."""
    length = spoken_length(line)
    if length <= 0:
        return MIN_SHOT_SECONDS, "no spoken characters — is this line empty?"

    needed = length / rate + BREATH_SECONDS
    seconds = max(MIN_SHOT_SECONDS, int(needed + 0.999))

    note = None
    if seconds < needed:
        note = "rounded down below what the line needs"
    if seconds > max_seconds:
        overflow = length - (max_seconds - BREATH_SECONDS) * rate
        note = (
            f"needs {seconds}s but the model caps at {max_seconds}s — "
            f"split this line or cut about {int(overflow + 0.999)} characters"
        )
        seconds = max_seconds
    elif length / (seconds - BREATH_SECONDS) > rate * 1.25:
        note = "delivery would be rushed; consider a shorter line"
    return seconds, note


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--line", action="append", default=[], required=True,
                        help="one shot's spoken line; repeat per shot")
    parser.add_argument("--target", type=float, default=0,
                        help="the total the person asked for, in seconds (advisory)")
    parser.add_argument("--rate", type=float, default=NARRATION_RATE,
                        help=f"spoken characters per second (default {NARRATION_RATE})")
    parser.add_argument("--max-shot-seconds", type=int, default=30,
                        help="the chosen model's ceiling, from video_generate(action='models')")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    shots = []
    for index, line in enumerate(args.line, start=1):
        seconds, note = shot_duration(
            line, rate=args.rate, max_seconds=args.max_shot_seconds
        )
        shots.append({
            "shot": index,
            "seconds": seconds,
            "spoken_chars": round(spoken_length(line), 1),
            "rate": round(spoken_length(line) / max(seconds - BREATH_SECONDS, 0.1), 2),
            "note": note,
            "line": line,
        })

    total = sum(shot["seconds"] for shot in shots)
    report = {"shots": shots, "total_seconds": total}

    if args.target:
        drift = total - args.target
        report["target_seconds"] = args.target
        report["drift_seconds"] = round(drift, 1)
        if abs(drift) > max(3.0, args.target * 0.15):
            chars = abs(drift) * args.rate
            report["advice"] = (
                f"the script runs {abs(drift):.0f}s "
                f"{'over' if drift > 0 else 'under'} the {args.target:.0f}s asked for. "
                + (f"Cut about {chars:.0f} characters, or tell the person the video "
                   f"will be {total}s — do not squeeze the timing instead."
                   if drift > 0 else
                   f"Add about {chars:.0f} characters, or accept a {total}s video — "
                   f"do not stretch shots to fill the gap, the model will pad them.")
            )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"{'shot':<6}{'secs':<7}{'chars':<8}{'chars/s':<10}line")
    for shot in shots:
        print(f"{shot['shot']:<6}{shot['seconds']:<7}{shot['spoken_chars']:<8}"
              f"{shot['rate']:<10}{shot['line'][:34]}")
        if shot["note"]:
            print(f"{'':<6}⚠ {shot['note']}")
    print(f"\ntotal={total}s", end="")
    if args.target:
        print(f"  target={args.target:.0f}s  drift={report['drift_seconds']:+.0f}s")
        if "advice" in report:
            print(f"\n{report['advice']}")
    else:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
