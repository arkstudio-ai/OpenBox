"""Review the reference commercial's key performances at normal viewing scale."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from light import commercial_frame_audit as pixels
from light import commercial_plan as recipe
from light import reference_quality
from light.remake import _parallel, _reference_asset

VERSION = "normal-playback-v1"
VISIBILITY = "product-focus-v2"
# The existing director separately reviews the three match cuts and the street callback.
ANCHORS = (2, 22, 26, 27)
GUIDE = ("按正常速度观看广告的标准检查这几条已剪定的实拍。每张时间条有同一镜的五个等距画面（1至5）。"
         "逐镜简短描述实际动作。若普通观看明显看出双重商品、主要人物/道具换错、长时间无承托、错误用法或突兀跳切，"
         "指出一处具体镜号、格号与观察供短区间复核。一帧轻微形变、局部模糊、遮挡、反光、正常交接轻扶无需提出。"
         "product_focus表示镜头是否强调商品，未强调仍允许按剧情自然入画。街舞保持原有穿戴或携带；固定设备留在原安装处。"
         "布景或充气道具自带的夸张装饰与真人身上或手中的第二件实物须作区分。"
         "不要因人物还没做下一镜动作、表情渐变或不可见细节提出问题。")
DETAIL = ("在参照图之后，先看疑点附近最多半秒的真实连续画面（绝对F号从小到大），最后一张是其中一个正常放映尺寸的实拍帧。"
          "按正常播放判断：只有普通观众容易察觉、"
          "街角出现同一件正常穿戴或携带的商品合乎剧情，product_focus只标识镜头重点。"
          "连续多帧存在的明显穿帮，或整幅突换又恢复的明显闪断才退回。所给疑点是待查线索。轻微单帧变形、正常透视、"
          "正常背景装饰可放行。真人首次拿取唯一一件穿戴商品时，若其穿戴部位已经有清楚可辨的另一件同类物，"
          "连续数帧两件同时可见就是明显错物，即使两件颜色不同。"
          "合理遮挡、自然换手或证据不清时接受。若持续有错，给出真实首末F号及一条重选或补拍本镜能执行的修正。")


class Observation(BaseModel):
    shot: int
    visible: str = Field(min_length=2, max_length=200)


class Concern(BaseModel):
    shot: int
    cell: int = Field(ge=1, le=5)
    reason: str = Field(min_length=3, max_length=330)


class Screening(BaseModel):
    observations: list[Observation]
    concern: Concern | None = None

    @model_validator(mode="after")
    def actual_shots(self):
        if tuple(row.shot for row in self.observations) != ANCHORS or (self.concern and self.concern.shot not in ANCHORS):
            raise ValueError(f"Describe each submitted shot {ANCHORS} in this order.")
        return self


class Finding(BaseModel):
    severity: Literal["clear_or_minor", "sustained_obvious_defect", "whole_frame_flash"]
    first: int | None = None
    last: int | None = None
    observation: str = Field(min_length=3, max_length=420)
    correction: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def place_defect(self):
        if self.severity != "clear_or_minor" and (self.first is None or self.last is None
                                                 or self.first > self.last or not self.correction.strip()):
            raise ValueError("Identify actual first and last frames and a correction for a clear defect.")
        return self


def plan_key(plan):
    return pixels._digest(VERSION, pixels.visual_plan(plan))


def _old_suspicions(decisions, cuts):
    targets = []
    for row in decisions.values():
        i = row.get("shot")
        if (row.get("version") != pixels.VERSION or row.get("ok") is not False or i not in cuts
                or row.get("board") != cuts[i]["review_board"] or row.get("window") != pixels.window(cuts[i])):
            continue
        native = [f for key in row.get("native_checks", []) for f in decisions.get(key, {}).get("confirmed", [])]
        first, last = pixels._batches(i)[0][0], pixels._batches(i)[-1][-1]
        numbers = {f["frame"] for f in native if first <= f["frame"] <= last}
        starts = sorted(n for n in numbers if all(n + step in numbers for step in range(4)))
        if not starts:
            continue
        center = starts[len(starts) // 2] + 1
        targets.append((i, center, next(f["observation"] for f in native if f["frame"] == center)))
    return sorted(set(targets))


def valid(decisions, proof, plan, cuts):
    row = decisions.get(proof.get("playback_check")) or {}
    boards = proof.get("cut_assets") or []
    windows = row.get("windows") or []
    if (row.get("version") != VERSION or row.get("plan") != plan_key(plan) or row.get("ok") is not True
            or row.get("boards") != boards or len(boards) != len(recipe.BEATS) or len(windows) != len(boards)):
        return False
    if any(not isinstance(w, list) or len(w) != 5 or None in w or w[3] != beat.frames for w, beat in zip(windows, recipe.BEATS, strict=True)):
        return False
    if cuts is not None and windows != [pixels.window(cuts.get(i, {})) for i in range(1, len(boards) + 1)]:
        return False
    screen = decisions.get(row.get("screen")) or {}
    report = screen.get("report") or {}
    actual = [item.get("shot") for item in report.get("observations", []) if item.get("visible")]
    if concern := report.get("concern"):
        i, cell = concern.get("shot"), concern.get("cell")
        if i not in ANCHORS or cell not in (1, 2, 3, 4, 5):
            return False
        center = pixels._batches(i)[0][0] + round((windows[i - 1][3] - 1) * (cell - 1) / 4)
        if not any(t[0] == i and abs(t[1] - center) <= 5 for t in row.get("targets", [])):
            return False
    return (actual == list(ANCHORS) and screen.get("boards") == [boards[i - 1] for i in ANCHORS]
            and row.get("targets") is not None and len(row.get("details", [])) == len(row["targets"])
            and all((detail := decisions.get(key, {})).get("ok") is True and detail.get("neighborhood")
                    and (detail.get("shot"), detail.get("center")) == (target[0], target[1])
                    for key, target in zip(row.get("details", []), row["targets"], strict=True)))


async def inspect(ctx, folder, prefix, run, plan, cuts, material, cast, record):
    decisions = run.setdefault("director_decisions", {})
    boards = [cuts[i]["review_board"] for i in range(1, len(recipe.BEATS) + 1)]
    windows = [pixels.window(cuts[i]) for i in range(1, len(boards) + 1)]
    flagged = _old_suspicions(decisions, cuts)
    refs = [*dict.fromkeys([*material, *(cast[r] for r in ("protagonist", "authority") if cast.get(r))])]
    key = f"{recipe.REVIEW}:{VERSION}:{pixels._digest(VISIBILITY, plan_key(plan), boards, windows, flagged, refs)}"
    if key in decisions:
        return key, decisions[key]
    shots = recipe.shots(plan)
    selected = [boards[i - 1] for i in ANCHORS]
    screen_key = f"{recipe.REVIEW}:{VERSION}:anchors:{pixels._digest(VISIBILITY, plan_key(plan), selected, refs)}"
    screen = decisions.get(screen_key)
    if screen is None:
        actions = [{"shot": i, "action": shots[i - 1]["action"], "product_focus": shots[i - 1]["product_visible"],
                    **pixels._product_state(plan, shots[i - 1])} for i in ANCHORS]
        report = await reference_quality.ask(ctx, "commercial_normal_playback", Screening, pixels.LANGUAGE[plan.language] + GUIDE,
                    {"language": plan.language, "product": plan.product, "product_mode": plan.mode, "actions": actions,
                     "image_order": f"{len(refs)} product/cast references, then photographed five-frame strips for {ANCHORS}"}, [*refs, *selected],
                    max_tokens=1100, timeout=55)
        screen = await record(ctx, prefix, run, 28, screen_key, {**report, "boards": selected})
    if issue := screen["report"].get("concern"):
        i = issue["shot"]
        first = pixels._batches(i)[0][0]
        at = first + round((cuts[i]["output_frames"] - 1) * (issue["cell"] - 1) / 4)
        if not any(n == i and abs(f - at) <= 5 for n, f, _ in flagged):
            flagged.append((i, at, issue["reason"]))

    async def local(target):
        i, center, reason = target
        at, last = pixels._batches(i)[0][0], pixels._batches(i)[-1][-1]
        lo, hi = max(at, center - 6), min(last, center + 6)
        stamp = pixels._digest(VERSION, VISIBILITY, plan_key(plan), windows[i - 1], boards[i - 1], center, lo, hi, reason, refs)
        local_key = f"{recipe.REVIEW}:{VERSION}:area:{i}:{stamp}"
        if local_key in decisions:
            return local_key, decisions[local_key]
        edited = await pixels._edited_cut(ctx, Path(folder), cuts[i], stamp)
        frames = await pixels._frames(edited, lo - at, hi - at, small=True, language=plan.language)
        chart = await _reference_asset(ctx, f"normal-playback-{stamp}", pixels._sheet(frames, lo))
        image, = await pixels._frames(edited, center - at, center - at, small=False, language=plan.language)
        normal_size = await _reference_asset(ctx, f"normal-playback-{stamp}-center", pixels._jpeg(image))
        review = await reference_quality.ask(ctx, "commercial_check_playback_moment", Finding, pixels.LANGUAGE[plan.language] + DETAIL,
                     {"language": plan.language, "shot": i, "product": plan.product, "product_mode": plan.mode, "product_focus": shots[i - 1]["product_visible"],
                      "reason_to_check": reason, "first_frame": lo, "last_frame": hi,
                      "action": shots[i - 1]["action"], **pixels._product_state(plan, shots[i - 1]),
                      "references_first": len(refs), "normal_size_frame": center},
                     [*refs, chart, normal_size], max_tokens=650, timeout=50)
        value = Finding.model_validate(review["report"])
        visible = (value.first is not None and value.last is not None and lo <= value.first <= value.last <= hi and value.correction.strip()
                   and ((value.severity == "sustained_obvious_defect" and value.last - value.first >= 3)
                        or (value.severity == "whole_frame_flash" and lo < value.first <= value.last < hi)))
        entry = {**review, "version": VERSION, "identity": stamp, "shot": i, "center": center, "neighborhood": chart, "normal_size": normal_size,
                 "range": [lo, hi], "ok": not visible, "reason": value.observation, "correction": value.correction}
        return local_key, await record(ctx, prefix, run, i, local_key, entry)

    checked = await _parallel(flagged, local, concurrency=2) if flagged else []
    failed = [row for _, row in checked if not row["ok"]]
    receipt = {"version": VERSION, "plan": plan_key(plan), "boards": boards, "windows": windows, "screen": screen_key,
               "targets": [list(item) for item in flagged], "details": [k for k, _ in checked], "ok": not failed}
    if failed:
        receipt["issues"] = failed
    return key, await record(ctx, prefix, run, 28, key, receipt)
