"""Agent-authored continuous-action remake and the contract for its single final cut."""

from __future__ import annotations

import json
import math
from urllib.parse import unquote, urlsplit
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from light import shortcut_plan, silk_road
from light.reference_plan import ShotPlan

TEMPLATE = "wan_story"
SODA_TEMPLATE = "soda_story"
TEMPLATES = (TEMPLATE, SODA_TEMPLATE, silk_road.TEMPLATE, shortcut_plan.TEMPLATE)
VERSION = "wan-story-v2"
VERSIONS = ("wan-story-v1", VERSION)
MODEL = "wan3.0-video"
RESOLUTION = "1080p"
DURATIONS = (12, 13, 4)

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=42)]
Direction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=420)]


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: Short
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=18)]


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Direction
    lines: list[Line] = Field(min_length=1, max_length=2)


class Plan(BaseModel):
    """The agent writes the story, blocking and words from the current form."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: Short
    product: Short
    appearance: Direction
    setting_and_cast: Direction
    opening_frame: Direction
    reveal_frame: Direction
    opening: Scene
    response: Scene
    headline: Annotated[str, StringConstraints(min_length=2, max_length=10)]


class StoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["prepare", "render"]
    plan: shortcut_plan.ShortcutPlan | ShotPlan | silk_road.Plan | None = Field(
        default=None,
        description="The director's storyboard. Required on prepare. Omit on render when the App has already approved a plan.",
    )


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["prepare"] = "prepare"
    plan: ShotPlan


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["render"] = "render"


def display_script(plan: Plan) -> str:
    if isinstance(plan, ShotPlan):
        from light.reference_plan import display_script as display_shots
        return display_shots(plan)
    lines = [f"《{plan.title}》", plan.setting_and_cast]
    for scene in (plan.opening, plan.response):
        lines.extend(["", scene.action, *(f"{line.speaker}：「{line.text}」" for line in scene.lines)])
    lines.extend(["", f"片尾 · {plan.product}「{plan.headline}」"])
    return "\n".join(lines)


def planning_instruction(form: dict, action: str) -> str:
    from light.remake_case import reference_copy
    from light.reference_plan import DIRECTING

    data = {
        "店名": form["shop"],
        "素材": form["asset_ids"],
        "任务": form["brief"],
        "用户参考文案": reference_copy(form),
    }
    directing = DIRECTING
    if form.get("template") == SODA_TEMPLATE:
        from light.soda_story import DIRECTING as directing
    elif form.get("template") == shortcut_plan.TEMPLATE:
        directing = shortcut_plan.DIRECTING
    elif form.get("template") == silk_road.TEMPLATE:
        directing = silk_road.DIRECTING
        data["确认菜名"] = form.get("product_name", "")
    return directing + (
        "直接调用 light_wan_story，action=prepare，把完整分镜放在 plan。只保存文案供用户编辑，成功后结束。"
        if action == "prepare" else
        "用户已点击生成。直接调用 light_wan_story，action=render，省略plan以使用已确认分镜。"
        "只有完整合成并核对所有分镜的成片可以交付。"
    ) + "\n表单数据：" + json.dumps(data, ensure_ascii=False)


def parse_plan(data, *, template=None):
    if template == shortcut_plan.TEMPLATE:
        return shortcut_plan.ShortcutPlan.model_validate(data)
    if isinstance(data, dict) and data.get("format") == silk_road.FORMAT:
        return silk_road.Plan.model_validate(data)
    return (ShotPlan if isinstance(data, dict) and "shots" in data else Plan).model_validate(data)


def shot_count(run: dict) -> int:
    saved = run.get("remake_adaptation") or {}
    if saved.get("format") == silk_road.FORMAT:
        return len(silk_road.case()["shots"])
    return len(saved["shots"]) if saved.get("format") == "shot-list-v2" else 3




def is_complete_cut(run: dict, job, *, require_delivery_review: bool = True) -> bool:
    """Verify the persisted edit against the authored, ordered segment assets, even during recovery."""
    if (run.get("remake_adaptation") or {}).get("format") == silk_road.FORMAT:
        return silk_road.is_complete_cut(run, job)
    original = run.get("wan_cuts") or []
    from light.reference_quality import FinalReport, delivery_cuts

    cuts = delivery_cuts(run)
    count = shot_count(run)
    key = run.get("wan_final_key")
    actual_key = getattr(job, "idempotency_key", "") or ""
    if (
        not key
        or (actual_key != key and not actual_key.startswith(key + ":retry:"))
        or len(original) != count
        or [c["shot"] for c in original] != list(range(1, count + 1))
        or not cuts
        or job.kind != "compose"
        or job.status != "completed"
        or not job.output_asset_id
    ):
        return False
    if require_delivery_review and run.get("wan_quality") is not None:
        final = run["wan_quality"].get("final") or {}
        try:
            if (final.get("output") != job.output_asset_id or not final.get("dialogue_matches")
                    or not FinalReport.model_validate(final["report"]).accepted):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    req = job.request_data or {}
    timeline = req.get("timeline") or {}
    shots = timeline.get("shots") or []
    if len(shots) != len(cuts) or len({cut["source"] for cut in cuts}) != len(cuts):
        return False
    for shot, cut in zip(shots, cuts):
        path = unquote(urlsplit(str(shot.get("asset") or "")).path).split("/")
        if (
            (shot.get("asset") != cut["source"] and cut["source"] not in path)
            or not math.isclose(float(shot.get("in_sec") or 0), cut["in_sec"], abs_tol=0.002)
            or not math.isclose(float(shot.get("duration_sec") or 0), cut.get("source_duration_sec", cut["duration_sec"]), abs_tol=0.002)
            or not math.isclose(float(shot.get("speed") or 1), cut.get("speed", 1), abs_tol=0.0001)
        ):
            return False
    at = 0.0
    overlays = (req.get("timeline") or {}).get("overlays") or []
    for cut in cuts:
        if cut.get("hold_asset"):
            matched = any((layer.get("asset") == cut["hold_asset"] or cut["hold_asset"] in
                           unquote(urlsplit(str(layer.get("asset") or "")).path).split("/"))
                          and math.isclose(layer.get("from_sec", -1), at, abs_tol=0.002)
                          and math.isclose(layer.get("to_sec", -1), at + cut["duration_sec"], abs_tol=0.002)
                          for layer in overlays)
            if not matched:
                return False
        at += cut["duration_sec"]
    expected = sum(cut["duration_sec"] for cut in cuts)
    return math.isclose(float(req.get("duration_sec") or 0), expected, abs_tol=0.09)
