#!/usr/bin/env python3
"""A loose notebook for one video, kept in the workspace.

Deliberately not a state machine. It records what has been decided and made so
a later turn can pick the work up; it never blocks a step, orders the steps, or
decides that something is "missing". Ignore it, or keep notes in your own
shape — nothing downstream reads this but you.

    state.py init  --slug spring-tips --title "三招出片"
    state.py set   --slug spring-tips --key script --value "$(cat script.txt)"
    state.py shot  --slug spring-tips --index 1 --job video_abc --path /workspace/media/1.mp4
    state.py show  --slug spring-tips
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("VIDEO_STATE_ROOT", "/workspace/videos"))


def path_for(slug: str) -> Path:
    return ROOT / slug / "state.json"


def load(slug: str) -> dict:
    target = path_for(slug)
    if not target.exists():
        return {"slug": slug, "shots": []}
    return json.loads(target.read_text(encoding="utf-8"))


def save(slug: str, data: dict) -> Path:
    target = path_for(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "set", "shot", "show"):
        p = sub.add_parser(name)
        p.add_argument("--slug", required=True)
        if name == "init":
            p.add_argument("--title", default="")
        if name == "set":
            p.add_argument("--key", required=True)
            p.add_argument("--value", required=True)
        if name == "shot":
            p.add_argument("--index", type=int, required=True)
            p.add_argument("--job", default="")
            p.add_argument("--path", default="")
            p.add_argument("--asset", default="")
            p.add_argument("--transcript", default="")
            p.add_argument("--seconds", default="")
    args = parser.parse_args()

    data = load(args.slug)
    if args.command == "init":
        data.setdefault("shots", [])
        data["title"] = args.title
    elif args.command == "set":
        data[args.key] = args.value
    elif args.command == "shot":
        shots = {int(item["index"]): item for item in data.get("shots", [])}
        entry = shots.get(args.index, {"index": args.index})
        for field in ("job", "path", "asset", "transcript", "seconds"):
            value = getattr(args, field)
            if value:
                entry[field] = value
        shots[args.index] = entry
        data["shots"] = [shots[key] for key in sorted(shots)]
    elif args.command == "show":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    target = save(args.slug, data)
    print(f"saved {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
