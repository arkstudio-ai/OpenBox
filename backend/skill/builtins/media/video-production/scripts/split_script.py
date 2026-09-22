#!/usr/bin/env python3
"""Greedily split a spoken script at sentence and semantic boundaries.

The default is an advisory 40 characters per segment. The selected model's
live duration range remains authoritative; pass a different ``--max-chars``
when its measured capacity calls for one. Output includes an argv fragment
that can be appended directly to ``plan_shots.py``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

PRIMARY_BOUNDARY = re.compile(r"(?<=[。！？!?；;…])|\n+")
SECONDARY_BOUNDARY = re.compile(r"(?<=[，,、：:])")


def _split_units(text: str, max_chars: int) -> list[str]:
    units: list[str] = []
    for sentence in (part.strip() for part in PRIMARY_BOUNDARY.split(text)):
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            units.append(sentence)
            continue
        for part in (piece.strip() for piece in SECONDARY_BOUNDARY.split(sentence)):
            if not part:
                continue
            while len(part) > max_chars:
                units.append(part[:max_chars])
                part = part[max_chars:]
            if part:
                units.append(part)
    return units


def split_script(text: str, *, max_chars: int = 40, rate: float = 4.0) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("讲稿为空")
    if max_chars < 8:
        raise ValueError("max-chars 不能小于 8")
    if rate <= 0:
        raise ValueError("rate 必须大于 0")

    segments: list[str] = []
    current = ""
    for unit in _split_units(text, max_chars):
        if current and len(current) + len(unit) > max_chars:
            segments.append(current)
            current = unit
        else:
            current += unit
    if current:
        segments.append(current)

    rows = [
        {
            "idx": index,
            "scriptText": segment,
            "line": segment,
            "chars": len(segment),
            "estSeconds": round(len(segment) / rate, 1),
        }
        for index, segment in enumerate(segments, start=1)
    ]
    plan_args = [value for row in rows for value in ("--line", row["scriptText"])]
    return {
        "segments": rows,
        "totalChars": sum(row["chars"] for row in rows),
        "estTotalSeconds": round(sum(row["chars"] for row in rows) / rate, 1),
        "maxChars": max_chars,
        "plan_shots_args": plan_args,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", help="讲稿全文；省略时从 stdin 读取")
    parser.add_argument("--max-chars", type=int, default=40)
    parser.add_argument("--rate", type=float, default=4.0, help="仅用于展示估算")
    args = parser.parse_args()
    text = args.text if args.text is not None else sys.stdin.read()
    try:
        result = split_script(text, max_chars=args.max_chars, rate=args.rate)
    except ValueError as exc:
        result = {
            "error": {
                "code": "INVALID_INPUT",
                "message": str(exc),
                "retryable": False,
            }
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Advice, not a gate: invalid input is data for the caller, not a blocked
    # workflow. Keep zero in the error case as well.
    return 0


if __name__ == "__main__":
    sys.exit(main())
