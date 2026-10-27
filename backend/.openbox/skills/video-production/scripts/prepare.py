#!/usr/bin/env python3
"""Validate and save one complete video draft, without approval or paid work.

    python3 /opt/openbox/skills/video-production/scripts/prepare.py --plan /workspace/plan.json

The input contains slug, title, script, model, resolution, visual_anchor and
shots. Each shot has index, script, prompt, planned_seconds, input_assets and
optional speech (false only for a silent shot). Optional ratio, material_plan
describe the already chosen plan. Live model limits and asset ownership remain
the video tools' authority; this helper does not quote prices.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys

from lint_prompt import lint_prompt
import state


ROLES = {"reference_image", "first_frame", "last_frame", "reference_video", "reference_audio"}
PLAN_FIELDS = {"slug", "title", "script", "model", "resolution", "visual_anchor", "shots",
               "ratio", "material_plan"}
SHOT_FIELDS = {"index", "script", "prompt", "planned_seconds", "input_assets", "speech"}


def _text(value, field, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{field} must be {'a string' if empty else 'a nonempty string'}")
    return value


def prepare(plan: dict) -> dict:
    if not isinstance(plan, dict) or set(plan) - PLAN_FIELDS:
        raise ValueError("Plan must contain only the documented draft fields; approvals and jobs are not draft inputs")
    slug = _text(plan.get("slug"), "slug")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
        raise ValueError("slug must be 1–80 lowercase letters, digits or hyphens")
    for name in ("title", "script", "model", "resolution", "visual_anchor"):
        _text(plan.get(name), name, empty=name in {"script", "visual_anchor"})
    if "ratio" in plan:
        _text(plan["ratio"], "ratio")
    if "material_plan" in plan and not isinstance(plan["material_plan"], dict):
        raise ValueError("material_plan must be an object")
    shots = plan.get("shots")
    if not isinstance(shots, list) or not 1 <= len(shots) <= 32:
        raise ValueError("shots must contain 1–32 shots in timeline order")

    reports, notebook_shots, validations = [], [], []
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict) or set(shot) - SHOT_FIELDS:
            raise ValueError(f"shot {index}: only draft fields are allowed; jobs and acceptances cannot be imported")
        if type(shot.get("index")) is not int or shot["index"] != index:
            raise ValueError("shot indices must be consecutive, starting at 1")
        script = _text(shot.get("script"), f"shot {index}.script", empty=True)
        prompt = _text(shot.get("prompt"), f"shot {index}.prompt")
        if len(prompt) > 32000:
            raise ValueError(f"shot {index}: prompt exceeds 32000 characters")
        seconds = shot.get("planned_seconds")
        if type(seconds) is not int or not 1 <= seconds <= 300:
            raise ValueError(f"shot {index}: planned_seconds must be an explicit integer from 1 to 300; verify live model limits")
        speech = shot.get("speech", True)
        if type(speech) is not bool or (speech and not script.strip()):
            raise ValueError(f"shot {index}: speech must be boolean and a speaking shot needs its exact script")
        refs = shot.get("input_assets", [])
        if not isinstance(refs, list) or len(refs) > 8:
            raise ValueError(f"shot {index}: input_assets must contain at most 8 references")
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"asset_id", "role"}:
                raise ValueError(f"shot {index}: each input needs asset_id and its confirmed role")
            _text(ref["asset_id"], f"shot {index}.asset_id")
            if ref["role"] not in ROLES:
                raise ValueError(f"shot {index}: unsupported reference role")
        lint = lint_prompt(script_text=script, prompt=prompt, visual_anchor=plan["visual_anchor"],
                           image_count=sum(r["role"] in {"reference_image", "first_frame", "last_frame"} for r in refs),
                           video_count=sum(r["role"] == "reference_video" for r in refs), speech=speech)
        reports.append({"shot": index, **lint})
        notebook_shots.append({"index": index, "script": script, "prompt": prompt,
                               "planned_seconds": str(seconds), "model": plan["model"],
                               "resolution": plan["resolution"],
                               "assets": json.dumps(refs, ensure_ascii=False, sort_keys=True)})
        validations.append({"action": "validate", "shot": index, "prompt": prompt,
                          "model": plan["model"], "resolution": plan["resolution"],
                          "duration": seconds, "input_assets": refs,
                          **({"ratio": plan["ratio"]} if "ratio" in plan else {})})
    if not all(report["ok"] for report in reports):
        return {"ok": False, "saved": False, "shots": reports,
                "next_action": "Correct only the reported fields using accepted_examples, then rerun this same plan. Do not inspect script source or unrelated projects."}

    previous = state.load(slug)
    draft = copy.deepcopy(previous)
    for field in PLAN_FIELDS - {"shots", "slug"}:
        if field in plan:
            draft[field] = plan[field]
    if "ratio" not in plan:
        draft.pop("ratio", None)
    if "material_plan" in previous and "material_plan" not in plan:
        raise ValueError("Existing material_plan must be included explicitly; it cannot be silently inherited")
    old_shots = {s["index"]: s for s in previous.get("shots", [])}
    draft["shots"] = [{**old_shots.get(s["index"], {}), **s} for s in notebook_shots]
    changed = state.shots_hash(previous) != state.shots_hash(draft) or state.script_hash(previous) != state.script_hash(draft)
    if changed and any(any(field in s for field in state.TAKE_OUTPUT_FIELDS) for s in old_shots.values()):
        raise ValueError("This draft already has generated takes. Resume its recorded jobs; use a new slug for a new production, not to retry an uncertain paid submission")
    target = state.path_for(slug)
    if draft != previous or not target.exists():
        target = state.save(slug, draft)
    return {"ok": True, "saved": True, "state_path": str(target), "shots": reports,
            "validation_requests": validations,
            "approval_recorded": False,
            "next_action": "Validate these exact validation_requests, then review the complete production plan only if it still needs approval. Reuse the user's existing decisions. Do not estimate credits or add a fee confirmation. This saved draft is not approval."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="Absolute path to the complete draft JSON")
    args = parser.parse_args()
    try:
        path = Path(args.plan)
        if not path.is_absolute():
            raise ValueError("--plan requires an absolute path")
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Plan exceeds 1 MiB")
        result = prepare(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        result = {"ok": False, "saved": False, "error": str(exc),
                  "next_action": "Correct the specified plan or missing dependency; do not search other directories for substitutes."}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
