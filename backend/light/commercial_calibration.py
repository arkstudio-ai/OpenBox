"""Bind an automatically directed hand-held product's height to its verified practical position."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from db.base import get_db_session
from db.models.video_job import VideoJob
from light import commercial_plan, reference_quality, remake_case


Slot = Literal["carry", "contact", "routine", "track_wardrobe", "handoff_preparation",
               "insert_1", "insert_2", "insert_3", "insert_4", "insert_5",
               "opening_1", "opening_2", "opening_3", "opening_4", "opening_5"]


class Edit(BaseModel):
    slot: Slot
    original: str = Field(min_length=1, max_length=130)
    replacement: str = Field(min_length=1, max_length=130)


class Alignment(BaseModel):
    conflicts_with_merchant: bool = Field(description="The actual pose conflicts with an explicitly requested height, grip, support or use.")
    same_actor_product_and_support: bool = Field(description="Same actor and exact item. Weight is visibly supported by the same specified hand, carrier or body region.")
    same_hand_side_and_feature: bool
    same_touch_is_reachable: bool
    only_natural_pose_details_differ: bool = Field(description="Only the height or the shape of the same supporting hand differs. No change to the action, item, gripping side or user requirements.")
    observed: commercial_plan.Direction
    edits: list[Edit] = Field(max_length=14)


class Check(BaseModel):
    respects_merchant: bool
    same_physical_action: bool
    position_matches_photo: bool
    consistent_everywhere: bool
    reason: commercial_plan.Direction


def _digest(plan):
    body = json.dumps(plan.model_dump(exclude={"obscured_product_videos"}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _directions(plan):
    return {"carry": plan.carry, "contact": plan.contact, "routine": plan.routine,
            "track_wardrobe": plan.track_wardrobe, "handoff_preparation": plan.handoff_preparation,
            **{f"insert_{i}": item for i, item in enumerate(plan.inserts, 1)},
            **{f"opening_{i}": item for i, item in enumerate(plan.insert_openings, 1)}}


def _apply(plan, edits):
    values = _directions(plan)
    if not edits or not any(edit.slot in {"carry", "contact"} for edit in edits):
        raise ValueError("Supply a physical height correction for carry or contact.")
    for edit in edits:
        text = values.get(edit.slot, "")
        if edit.original == edit.replacement or text.count(edit.original) != 1:
            raise ValueError(f"Quote a unique, exact height phrase from {edit.slot}.")
        values[edit.slot] = text.replace(edit.original, edit.replacement, 1)
    data = plan.model_dump()
    for key, text in values.items():
        if key.startswith(("insert_", "opening_")):
            kind, index = key.split("_")
            data["inserts" if kind == "insert" else "insert_openings"][int(index) - 1] = text
        else:
            data[key] = text
    return commercial_plan.Plan.model_validate(data)


async def _staging_form(ctx, prefix, plan, *, aligned=None, evidence=None):
    from light.production import _lock, _now

    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        work = form.get("run") or {}
        if (form.get("template") != "product_story" or work.get("action") != "render"
                or work.get("job_prefix") != prefix or not work.get("commercial_auto_staging")):
            return None
        existing = work.get("commercial_carry_calibration")
        if existing:
            return form if _digest(plan) in {existing["before_plan_digest"], existing["after_plan_digest"]} else None
        if (work.get("remake_stage") != "remake_frames" or work.get("wan_cuts") or work.get("wan_final_key")
                or any(":take:" in key for key in work.get("director_decisions", {}))
                or commercial_plan.Plan.model_validate(work["remake_adaptation"]) != plan):
            return None
        # Another worker or a previously submitted but unacknowledged take also pins the existing plan.
        shot = await db.scalar(select(VideoJob.id).where(VideoJob.session_id == ctx.session_id,
                             VideoJob.kind == "segment", VideoJob.idempotency_key.startswith(f"{prefix}:")).limit(1))
        if shot:
            return None
        if aligned is None:
            return form
        value = aligned.model_dump()
        work = {**work, "remake_adaptation": value, "commercial_carry_calibration": evidence}
        row.light_production = {**form, "run": work, "remake_adaptation": value,
                                "script": commercial_plan.display_script(aligned)}
        row.updated_at = _now()
        return row.light_production


async def align_carry(ctx, prefix, run, plan, photograph, cast, product, verdict):
    if plan.mode != "carry" or not run.get("commercial_auto_staging"):
        return None
    checks = verdict.get("checks", [])
    photographed = {item["photograph"] for item in checks}
    if photograph not in photographed:
        return None
    for item in checks:
        report = item["report"]
        if (not all(report.get(field) for field in ("actor_matches", "product_matches", "support_matches", "single_product"))
                or not report.get("feature_xy") or not report.get("landmark_xy")):
            return None
    form = await _staging_form(ctx, prefix, plan)
    if not form:
        return None
    if not form["run"].get("commercial_carry_calibration"):
        merchant = {"brief": form.get("brief"), "copy": remake_case.reference_copy(form),
                    "notes": form.get("notes"), "promotion": form.get("promotion_brief")}
        pictures = [photograph, cast, *product]
        data = {"language": plan.language, "merchant": merchant, "product": plan.appearance,
                "directions": _directions(plan), "observations": [item["report"]["reason"] for item in checks]}
        for attempt in range(2):
            result = await reference_quality.ask(
                ctx, "align_commercial_carry_height", Alignment,
                "场记校准开拍前编剧自拟的动作到第一张真实的合体定装，第二张是演员，余图是用户商品。"
                "独立检查同一实际商品、可见部件、数量、支撑、演员左右手和触碰角度。翻面、换手、靠新桌台、"
                "替代件、漂浮、手够不到、已改变商品用法都不属于自然姿态差异。若现有照片违背商家明确指定的高度、握法或穿挂位置，"
                "必须标记conflicts_with_merchant=true。若只是已经真实承重的商品部件略高或略低，按相片观察写出自然的人体位置。"
                "把所有确实描述同一移动中高度的句子统一到这个实际位置。还要逐字检查carry是否把手形写得比实拍更具体："
                "若图中确实由原来那只手在同一区域承受重量、指尖自然可及，而编剧写的是包握、实拍是掌托，"
                "必须在edits中另列短语，将未被商家指定的手形改写为照片中实际看见的承重手形。保留原身体侧、商品状态及运动可行性。"
                "上例中的两种手形都由同一只手同一区域真实承重，因此在商家未锁定握法时，符合only_natural_pose_details_differ。"
                "只替换最短的准确原文片段；"
                "开瓶喝水、在桌面准备和交接属于不同动作，不因碰点高度改变而改写。其余动作、左右、服装、语种不变。"
                "用成片语种写报告和替换短语。", data, pictures, max_tokens=2000, timeout=70)
            proposal = Alignment.model_validate(result["report"])
            if (proposal.conflicts_with_merchant or not all((proposal.same_actor_product_and_support,
                    proposal.same_hand_side_and_feature, proposal.same_touch_is_reachable,
                    proposal.only_natural_pose_details_differ))):
                return None
            try:
                adjusted = _apply(plan, proposal.edits)
            except ValueError as exc:
                if attempt:
                    return None
                data["correction"] = str(exc)[:320]
                continue
            break
        review = await reference_quality.ask(
            ctx, "verify_commercial_carry_height", Check,
            "独立核对图1实际携带商品的照片与用户要求。可调整自拟身体高度及同一承重手的具体掌形描述。"
            "同一演员、同一真实商品和部件、同一承重手或携具、触点侧别和有意义的动作方向必须成立。"
            "具体核对文字与掌指落点，手掌托在下面却仍写着手指环握四周，要判position_matches_photo=false。商家要求的特定握法不得被改。"
            "再对照每个涉及携带与三次转场的文本，须互相一致并能以此自然接拍。"
            "数值、颜色、服装、剧情或使用方法被改动，或用户指定高度被覆盖，均不通过。用成片语言写明具体证据。",
            {"language": plan.language, "merchant": merchant, "before": _directions(plan),
             "after": _directions(adjusted), "observed": proposal.observed}, pictures, max_tokens=900, timeout=60)
        check = Check.model_validate(review["report"])
        if not all((check.respects_merchant, check.same_physical_action, check.position_matches_photo,
                    check.consistent_everywhere)):
            return None
        proof = {"photograph": photograph, "cast": cast, "source_ids": list(product),
                 "before_plan_digest": _digest(plan), "after_plan_digest": _digest(adjusted),
                 "proposal": proposal.model_dump(), "review": review}
        form = await _staging_form(ctx, prefix, plan, aligned=adjusted, evidence=proof)
    if not form:
        return None
    stored = form["run"]["commercial_carry_calibration"]
    if stored["photograph"] != photograph or stored["cast"] != cast or stored["source_ids"] != list(product):
        return None
    run["commercial_carry_calibration"] = stored
    run["remake_adaptation"] = form["run"]["remake_adaptation"]
    return commercial_plan.Plan.model_validate(run["remake_adaptation"])
