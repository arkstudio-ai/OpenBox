#!/usr/bin/env python3
"""Push the video-production skill to a WUYING desktop, on its own.

`wuying_bootstrap.py` also installs the skill, but it reinstalls the runtime,
action server, tunnel and MCP supervisor along the way — far too much to run
just because a script changed. This is the same `Desktop.put` over the same
ECD command channel, and nothing else.

The command channel matters: on a hardened desktop the action server runs as
an unprivileged `sandbox` user under `no_new_privs`, and `/` is mounted
read-only, so writing to `/opt/openbox/skills` through it is impossible by
design. ECD `run-command` executes as root and can.

The skill root is replaced rather than merged. The 2026-09-01 rewrite dropped
four reference files describing the retired approval pipeline; leaving them
behind would keep feeding the agent instructions for a workflow that no
longer exists.

    python3 scripts/wuying_push_skill.py --env-file .env.wuying-prod
    python3 scripts/wuying_push_skill.py --desktop-id ecd-xxx --region cn-shanghai
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from wuying_bootstrap import VIDEO_PRODUCTION_SKILL_DIR, Desktop  # noqa: E402

REMOTE_ROOT = "/opt/openbox/skills/video-production"


def _skill_files() -> list[pathlib.Path]:
    return sorted(
        path for path in VIDEO_PRODUCTION_SKILL_DIR.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not path.name.startswith(".")
    )


def _profile_from_env_file(path: pathlib.Path) -> tuple[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    try:
        return values["WUYING_DESKTOP_ID"], values["WUYING_REGION_ID"]
    except KeyError as exc:
        raise SystemExit(f"{path} is missing {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=pathlib.Path,
                        help="read WUYING_DESKTOP_ID / WUYING_REGION_ID from this file")
    parser.add_argument("--desktop-id")
    parser.add_argument("--region", default="cn-shanghai")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.env_file:
        desktop_id, region = _profile_from_env_file(args.env_file)
    elif args.desktop_id:
        desktop_id, region = args.desktop_id, args.region
    else:
        raise SystemExit("give --env-file or --desktop-id")

    files = _skill_files()
    print(f"skill: {VIDEO_PRODUCTION_SKILL_DIR}")
    print(f"target: {desktop_id} ({region}) -> {REMOTE_ROOT}")
    for path in files:
        print(f"  {path.relative_to(VIDEO_PRODUCTION_SKILL_DIR)}")
    if args.dry_run:
        print(f"\ndry run: {len(files)} files not sent")
        return 0

    desktop = Desktop(desktop_id, region)
    print("\nbefore:")
    print(desktop.run(f"find {REMOTE_ROOT} -type f 2>/dev/null | sort || true", check=False))

    # Replace, do not merge — a leftover reference still reads as instruction.
    desktop.run(f"rm -rf {REMOTE_ROOT} && mkdir -p {REMOTE_ROOT}")
    for path in files:
        relative = path.relative_to(VIDEO_PRODUCTION_SKILL_DIR)
        mode = "755" if relative.parts[0] == "scripts" else "644"
        desktop.put(path, f"{REMOTE_ROOT}/{relative}", mode=mode)

    print("\nafter:")
    print(desktop.run(f"find {REMOTE_ROOT} -type f | sort"))
    print(desktop.run(
        f"cd {REMOTE_ROOT} && python3 scripts/plan_shots.py --line 'test' --json | head -3"
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
