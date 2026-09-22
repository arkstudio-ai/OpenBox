#!/usr/bin/env python3
"""A loose, advisory notebook for one video project.

This is deliberately not a state machine. It records the script, selected
model, shot plan, paid jobs, transcripts, and explicit acceptances so a later
turn can resume safely. ``check`` only prints observations and always exits
zero; it never authorises or blocks another step.

Examples::

    state.py init --slug spring-tips --title "三招出片"
    state.py set --slug spring-tips --key script --value "$(cat script.txt)"
    state.py confirm --slug spring-tips --kind script --note "用户确认成稿卡"
    state.py shot --slug spring-tips --index 1 --script "第一段" \
      --prompt "完整 prompt" --planned-seconds 8
    state.py confirm --slug spring-tips --kind shots --note "用户确认拆段与费用"
    state.py shot --slug spring-tips --index 1 --job video_abc --path shot1.mp4
    state.py check --slug spring-tips --final final.mp4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(os.environ.get("VIDEO_STATE_ROOT", "/workspace/videos"))

# Output fields must not invalidate a confirmed creative plan. Everything in
# this tuple affects the user's script/model/shot/cost decision and therefore
# participates in the confirmation hash.
SHOT_PLAN_FIELDS = (
    "script",
    "prompt",
    "planned_seconds",
    "assets",
    "model",
    "resolution",
)


def path_for(slug: str) -> Path:
    return ROOT / slug / "state.json"


def load(slug: str) -> dict:
    target = path_for(slug)
    if not target.exists():
        return {"slug": slug, "shots": [], "confirmations": {}}
    data = json.loads(target.read_text(encoding="utf-8"))
    data.setdefault("slug", slug)
    data.setdefault("shots", [])
    data.setdefault("confirmations", {})
    return data


def save(slug: str, data: dict) -> Path:
    target = path_for(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def script_hash(data: dict) -> str:
    return _hash(data.get("script", ""))


def shot_plan(shot: dict) -> dict:
    return {field: shot.get(field, "") for field in SHOT_PLAN_FIELDS}


def shot_hashes(data: dict) -> dict[str, str]:
    return {
        str(int(shot["index"])): _hash(shot_plan(shot))
        for shot in sorted(data.get("shots", []), key=lambda item: int(item["index"]))
    }


def shots_snapshot(data: dict) -> dict:
    return {
        "model": data.get("model", ""),
        "resolution": data.get("resolution", ""),
        "shots": [
            {"index": int(shot["index"]), **shot_plan(shot)}
            for shot in sorted(data.get("shots", []), key=lambda item: int(item["index"]))
        ],
    }


def shots_hash(data: dict) -> str:
    return _hash(shots_snapshot(data))


def confirm(data: dict, kind: str, note: str = "") -> dict:
    """Record an advisory snapshot of what the user confirmed."""
    recorded = {
        "hash": script_hash(data) if kind == "script" else shots_hash(data),
        "note": note,
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if kind == "shots":
        # A shots confirmation is the combined U2 shot-plan + spend
        # confirmation. Keep per-shot hashes so check can name only the
        # segments affected by a later edit.
        recorded.update(
            {
                "script_hash": script_hash(data),
                "shot_hashes": shot_hashes(data),
                "model": data.get("model", ""),
                "resolution": data.get("resolution", ""),
                "includes_spend": True,
            }
        )
    data.setdefault("confirmations", {})[kind] = recorded
    return recorded


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_media(path: str) -> dict:
    """Return duration/audio facts without raising on missing or bad media."""
    target = Path(path)
    if not target.exists():
        return {"ok": False, "has_audio": False, "duration": None, "error": "文件不存在"}
    try:
        done = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json", str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {"ok": False, "has_audio": False, "duration": None, "error": str(exc)}
    if done.returncode != 0:
        return {
            "ok": False,
            "has_audio": False,
            "duration": None,
            "error": done.stderr.strip() or "ffprobe 读取失败",
        }
    try:
        payload = json.loads(done.stdout)
        duration = _number(payload.get("format", {}).get("duration"))
        has_audio = any(
            stream.get("codec_type") == "audio" for stream in payload.get("streams", [])
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "has_audio": False, "duration": None, "error": str(exc)}
    return {"ok": True, "has_audio": has_audio, "duration": duration, "error": ""}


def _changed_shots(data: dict, confirmation: dict) -> list[int]:
    previous = confirmation.get("shot_hashes", {})
    current = shot_hashes(data)
    changed = {
        int(index)
        for index in set(previous) | set(current)
        if previous.get(index) != current.get(index)
    }
    global_plan_changed = (
        confirmation.get("model", "") != data.get("model", "")
        or confirmation.get("resolution", "") != data.get("resolution", "")
    )
    if global_plan_changed or (
        confirmation.get("script_hash") != script_hash(data) and not changed
    ):
        changed.update(int(index) for index in set(previous) | set(current))
    return sorted(changed)


def check_state(
    data: dict,
    *,
    final_path: str = "",
    probe: Callable[[str], dict] = probe_media,
) -> dict:
    """Build an advisory issue report. The caller decides what to do."""
    issues: list[dict] = []

    def add(code: str, message: str, shot: int | None = None) -> None:
        item = {"code": code, "message": message}
        if shot is not None:
            item["shot"] = shot
        issues.append(item)

    confirmations = data.get("confirmations", {})
    script_confirmation = confirmations.get("script")
    shots_confirmation = confirmations.get("shots")
    if not script_confirmation:
        add("script_unconfirmed", "成稿尚无确认记录")
    elif script_confirmation.get("hash") != script_hash(data):
        affected = _changed_shots(data, shots_confirmation or {})
        suffix = f"；受影响段：{','.join(map(str, affected))}" if affected else ""
        add("script_drift", f"已确认讲稿内容已改动{suffix}，请重新拆段并确认")

    if not shots_confirmation:
        add("shots_unconfirmed", "拆段、prompt 与费用尚无合并确认记录")
    elif shots_confirmation.get("hash") != shots_hash(data):
        affected = _changed_shots(data, shots_confirmation)
        suffix = ",".join(map(str, affected)) or "未能定位"
        add(
            "shots_drift",
            f"已确认拆段内容已改动；受影响段：{suffix}，原费用确认同时作废",
        )

    actual_durations: list[float] = []
    for shot in sorted(data.get("shots", []), key=lambda item: int(item["index"])):
        index = int(shot["index"])
        if not shot.get("job"):
            add("shot_job_missing", f"第 {index} 段尚未记录生成任务", index)

        media = probe(str(shot.get("path", ""))) if shot.get("path") else {
            "ok": False,
            "has_audio": False,
            "duration": None,
            "error": "尚未记录成片路径",
        }
        if not media.get("has_audio"):
            add("shot_audio_missing", f"第 {index} 段没有可确认的音轨：{media.get('error', '')}", index)

        planned = _number(shot.get("planned_seconds"))
        actual = _number(shot.get("seconds"))
        if actual is None:
            actual = _number(media.get("duration"))
        if actual is not None:
            actual_durations.append(actual)
        if planned is None:
            add("shot_plan_duration_missing", f"第 {index} 段未记录计划秒数", index)
        elif actual is None:
            add("shot_actual_duration_missing", f"第 {index} 段无法读取实际秒数", index)
        elif abs(actual - planned) > max(2.0, planned * 0.25):
            add(
                "shot_duration_drift",
                f"第 {index} 段计划 {planned:g}s、实际 {actual:g}s，偏差超过 max(2s, 25%)",
                index,
            )

    if final_path:
        final_media = probe(final_path)
        if not final_media.get("has_audio"):
            add("final_audio_missing", f"成片没有可确认的音轨：{final_media.get('error', '')}")
        final_duration = _number(final_media.get("duration"))
        expected = sum(actual_durations)
        if final_duration is None:
            add("final_duration_missing", "无法读取成片实际时长")
        elif actual_durations and abs(final_duration - expected) > max(2.0, expected * 0.05):
            add(
                "final_duration_mismatch",
                f"成片 {final_duration:g}s 与各段实测合计 {expected:g}s 不符",
            )

    return {"status": "attention" if issues else "clear", "issues": issues}


def _shot_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--job", default="")
    parser.add_argument("--path", default="")
    parser.add_argument("--asset", default="")
    parser.add_argument("--transcript", default="")
    parser.add_argument("--seconds", default="", help="actual measured seconds")
    parser.add_argument("--planned-seconds", default="")
    parser.add_argument("--script", default="")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--assets", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--resolution", default="")
    parser.add_argument(
        "--accept", default="",
        help="user's reason for accepting an STT/duration mismatch",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "set", "shot", "confirm", "check", "show"):
        command = sub.add_parser(name)
        command.add_argument("--slug", required=True)
        if name == "init":
            command.add_argument("--title", default="")
        elif name == "set":
            command.add_argument("--key", required=True)
            command.add_argument("--value", required=True)
        elif name == "shot":
            _shot_parser(command)
        elif name == "confirm":
            command.add_argument("--kind", choices=("script", "shots"), required=True)
            command.add_argument("--note", default="")
        elif name == "check":
            command.add_argument("--final", default="")
    args = parser.parse_args()

    data = load(args.slug)
    if args.command == "init":
        data["title"] = args.title
    elif args.command == "set":
        data[args.key] = args.value
    elif args.command == "shot":
        shots = {int(item["index"]): item for item in data.get("shots", [])}
        entry = shots.get(args.index, {"index": args.index})
        for field in (
            "job", "path", "asset", "transcript", "seconds", "planned_seconds",
            "script", "prompt", "assets", "model", "resolution",
        ):
            value = getattr(args, field)
            if value != "":
                entry[field] = value
        if args.accept:
            entry["accept"] = {
                "reason": args.accept,
                "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        shots[args.index] = entry
        data["shots"] = [shots[key] for key in sorted(shots)]
    elif args.command == "confirm":
        recorded = confirm(data, args.kind, args.note)
        print(json.dumps({"confirmed": args.kind, **recorded}, ensure_ascii=False, indent=2))
        save(args.slug, data)
        return 0
    elif args.command == "check":
        print(json.dumps(check_state(data, final_path=args.final), ensure_ascii=False, indent=2))
        return 0
    elif args.command == "show":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    target = save(args.slug, data)
    print(f"saved {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
