#!/usr/bin/env python3
"""Compare an intended line against what a generated video actually says.

Ported from the retired server-side gate (video_workflow.normalize_spoken_text
/ compare_transcript). It reports; it does not decide. A `suspect` verdict is a
prompt to look, not a refusal — the person, not this script, judges whether a
take is usable.

    compare_transcript.py --intended "..." --heard "..."
    compare_transcript.py --json pairs.json      # [{"id":..,"intended":..,"heard":..}]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys

#: Punctuation and whitespace never reach the ear, so they never count against
#: a take. Kept byte-identical to the server rule these numbers were tuned on.
_PUNCT = re.compile(r"[\s。！？；：，、,.!?;:…·~—\-\"'“”‘’（）()《》<>【】\[\]]+")
#: Fillers a speech model sprinkles in without changing the meaning.
_FILLERS = "嗯呃唔诶哦噢喔呀啊吧呢啦嘛"

DEFAULT_THRESHOLD = 0.90


def normalize_spoken_text(value: str) -> str:
    compact = _PUNCT.sub("", value or "")
    return compact.translate({ord(char): None for char in _FILLERS})


def compare(intended: str, heard: str, threshold: float = DEFAULT_THRESHOLD) -> dict:
    expected = normalize_spoken_text(intended)
    actual = normalize_spoken_text(heard)
    matcher = difflib.SequenceMatcher(None, expected, actual)
    similarity = matcher.ratio()
    notes: list[str] = []
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation == "delete" and i2 - i1 >= 2:
            notes.append(f"疑似漏念「{expected[i1:i2]}」")
        elif operation == "insert" and j2 - j1 >= 2:
            notes.append(f"疑似多念「{actual[j1:j2]}」")
        elif operation == "replace" and max(i2 - i1, j2 - j1) >= 1:
            # A one-character swap can survive a high ratio and still change
            # the meaning (出片 → 出花), so any replacement is worth a look.
            notes.append(f"疑似念错「{expected[i1:i2]}→{actual[j1:j2]}」")
    return {
        "similarity": round(similarity, 3),
        "verdict": "ok" if similarity >= threshold and not notes else "suspect",
        "normalized_intended": expected,
        "normalized_heard": actual,
        "notes": notes,
        "threshold": threshold,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intended")
    parser.add_argument("--heard")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    if args.json_path:
        pairs = json.load(open(args.json_path, encoding="utf-8"))
        results = [
            {"id": item.get("id"), **compare(item["intended"], item["heard"], args.threshold)}
            for item in pairs
        ]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if args.intended is None or args.heard is None:
        parser.error("pass --intended and --heard, or --json")
    print(json.dumps(compare(args.intended, args.heard, args.threshold), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
