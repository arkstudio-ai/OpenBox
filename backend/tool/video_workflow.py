"""Skill-only spoken-video production state, approvals, lint, and gates.

The model can propose scripts and prompts, but the backend owns the mutable
state machine.  Every approval is bound to a content hash, so editing a script,
segment, reference, transcript, or output automatically makes downstream
evidence stale without deleting its audit record.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update

from core.log import create_logger
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.video_workflow")

_PUNCT = re.compile(r"[\s。！？；：，、,.!?;:…·~—\-\"'“”‘’（）()《》<>【】\[\]]+")
_FILLERS = "嗯呃唔诶哦噢喔呀啊吧呢啦嘛"
_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v"}


class SegmentSpec(BaseModel):
    ordinal: int = Field(ge=1, le=100)
    role: Literal["hook", "body", "transition", "closing"] = "body"
    script_text: str = Field(min_length=1, max_length=2000)
    prompt: str = Field(min_length=1, max_length=32_000)
    input_assets: list[str] = Field(default_factory=list, max_length=7)
    # Per-segment model override; None = the configured default. Validated and
    # canonicalized against the provider routing table at set_segments time.
    model: str | None = Field(default=None, max_length=160)


class VideoProjectArgs(BaseModel):
    action: Literal[
        "create",
        "set_script",
        "set_segments",
        "request_approval",
        "revise_segment",
        "set_segment_feedback",
        "status",
    ]
    # Optional for non-create actions: omitted, the session's newest live
    # production is used (25-char ids get miscopied; never guess one).
    production_id: str | None = Field(default=None, max_length=96)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    brief: str | None = Field(default=None, min_length=1, max_length=10_000)
    mode: Literal["standard", "delegated"] = "standard"
    target_duration_seconds: int = Field(default=60, ge=15, le=180)
    ratio: Literal["9:16"] = "9:16"
    resolution: Literal["720p", "1080p"] = "720p"
    quality_policy: Literal["required", "advisory"] = "required"
    channel_name: str = Field(default="", max_length=100)
    script_text: str | None = Field(default=None, min_length=1, max_length=20_000)
    segment_prompt: str | None = Field(default=None, min_length=1, max_length=32_000)
    visual_anchor: str | None = Field(default=None, min_length=1, max_length=2000)
    character_reference_asset: str | None = Field(default=None, max_length=512)
    character_reference_type: Literal["virtual", "real_person"] = "virtual"
    character_identity_id: str | None = Field(default=None, max_length=96)
    segments: list[SegmentSpec] = Field(default_factory=list, max_length=100)
    approval_kind: Literal["script", "segments", "spend", "quality", "render"] | None = None
    segment_id: str | None = Field(default=None, max_length=96)
    revision_reason: str | None = Field(default=None, min_length=2, max_length=1000)
    feedback: Literal["approved", "rejected"] | None = None
    feedback_note: str | None = Field(default=None, max_length=1000)
    # Explicit, user-confirmed escalations: deactivating segments a paid call
    # already touched, or swapping the bound character reference.
    allow_replan: bool = False
    replace_character_reference: bool = False

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "create":
            if not self.title or not self.brief:
                raise ValueError("create requires title and brief")
        if self.action == "set_script" and not self.script_text:
            raise ValueError("set_script requires script_text")
        if self.action == "set_segments":
            if not self.visual_anchor:
                raise ValueError("set_segments requires visual_anchor")
            if not self.segments:
                raise ValueError("set_segments requires segments")
            if self.character_reference_type == "real_person":
                if not self.character_reference_asset:
                    raise ValueError("real_person requires character_reference_asset")
                if not self.character_identity_id:
                    raise ValueError("real_person requires an active character_identity_id")
            elif self.character_identity_id:
                raise ValueError("character_identity_id is only valid for real_person")
        if self.action == "request_approval" and not self.approval_kind:
            raise ValueError("request_approval requires approval_kind")
        if self.action == "revise_segment" and (not self.segment_id or not self.revision_reason):
            raise ValueError("revise_segment requires segment_id and revision_reason")
        if self.action == "set_segment_feedback":
            if not self.segment_id or not self.feedback:
                raise ValueError("set_segment_feedback requires segment_id and feedback")
            if self.feedback == "rejected" and not (self.feedback_note or "").strip():
                raise ValueError(
                    "rejected feedback requires feedback_note — it becomes the revision rationale"
                )
        return self


def content_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_spoken_text(value: str) -> str:
    compact = _PUNCT.sub("", value or "")
    return compact.translate({ord(char): None for char in _FILLERS})


def replace_segment_dialogue(
    script_text: str,
    segments: list[Any],
    source_segment_id: str,
    replacement_text: str,
) -> str:
    """Replace one segment in the full script without disturbing its separators."""
    cursor = 0
    ordered = sorted(segments, key=lambda row: row.ordinal)
    for row in ordered:
        start = script_text.find(row.script_text, cursor)
        if start < 0:
            break
        end = start + len(row.script_text)
        if row.id == source_segment_id:
            return script_text[:start] + replacement_text + script_text[end:]
        cursor = end
    return "\n".join(
        replacement_text if row.id == source_segment_id else row.script_text
        for row in ordered
    )


def compare_transcript(script_text: str, transcript_text: str, threshold: float = 0.90) -> dict:
    expected = normalize_spoken_text(script_text)
    heard = normalize_spoken_text(transcript_text)
    matcher = difflib.SequenceMatcher(None, expected, heard)
    similarity = matcher.ratio()
    notes: list[str] = []
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation == "delete" and i2 - i1 >= 2:
            notes.append(f"疑似漏念「{expected[i1:i2]}」")
        elif operation == "insert" and j2 - j1 >= 2:
            notes.append(f"疑似多念「{heard[j1:j2]}」")
        elif operation == "replace" and max(i2 - i1, j2 - j1) >= 1:
            notes.append(f"疑似念错「{expected[i1:i2]}→{heard[j1:j2]}」")
    return {
        "similarity": round(similarity, 3),
        "verdict": "ok" if similarity >= threshold and not notes else "suspect",
        "normalized_script": expected,
        "normalized_transcript": heard,
        "notes": notes,
        "threshold": threshold,
    }


_PROMPT_LINT_RULES: dict[str, dict[str, Any]] = {
    "dialogue_exact": {
        "requirement": (
            "The prompt must contain @ immediately followed by the exact segment dialogue."
        ),
        "accepted_examples": ["@<本段逐字台词>", "Speak exactly: @<exact segment dialogue>"],
    },
    "visual_continuity": {
        "requirement": "Declare one consistent visual base/anchor for the whole video.",
        "accepted_examples": [
            "全片一致的画面基底：同一人物、服装、场景、光线和产品",
            (
                "Consistent visual base: same presenter, wardrobe, set, lighting, "
                "and product throughout"
            ),
            "Visual anchor: same presenter and setting in every segment",
        ],
    },
    "fixed_camera": {
        "requirement": "Explicitly use a fixed/locked shot.",
        "accepted_examples": [
            "固定镜头",
            "固定机位",
            "Fixed shot",
            "Fixed camera",
            "Locked-off camera",
        ],
    },
    "framing": {
        "requirement": "Specify medium, half-body, or close-up framing.",
        "accepted_examples": ["中景", "半身", "近景", "Medium shot", "Half-body", "Close-up"],
    },
    "natural_action": {
        "requirement": "Describe a natural body/hand action for the presenter.",
        "accepted_examples": [
            "自然肢体动作：抬手展示产品",
            "Natural gestures: gently point to the product",
        ],
    },
    "tone": {
        "requirement": "Describe the speaking/performance tone.",
        "accepted_examples": ["语气：专业亲切", "Tone: calm, professional, and friendly"],
    },
    "no_subtitles": {
        "requirement": (
            "State that the generated segment has no subtitles; captions are added only in post."
        ),
        "accepted_examples": [
            "无字幕，字幕只能后期合成",
            "No subtitles; subtitles are added only in post-production",
            "Subtitles: none; captions added in post",
        ],
    },
    "unsafe_asset_reference": {
        "requirement": (
            "Do not put URLs or asset IDs in prompt text; use numbered reference labels."
        ),
        "accepted_examples": ["参考图片1", "参考视频1"],
    },
    "invalid_image_reference": {
        "requirement": (
            "Every numbered image reference must exist in this segment's supplied assets."
        ),
        "accepted_examples": ["参考图片1"],
    },
    "invalid_video_reference": {
        "requirement": (
            "Every numbered video reference must exist in this segment's supplied assets."
        ),
        "accepted_examples": ["参考视频1"],
    },
    "invalid_asset": {
        "requirement": "Every input_assets entry must be a ready image/video owned by this user.",
        "accepted_examples": ["Remove the invalid id or replace it with a ready owned asset id"],
    },
    "dialogue_too_long": {
        "requirement": "A segment may contain at most 48 normalized spoken characters.",
        "accepted_examples": ["Split this dialogue into two contiguous segments"],
    },
    "generated_output_as_reference": {
        "requirement": (
            "Reference the originally uploaded material, not a previously generated "
            "segment output of this production."
        ),
        "accepted_examples": ["Use the original upload's asset id as the reference"],
    },
}


def _contains_any(value: str, phrases: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(phrase.casefold() in lowered for phrase in phrases)


def _lint_issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _corrected_prompt_template(*, script_text: str, visual_anchor: str) -> str:
    """A directly usable bilingual-safe recipe accepted by the linter."""
    return (
        f"Consistent visual base: {visual_anchor}\n"
        "Fixed shot / fixed camera.\n"
        "Framing: vertical 9:16 half-body medium shot.\n"
        "Natural gestures: use restrained, natural hand gestures appropriate to this line.\n"
        "Tone: natural, professional, and friendly.\n"
        f"Speak exactly: @{script_text.strip()}\n"
        "Subtitles: none; subtitles are added only in post-production."
    )


def _prompt_lint_failure_result(
    *,
    action: str,
    visual_anchor: str,
    segments: list[tuple[int, str, list[dict[str, str]]]],
) -> ToolResult:
    """Return actionable lint evidence without making the model guess synonyms."""
    used_codes = {
        issue["code"]
        for _ordinal, _script, issues in segments
        for issue in issues
    }
    rules = {
        code: _PROMPT_LINT_RULES.get(
            code,
            {"requirement": "Correct the reported validation failure.", "accepted_examples": []},
        )
        for code in sorted(used_codes)
    }
    payload = {
        "error_code": "prompt_lint_failed",
        "action": action,
        "retry_policy": (
            "Do not submit identical arguments again. Change every segment listed below, "
            "using corrected_prompt_template as the safe starting point."
        ),
        "required_visual_anchor": visual_anchor,
        "rules": rules,
        "segments": [
            {
                "ordinal": ordinal,
                "missing": [issue["code"] for issue in issues],
                "failures": issues,
                "corrected_prompt_template": _corrected_prompt_template(
                    script_text=script_text,
                    visual_anchor=visual_anchor,
                ),
            }
            for ordinal, script_text, issues in segments
        ],
    }
    return ToolResult(
        title="Prompt lint failed",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        metadata={
            # It is a real failed mutation, while validation_failed tells the
            # history converter to replay this structured recipe in full
            # instead of applying the ordinary 200-character error truncation.
            "error": True,
            "validation_failed": True,
            "retry_requires_changed_args": True,
            "failure_code": "prompt_lint_failed",
        },
    )


def lint_segment_prompt(
    *,
    script_text: str,
    prompt: str,
    visual_anchor: str,
    image_count: int,
    video_count: int,
) -> dict:
    failures: list[str] = []
    issues: list[dict[str, str]] = []
    warnings: list[str] = []

    def fail(code: str, message: str) -> None:
        failures.append(message)
        issues.append(_lint_issue(code, message))

    spoken_length = len(normalize_spoken_text(script_text))
    if spoken_length > 48:
        fail("dialogue_too_long", f"台词 {spoken_length} 字，超过 48 字硬上限")
    elif spoken_length > 40:
        warnings.append(f"台词 {spoken_length} 字，建议压到 40 字以内")
    if f"@{script_text.strip()}" not in prompt:
        fail("dialogue_exact", "prompt 必须用 @ 紧接本段逐字台词")

    anchor = visual_anchor.strip()
    anchor_is_literal = bool(anchor) and anchor.casefold() in prompt.casefold()
    anchor_is_declared = _contains_any(
        prompt,
        (
            "全片一致的画面基底",
            "全片一致画面基底",
            "画面一致性",
            "一致的视觉基底",
            "consistent visual base",
            "consistent visual anchor",
            "visual anchor",
        ),
    )
    if not (anchor_is_literal or anchor_is_declared):
        fail("visual_continuity", "prompt 缺少全片一致的画面基底")
    if not _contains_any(
        prompt,
        (
            "固定镜头",
            "固定机位",
            "锁定镜头",
            "fixed shot",
            "fixed camera",
            "locked-off camera",
            "locked off camera",
            "static camera",
        ),
    ):
        fail("fixed_camera", "prompt 必须显式声明固定镜头")
    if not _contains_any(
        prompt,
        (
            "中景",
            "半身",
            "近景",
            "中近景",
            "medium shot",
            "half-body",
            "half body",
            "close-up",
            "close up",
            "medium close-up",
        ),
    ):
        fail("framing", "prompt 缺少中景/半身/近景构图")
    if not _contains_any(
        prompt,
        (
            "自然肢体动作",
            "自然动作",
            "手势",
            "姿态",
            "微笑",
            "点头",
            "前倾",
            "抬手",
            "举起",
            "拿起",
            "转动",
            "托住",
            "放回",
            "指向",
            "natural gesture",
            "natural movement",
            "hand gesture",
            "point to",
            "point toward",
            "gently lift",
            "gently hold",
        ),
    ):
        fail("natural_action", "prompt 缺少自然肢体动作")
    if not _contains_any(
        prompt,
        ("语气", "口播语气", "tone", "delivery", "speaking style", "performance style"),
    ):
        fail("tone", "prompt 缺少语气描述")
    if not _contains_any(
        prompt,
        (
            "无字幕",
            "不要字幕",
            "不显示字幕",
            "no subtitles",
            "without subtitles",
            "subtitles: none",
            "captions: none",
        ),
    ):
        fail("no_subtitles", "prompt 必须写明无字幕，字幕只能后期合成")
    if re.search(r"https?://|asset://|asset[_-][A-Za-z0-9]", prompt, re.IGNORECASE):
        fail(
            "unsafe_asset_reference",
            "prompt 正文不得包含 URL 或素材 ID，只能用参考图片N/参考视频N",
        )
    for value in re.findall(r"参考图片(\d+)", prompt):
        if int(value) < 1 or int(value) > image_count:
            fail(
                "invalid_image_reference",
                f"prompt 引用了不存在的参考图片{value}（实际 {image_count} 张）",
            )
    for value in re.findall(r"参考视频(\d+)", prompt):
        if int(value) < 1 or int(value) > video_count:
            fail(
                "invalid_video_reference",
                f"prompt 引用了不存在的参考视频{value}（实际 {video_count} 个）",
            )
    return {"ok": not failures, "failures": failures, "issues": issues, "warnings": warnings}


async def _owned_production(db, production_id: str, user_id: str):
    from db.models.video_production import VideoProduction

    return (
        await db.execute(
            select(VideoProduction).where(
                VideoProduction.id == production_id,
                VideoProduction.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def _resolve_production(db, args: VideoProjectArgs, ctx: ToolContext):
    """Explicit production_id wins; otherwise the session's newest live
    (non-delivered) production. Returns (production | None, error_message)."""
    from db.models.video_production import VideoProduction

    if args.production_id:
        production = await _owned_production(db, args.production_id, ctx.user_id)
        if not production:
            return None, "No owned production has that production_id."
        return production, ""
    if not ctx.session_id:
        return None, (
            "No production_id given and no session to resolve one from. "
            "Pass production_id explicitly."
        )
    production = (
        await db.execute(
            select(VideoProduction)
            .where(
                VideoProduction.user_id == ctx.user_id,
                VideoProduction.session_id == ctx.session_id,
                VideoProduction.status != "delivered",
            )
            .order_by(VideoProduction.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not production:
        return None, (
            "No active production in this session. Pass production_id explicitly "
            "or create one. Never retry with a guessed id — call status with no id "
            "to see what exists."
        )
    return production, ""


async def _active_segments(db, production_id: str):
    from db.models.video_production import VideoSegment

    return list(
        (
            await db.execute(
                select(VideoSegment)
                .where(
                    VideoSegment.production_id == production_id,
                    VideoSegment.is_active.is_(True),
                )
                .order_by(VideoSegment.ordinal)
            )
        ).scalars()
    )


_IN_FLIGHT_STATUSES = {"submitting", "queued", "in_progress", "generating", "finalizing", "transfer_failed"}


def _replan_guard(rows: list[Any], *, allow_replan: bool, action: str) -> ToolResult | None:
    """Refuse to deactivate segments a paid call already touched.

    In-flight jobs are refused unconditionally (money in flight); settled
    non-planned segments (generated/failed/cancelled) need an explicit,
    user-confirmed allow_replan. Old outputs are never deleted either way —
    deactivated rows survive as inactive revisions.
    """
    if not rows:
        return None
    in_flight = [row for row in rows if row.status in _IN_FLIGHT_STATUSES]
    if in_flight:
        detail = ", ".join(f"segment {row.ordinal}={row.status}" for row in in_flight)
        return ToolResult(
            title="Segments still running",
            output=(
                f"{action} would deactivate segments with jobs in flight: {detail}. "
                "Wait for them to settle or cancel them first; allow_replan does not "
                "override running jobs."
            ),
        )
    touched = [row for row in rows if row.status != "planned"]
    if touched and not allow_replan:
        detail = ", ".join(f"segment {row.ordinal}={row.status}" for row in touched)
        return ToolResult(
            title="Replan confirmation required",
            output=(
                f"{action} would deactivate segments a paid call already produced: {detail}. "
                "Use revise_segment for selective fixes. To replan anyway, tell the user "
                "the old outputs stay archived as inactive revisions and resubmit with "
                "allow_replan=true."
            ),
        )
    return None


async def _own_output_asset_ids(db, production_id: str) -> set[str]:
    from db.models.video_production import VideoSegment

    rows = await db.execute(
        select(VideoSegment.output_asset_id).where(
            VideoSegment.production_id == production_id,
            VideoSegment.output_asset_id.is_not(None),
        )
    )
    return {value for (value,) in rows if value}


async def _matching_approval(db, production_id: str, kind: str, scope_hash: str):
    from db.models.video_production import VideoApproval

    return (
        await db.execute(
            select(VideoApproval)
            .where(
                VideoApproval.production_id == production_id,
                VideoApproval.kind == kind,
                VideoApproval.scope_hash == scope_hash,
                VideoApproval.decision.in_(["approved", "override"]),
            )
            .order_by(VideoApproval.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def spend_scope(production, segments: list[Any]) -> str:
    return content_hash(
        {
            "plan_hash": production.plan_hash,
            "segment_ids": [row.id for row in segments],
            "resolution": production.resolution,
            "ratio": production.ratio,
            "generate_audio": True,
        }
    )


def quality_scope(production, segments: list[Any]) -> str:
    return content_hash(
        {
            "plan_hash": production.plan_hash,
            "segments": [
                {
                    "id": row.id,
                    "output_asset_id": row.output_asset_id,
                    "transcript": row.transcript_text,
                    "similarity": row.stt_similarity,
                    "verdict": row.stt_verdict,
                }
                for row in segments
            ],
        }
    )


def render_scope(production, segments: list[Any]) -> str:
    return content_hash(
        {
            "quality_scope": quality_scope(production, segments),
            "channel_name": production.channel_name,
            "ratio": production.ratio,
            "resolution": production.resolution,
        }
    )


async def _derive_status(db, production, segments: list[Any] | None = None) -> str:
    segments = segments if segments is not None else await _active_segments(db, production.id)
    if not production.script_hash:
        return "init"
    if not await _matching_approval(db, production.id, "script", production.script_hash):
        return "needs_script_approval"
    if not production.plan_hash or not segments:
        return "script_ok"
    if not await _matching_approval(db, production.id, "segments", production.plan_hash):
        return "needs_segments_approval"
    if any(row.status in {"failed", "cancelled"} for row in segments):
        return "needs_segment_revision"
    # A user-rejected generated segment routes the workflow back to
    # revise_segment (only rejected segments get regenerated).
    if any(row.review_status == "user_rejected" for row in segments):
        return "needs_segment_revision"
    if any(row.status in {"submitting", "queued", "in_progress", "generating", "finalizing", "transfer_failed"} for row in segments):
        return "generating"
    spend = await _matching_approval(db, production.id, "spend", spend_scope(production, segments))
    if not spend:
        return "needs_spend_approval"
    pending = [row for row in segments if row.status == "planned"]
    if pending:
        if spend.max_calls is None or spend.used_calls >= spend.max_calls:
            return "needs_spend_approval"
        return "spend_ok"
    if not all(row.status == "generated" and row.output_asset_id for row in segments):
        return "needs_segment_revision"
    if not all(row.stt_verdict for row in segments):
        return "generated"
    if production.quality_policy == "required" and not await _matching_approval(
        db, production.id, "quality", quality_scope(production, segments)
    ):
        return "needs_quality_approval"
    if not await _matching_approval(db, production.id, "render", render_scope(production, segments)):
        return "needs_render_approval"
    if production.render_asset_id:
        return "delivered"
    return "ready_to_render"


async def _refresh_status(db, production, segments: list[Any] | None = None) -> str:
    status = await _derive_status(db, production, segments)
    production.status = status
    production.updated_at = datetime.now(timezone.utc)
    return status


async def _owned_ready_asset(db, ref: str, user_id: str):
    from db.models.file_asset import FileAsset

    value = ref[6:] if ref.startswith("asset:") else ref
    return (
        await db.execute(
            select(FileAsset).where(
                FileAsset.id == value,
                FileAsset.user_id == user_id,
                FileAsset.status == "ready",
                FileAsset.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()


def _plan_hash(production, segments: list[Any]) -> str:
    return content_hash(
        {
            "script_hash": production.script_hash,
            "visual_anchor": production.visual_anchor,
            "character_asset_id": production.character_asset_id,
            "character_reference_type": production.character_reference_type,
            "character_identity_id": production.character_identity_id,
            "segments": [
                {
                    "id": row.id,
                    "ordinal": row.ordinal,
                    "revision": row.revision,
                    "role": row.role,
                    "content_hash": row.content_hash,
                    "input_asset_ids": list(row.input_asset_ids or []),
                }
                for row in segments
            ],
        }
    )


async def production_snapshot(production_id: str, user_id: str) -> dict[str, Any] | None:
    """Full production snapshot; the public name is imported by the HTTP API."""
    from db.base import get_db_session

    async with get_db_session() as db:
        production = await _owned_production(db, production_id, user_id)
        if not production:
            return None
        segments = await _active_segments(db, production.id)
        status = await _refresh_status(db, production, segments)
        scopes = {
            "script": production.script_hash,
            "segments": production.plan_hash,
            "spend": spend_scope(production, segments) if segments else "",
            "quality": quality_scope(production, segments) if segments else "",
            "render": render_scope(production, segments) if segments else "",
        }
        approvals = {
            kind: bool(scope and await _matching_approval(db, production.id, kind, scope))
            for kind, scope in scopes.items()
        }
        return {
            "production_id": production.id,
            "title": production.title,
            "brief": production.brief,
            "status": status,
            "mode": production.mode,
            "target_duration_seconds": production.target_duration_seconds,
            "ratio": production.ratio,
            "resolution": production.resolution,
            "quality_policy": production.quality_policy,
            "subtitles": production.subtitles,
            "channel_name": production.channel_name,
            "script_text": production.script_text,
            "script_hash": production.script_hash,
            "visual_anchor": production.visual_anchor,
            "character_asset_id": production.character_asset_id,
            "character_reference_type": production.character_reference_type,
            "character_identity_id": production.character_identity_id,
            "plan_hash": production.plan_hash,
            "render_asset_id": production.render_asset_id,
            "render_idempotency_key": (
                f"{production.id}:render:{scopes['render'][:16]}"
                if scopes["render"] and approvals.get("render")
                else ""
            ),
            "approvals": approvals,
            "segments": [
                {
                    "segment_id": row.id,
                    "ordinal": row.ordinal,
                    "revision": row.revision,
                    "role": row.role,
                    "script_text": row.script_text,
                    "prompt": row.prompt,
                    "input_asset_ids": list(row.input_asset_ids or []),
                    "content_hash": row.content_hash,
                    "model": row.model,
                    "lint": row.lint_data,
                    "status": row.status,
                    "generation_job_id": row.generation_job_id,
                    "output_asset_id": row.output_asset_id,
                    "transcript_text": row.transcript_text,
                    "stt_similarity": row.stt_similarity,
                    "stt_verdict": row.stt_verdict,
                    "stt_notes": list(row.stt_notes or []),
                    "review_status": row.review_status,
                    "review_note": row.review_note,
                    "generation_idempotency_key": f"{production.id}:{row.id}:generate",
                    "transcription_idempotency_key": f"{production.id}:{row.id}:stt",
                }
                for row in segments
            ],
        }


def _snapshot_output(value: dict[str, Any]) -> str:
    lines = [
        f"production_id={value['production_id']}",
        f"status={value['status']}",
        f"title={value['title']}",
        f"script_hash={value['script_hash']}",
        f"plan_hash={value['plan_hash']}",
        f"visual_anchor={value.get('visual_anchor', '')}",
        f"character_asset_id={value.get('character_asset_id') or ''}",
        f"character_reference_type={value.get('character_reference_type') or 'virtual'}",
        f"character_identity_id={value.get('character_identity_id') or ''}",
        f"render_idempotency_key={value.get('render_idempotency_key', '')}",
        "approvals=" + json.dumps(value["approvals"], ensure_ascii=False, separators=(",", ":")),
    ]
    for row in value["segments"]:
        lines.extend(
            [
                f"segment_{row['ordinal']}_id={row['segment_id']}",
                f"segment_{row['ordinal']}_revision={row['revision']}",
                f"segment_{row['ordinal']}_status={row['status']}",
                f"segment_{row['ordinal']}_generation_job_id={row['generation_job_id'] or ''}",
                f"segment_{row['ordinal']}_script={row['script_text']}",
                f"segment_{row['ordinal']}_prompt={row['prompt']}",
                f"segment_{row['ordinal']}_output_asset_id={row['output_asset_id'] or ''}",
                f"segment_{row['ordinal']}_stt={row['stt_verdict'] or ''}:{row['stt_similarity'] if row['stt_similarity'] is not None else ''}",
                f"segment_{row['ordinal']}_transcript={row['transcript_text'] or ''}",
                "segment_{}_stt_notes={}".format(
                    row["ordinal"],
                    json.dumps(row["stt_notes"], ensure_ascii=False, separators=(",", ":")),
                ),
                f"segment_{row['ordinal']}_review_status={row['review_status'] or ''}",
                f"segment_{row['ordinal']}_review_note={row.get('review_note') or ''}",
                f"segment_{row['ordinal']}_generation_key={row['generation_idempotency_key']}",
                f"segment_{row['ordinal']}_stt_key={row['transcription_idempotency_key']}",
            ]
        )
    return "\n".join(lines)


async def execute_project(args: VideoProjectArgs, ctx: ToolContext) -> ToolResult:
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.video_production import VideoApproval, VideoProduction, VideoSegment

    now = datetime.now(timezone.utc)
    if args.action == "create":
        production_id = ascending("production")
        async with get_db_session() as db:
            row = VideoProduction(
                id=production_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id or None,
                project_id=ctx.project_id or None,
                title=(args.title or "").strip(),
                brief=(args.brief or "").strip(),
                mode=args.mode,
                status="init",
                target_duration_seconds=args.target_duration_seconds,
                ratio=args.ratio,
                resolution=args.resolution,
                quality_policy=args.quality_policy,
                channel_name=args.channel_name.strip(),
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        snapshot = await production_snapshot(production_id, ctx.user_id)
        return ToolResult(
            title="Video production created",
            output=_snapshot_output(snapshot or {}),
            metadata=snapshot or {},
        )

    async with get_db_session() as db:
        production, resolve_error = await _resolve_production(db, args, ctx)
        if not production:
            return ToolResult(title="Video production not found", output=resolve_error)
        resolved_id = production.id

        if args.action == "set_script":
            script = (args.script_text or "").strip()
            new_hash = content_hash({"script_text": script})
            if new_hash != production.script_hash:
                refusal = _replan_guard(
                    await _active_segments(db, production.id),
                    allow_replan=args.allow_replan,
                    action="set_script",
                )
                if refusal:
                    return refusal
                await db.execute(
                    update(VideoSegment)
                    .where(VideoSegment.production_id == production.id, VideoSegment.is_active.is_(True))
                    .values(is_active=False, updated_at=now)
                )
                production.plan_hash = ""
                production.visual_anchor = ""
                production.character_asset_id = None
                production.character_reference_type = "virtual"
                production.character_identity_id = None
                production.render_asset_id = None
                production.subtitles = None
            production.script_text = script
            production.script_hash = new_hash
            production.error = None
            segments = await _active_segments(db, production.id)
            await _refresh_status(db, production, segments)

        elif args.action == "set_segments":
            if not await _matching_approval(db, production.id, "script", production.script_hash):
                return ToolResult(
                    title="Script approval required",
                    output="The current script hash has no user approval. Request script approval before planning segments.",
                )
            ordinals = [item.ordinal for item in args.segments]
            if len(set(ordinals)) != len(ordinals) or sorted(ordinals) != list(range(1, len(ordinals) + 1)):
                return ToolResult(title="Invalid segment plan", output="Segment ordinals must be unique and contiguous from 1.")
            if normalize_spoken_text("".join(item.script_text for item in args.segments)) != normalize_spoken_text(
                production.script_text
            ):
                return ToolResult(
                    title="Invalid segment plan",
                    output="Concatenated segment dialogue does not exactly match the approved script.",
                )
            character = None
            if args.character_reference_asset:
                character = await _owned_ready_asset(db, args.character_reference_asset, ctx.user_id)
                if not character or not character.mime.startswith("image/"):
                    return ToolResult(
                        title="Invalid character reference",
                        output="character_reference_asset must be a ready owned image asset.",
                    )
            existing_active = await _active_segments(db, production.id)
            new_character_id = character.id if character else None
            if (
                production.character_asset_id
                and new_character_id != production.character_asset_id
                and any(row.status != "planned" for row in existing_active)
                and not args.replace_character_reference
            ):
                return ToolResult(
                    title="Character reference locked",
                    output=(
                        "The production already has a character anchor bound to generated "
                        "output. Pass replace_character_reference=true only when the user "
                        "explicitly changes the presenter."
                    ),
                )
            own_outputs = await _own_output_asset_ids(db, production.id)
            model_by_ordinal: dict[int, str | None] = {}
            for item in args.segments:
                if not item.model:
                    model_by_ordinal[item.ordinal] = None
                    continue
                from core.config import get_config
                from tool.video_providers import resolve_route

                try:
                    route = resolve_route(item.model, get_config())
                except Exception as exc:
                    return ToolResult(
                        title="Invalid segment model",
                        output=f"segment {item.ordinal}: {exc}",
                    )
                model_by_ordinal[item.ordinal] = route.model
            identity = None
            if args.character_reference_type == "real_person":
                from db.models.video_material import VideoMaterialAsset, VideoMaterialGroup

                identity = (
                    await db.execute(
                        select(VideoMaterialGroup).where(
                            VideoMaterialGroup.id == args.character_identity_id,
                            VideoMaterialGroup.user_id == ctx.user_id,
                            VideoMaterialGroup.group_type == "LivenessFace",
                            VideoMaterialGroup.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if not identity or not identity.provider_group_id:
                    return ToolResult(
                        title="真人授权尚未完成",
                        output=(
                            "character_identity_id must be an active, user-owned LivenessFace identity. "
                            "Call video_identity.create/status and wait for the person to finish authorization."
                        ),
                    )
                binding = (
                    await db.execute(
                        select(VideoMaterialAsset).where(
                            VideoMaterialAsset.user_id == ctx.user_id,
                            VideoMaterialAsset.group_id == identity.id,
                            VideoMaterialAsset.source_asset_id == character.id,
                            VideoMaterialAsset.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if not binding or not binding.provider_asset_id:
                    return ToolResult(
                        title="真人参考素材尚未入库",
                        output=(
                            "The selected portrait is not active in this LivenessFace identity. "
                            "Call video_identity.add_asset with this identity_id and character asset_id first."
                        ),
                    )
            validated: list[tuple[SegmentSpec, list[Any], dict[str, Any], str]] = []
            issues_by_ordinal: dict[int, list[dict[str, str]]] = {}
            for item in args.segments:
                assets: list[Any] = []
                seen: set[str] = set()
                for ref in item.input_assets:
                    asset = await _owned_ready_asset(db, ref, ctx.user_id)
                    if not asset or not (asset.mime.startswith("image/") or asset.mime in _VIDEO_MIMES):
                        message = f"素材 {ref} 不是可用的自有图片/视频"
                        issues_by_ordinal.setdefault(item.ordinal, []).append(
                            _lint_issue("invalid_asset", message)
                        )
                        continue
                    if asset.id in own_outputs:
                        issues_by_ordinal.setdefault(item.ordinal, []).append(
                            _lint_issue(
                                "generated_output_as_reference",
                                f"素材 {ref} 是本片自己生成的段产物，不能回流作参考素材",
                            )
                        )
                        continue
                    if asset.id not in seen and (not character or asset.id != character.id):
                        assets.append(asset)
                        seen.add(asset.id)
                image_count = int(bool(character)) + sum(row.mime.startswith("image/") for row in assets)
                video_count = sum(row.mime in _VIDEO_MIMES for row in assets)
                lint = lint_segment_prompt(
                    script_text=item.script_text.strip(),
                    prompt=item.prompt.strip(),
                    visual_anchor=(args.visual_anchor or "").strip(),
                    image_count=image_count,
                    video_count=video_count,
                )
                if lint["issues"]:
                    issues_by_ordinal.setdefault(item.ordinal, []).extend(lint["issues"])
                hash_payload = {
                    "role": item.role,
                    "script_text": item.script_text.strip(),
                    "prompt": item.prompt.strip(),
                    "character_asset_id": character.id if character else None,
                    "character_reference_type": args.character_reference_type,
                    "character_identity_id": identity.id if identity else None,
                    "input_asset_ids": [row.id for row in assets],
                    "visual_anchor": (args.visual_anchor or "").strip(),
                }
                # Conditional key: existing productions' stored hashes stay
                # valid; setting a model forks the hash (and thus the plan and
                # spend scopes — a different model is a different price).
                if model_by_ordinal.get(item.ordinal):
                    hash_payload["model"] = model_by_ordinal[item.ordinal]
                item_hash = content_hash(hash_payload)
                validated.append((item, assets, lint, item_hash))
            if issues_by_ordinal:
                return _prompt_lint_failure_result(
                    action="set_segments",
                    visual_anchor=(args.visual_anchor or "").strip(),
                    segments=[
                        (
                            item.ordinal,
                            item.script_text.strip(),
                            issues_by_ordinal[item.ordinal],
                        )
                        for item in args.segments
                        if item.ordinal in issues_by_ordinal
                    ],
                )

            current = {row.ordinal: row for row in existing_active}
            hash_by_ordinal = {item.ordinal: item_hash for item, _assets, _lint, item_hash in validated}
            displaced = [
                row
                for row in current.values()
                if row.ordinal not in ordinals or row.content_hash != hash_by_ordinal.get(row.ordinal)
            ]
            refusal = _replan_guard(displaced, allow_replan=args.allow_replan, action="set_segments")
            if refusal:
                return refusal
            for row in current.values():
                if row.ordinal not in ordinals:
                    row.is_active = False
                    row.updated_at = now
            active: list[Any] = []
            for item, assets, lint, item_hash in validated:
                existing = current.get(item.ordinal)
                if existing and existing.content_hash == item_hash:
                    active.append(existing)
                    continue
                if existing:
                    existing.is_active = False
                    existing.updated_at = now
                latest_revision = (
                    await db.execute(
                        select(func.max(VideoSegment.revision)).where(
                            VideoSegment.production_id == production.id,
                            VideoSegment.ordinal == item.ordinal,
                        )
                    )
                ).scalar_one_or_none() or 0
                segment = VideoSegment(
                    id=ascending("segment"),
                    production_id=production.id,
                    ordinal=item.ordinal,
                    revision=latest_revision + 1,
                    is_active=True,
                    role=item.role,
                    script_text=item.script_text.strip(),
                    prompt=item.prompt.strip(),
                    content_hash=item_hash,
                    model=model_by_ordinal.get(item.ordinal),
                    input_asset_ids=[row.id for row in assets],
                    lint_data=lint,
                    status="planned",
                    created_at=now,
                    updated_at=now,
                )
                db.add(segment)
                active.append(segment)
            await db.flush()
            active.sort(key=lambda row: row.ordinal)
            production.visual_anchor = (args.visual_anchor or "").strip()
            production.character_asset_id = character.id if character else None
            production.character_reference_type = args.character_reference_type
            production.character_identity_id = identity.id if identity else None
            production.plan_hash = _plan_hash(production, active)
            production.render_asset_id = None
            production.subtitles = None
            production.error = None
            await _refresh_status(db, production, active)

        elif args.action == "revise_segment":
            segments = await _active_segments(db, production.id)
            source = next((row for row in segments if row.id == args.segment_id), None)
            if not source:
                return ToolResult(title="Active segment not found", output="segment_id is not active in this production.")

            revised_script = (args.script_text or source.script_text).strip()
            revised_prompt = (args.segment_prompt or source.prompt).strip()
            if revised_script != source.script_text and not args.segment_prompt:
                old_spoken_token = f"@{source.script_text.strip()}"
                if old_spoken_token not in source.prompt:
                    return ToolResult(
                        title="Replacement prompt required",
                        output=(
                            "The existing prompt does not contain the old word-for-word dialogue. "
                            "Provide segment_prompt together with script_text."
                        ),
                    )
                revised_prompt = source.prompt.replace(
                    old_spoken_token,
                    f"@{revised_script}",
                    1,
                )

            character = None
            if production.character_asset_id:
                character = await _owned_ready_asset(db, production.character_asset_id, ctx.user_id)
                if not character or not character.mime.startswith("image/"):
                    return ToolResult(
                        title="Invalid character reference",
                        output="The production character reference is no longer a ready owned image asset.",
                    )
            assets: list[Any] = []
            for ref in source.input_asset_ids or []:
                asset = await _owned_ready_asset(db, ref, ctx.user_id)
                if not asset or not (asset.mime.startswith("image/") or asset.mime in _VIDEO_MIMES):
                    return ToolResult(
                        title="Invalid segment reference",
                        output=f"Segment reference {ref} is no longer a ready owned image/video asset.",
                    )
                assets.append(asset)
            lint = lint_segment_prompt(
                script_text=revised_script,
                prompt=revised_prompt,
                visual_anchor=production.visual_anchor,
                image_count=int(bool(character)) + sum(row.mime.startswith("image/") for row in assets),
                video_count=sum(row.mime in _VIDEO_MIMES for row in assets),
            )
            if lint["failures"]:
                return _prompt_lint_failure_result(
                    action="revise_segment",
                    visual_anchor=production.visual_anchor,
                    segments=[(source.ordinal, revised_script, lint["issues"])],
                )

            if revised_script != source.script_text:
                production.script_text = replace_segment_dialogue(
                    production.script_text,
                    segments,
                    source.id,
                    revised_script,
                )
                production.script_hash = content_hash({"script_text": production.script_text})

            revise_hash_payload = {
                "role": source.role,
                "script_text": revised_script,
                "prompt": revised_prompt,
                "character_asset_id": character.id if character else None,
                "character_reference_type": production.character_reference_type,
                "character_identity_id": production.character_identity_id,
                "input_asset_ids": [row.id for row in assets],
                "visual_anchor": production.visual_anchor,
            }
            if source.model:
                revise_hash_payload["model"] = source.model
            item_hash = content_hash(revise_hash_payload)
            source.is_active = False
            source.updated_at = now
            latest_revision = (
                await db.execute(
                    select(func.max(VideoSegment.revision)).where(
                        VideoSegment.production_id == production.id,
                        VideoSegment.ordinal == source.ordinal,
                    )
                )
            ).scalar_one_or_none() or 0
            replacement = VideoSegment(
                id=ascending("segment"),
                production_id=production.id,
                ordinal=source.ordinal,
                revision=latest_revision + 1,
                is_active=True,
                role=source.role,
                script_text=revised_script,
                prompt=revised_prompt,
                content_hash=item_hash,
                model=source.model,
                input_asset_ids=list(source.input_asset_ids or []),
                lint_data={**lint, "revision_reason": args.revision_reason},
                status="planned",
                created_at=now,
                updated_at=now,
            )
            db.add(replacement)
            await db.flush()
            active = [replacement if row.id == source.id else row for row in segments]
            active.sort(key=lambda row: row.ordinal)
            production.plan_hash = _plan_hash(production, active)
            production.render_asset_id = None
            production.subtitles = None
            production.error = None
            await _refresh_status(db, production, active)

        elif args.action == "request_approval":
            segments = await _active_segments(db, production.id)
            kind = args.approval_kind or ""
            scope = ""
            question = ""
            options: list[tuple[str, str]] = []
            metadata: dict[str, Any] = {}
            question_detail: dict[str, Any] | None = None
            if kind == "script":
                if not production.script_hash:
                    return ToolResult(title="Nothing to approve", output="Set the script first.")
                scope = production.script_hash
                question = f"完整讲稿共 {len(normalize_spoken_text(production.script_text))} 字，确认按这版进入分段吗？"
                question_detail = {
                    "kind": "video_script_approval",
                    "script_text": production.script_text,
                }
                options = [("可以，按这版分段", "确认当前讲稿内容哈希"), ("需要修改", "不确认，先修改讲稿")]
            elif kind == "segments":
                if not segments or not production.plan_hash:
                    return ToolResult(title="Nothing to approve", output="Set and lint the segment plan first.")
                scope = production.plan_hash
                question = f"以上 {len(segments)} 段完整台词、提示词和素材已通过 lint，确认按这版生成吗？"
                question_detail = {
                    "kind": "video_segments_approval",
                    "segments": [
                        {
                            "ordinal": row.ordinal,
                            "role": row.role,
                            "script_text": row.script_text,
                            "prompt": row.prompt,
                        }
                        for row in sorted(segments, key=lambda row: row.ordinal)
                    ],
                }
                options = [("分段可以", "确认当前分段与素材快照"), ("需要调整", "不确认，先调整分段或画面")]
            elif kind == "spend":
                if not await _matching_approval(db, production.id, "segments", production.plan_hash):
                    return ToolResult(title="Segment approval required", output="Approve the current segment plan before spend approval.")
                blocked = [row for row in segments if row.status not in {"planned", "generated"}]
                if blocked:
                    detail = ", ".join(f"segment {row.ordinal}={row.status}" for row in blocked)
                    return ToolResult(
                        title="Resolve active segment jobs first",
                        output=(
                            "Spend approval only covers newly planned revisions. Resolve, cancel, or revise "
                            f"the following active segments before requesting more paid calls: {detail}."
                        ),
                    )
                pending = [row for row in segments if row.status == "planned"]
                if not pending:
                    return ToolResult(title="No generation spend needed", output="Every active segment is already generated.")
                scope = spend_scope(production, segments)
                estimated_seconds = sum(max(4, min(15, round(len(normalize_spoken_text(row.script_text)) / 3.2))) for row in pending)
                metadata = {"segment_ids": [row.id for row in pending], "estimated_seconds": estimated_seconds}
                # Name the model that will actually be billed. Hard-coding
                # "Seedance" here misreported the spend at the exact moment the
                # user authorises it — the card said Seedance while the segments
                # were frozen to whatever the composer had picked.
                models = await _pending_segment_models(db, production, pending, ctx.user_id)
                model_label = "/".join(models) if models else "默认模型"
                question = (
                    f"将提交 {len(pending)} 段 {model_label}，预计约 {estimated_seconds} 秒"
                    "并消耗视频额度。确认吗？"
                )
                options = [(f"确认，生成 {len(pending)} 段（消耗额度）", "最多允许本快照提交同样数量的新任务"), ("先不生成", "不调用收费的视频生成接口")]
            elif kind == "quality":
                if not segments or not all(row.status == "generated" and row.stt_verdict for row in segments):
                    return ToolResult(title="Quality evidence incomplete", output="Every active segment must be generated and transcribed first.")
                scope = quality_scope(production, segments)
                suspects = [row for row in segments if row.stt_verdict == "suspect"]
                metadata = {"suspect_segment_ids": [row.id for row in suspects]}
                question = (
                    f"逐段 STT 已完成：{len(segments) - len(suspects)} 段正常，{len(suspects)} 段疑似有差异。接受当前结果吗？"
                )
                options = [("接受当前全部段", "疑似段会记为人工豁免并可进入成片"), ("只重生疑似段", "不通过质检，先建立疑似段的新修订")]
            elif kind == "render":
                if production.quality_policy == "required" and not await _matching_approval(
                    db, production.id, "quality", quality_scope(production, segments)
                ):
                    return ToolResult(title="Quality approval required", output="Approve the current STT evidence before choosing the render form.")
                scope = render_scope(production, segments)
                question = "请选择最终成片形式。带字幕时只使用已确认的 STT 实际念词。"
                options = [("带字幕成片", "按每段实际转写文本烧录字幕"), ("无字幕成片", "只拼接画面和原音轨")]

            from question.question import Question, QuestionOption, QuestionRejectedError, ask

            try:
                answers = await ask(
                    session_id=ctx.session_id,
                    user_id=ctx.user_id or "default",
                    questions=[
                        Question(
                            question=question,
                            header={
                                "script": "讲稿确认",
                                "segments": "分段确认",
                                "spend": "生成费用确认",
                                "quality": "念词质检",
                                "render": "成片形式",
                            }[kind],
                            options=[QuestionOption(label=label, description=description) for label, description in options],
                            multiple=False,
                            custom=False,
                            detail=question_detail,
                        )
                    ],
                    tool={"messageID": ctx.message_id, "callID": ctx.part_id} if ctx.part_id else None,
                )
            except QuestionRejectedError:
                return ToolResult(title="Approval dismissed", output="The user dismissed the approval card; no approval was recorded.")
            answer = answers[0][0] if answers and answers[0] else ""
            positive = answer == options[0][0] or (kind == "render" and answer in {options[0][0], options[1][0]})
            decision = "rejected"
            max_calls = None
            if positive:
                decision = "approved"
                if kind == "spend":
                    max_calls = len(metadata["segment_ids"])
                if kind == "quality" and metadata.get("suspect_segment_ids"):
                    decision = "override"
                    await db.execute(
                        update(VideoSegment)
                        .where(VideoSegment.id.in_(metadata["suspect_segment_ids"]))
                        .values(review_status="override", review_note=answer, updated_at=now)
                    )
                if kind == "render":
                    production.subtitles = answer == options[0][0]
                    metadata["subtitles"] = production.subtitles
            approval = VideoApproval(
                id=ascending("approval"),
                production_id=production.id,
                user_id=ctx.user_id,
                session_id=ctx.session_id or None,
                kind=kind,
                scope_hash=scope,
                decision=decision,
                answer=answer or "未回答",
                max_calls=max_calls,
                used_calls=0,
                evidence_message_id=ctx.message_id or None,
                evidence_part_id=ctx.part_id or None,
                metadata_data=metadata,
                created_at=now,
            )
            db.add(approval)
            await db.flush()
            await _refresh_status(db, production, segments)
            if not positive:
                return ToolResult(
                    title="Approval not granted",
                    output=f"decision={decision}\nanswer={answer}\nNo downstream gate was opened.",
                    metadata={"production_id": production.id, "kind": kind, "decision": decision},
                )

        elif args.action == "set_segment_feedback":
            segments = await _active_segments(db, production.id)
            target = next((row for row in segments if row.id == args.segment_id), None)
            if not target:
                return ToolResult(
                    title="Active segment not found",
                    output="segment_id is not an active segment of this production; superseded revision ids do not accept feedback.",
                )
            if target.status != "generated" or not target.output_asset_id:
                return ToolResult(
                    title="Segment not reviewable",
                    output="Only generated segments with an output accept user feedback.",
                )
            target.review_status = (
                "user_approved" if args.feedback == "approved" else "user_rejected"
            )
            target.review_note = (args.feedback_note or "").strip() or None
            target.updated_at = now
            await _refresh_status(db, production, segments)

        elif args.action == "status":
            pass

    snapshot = await production_snapshot(resolved_id, ctx.user_id)
    return ToolResult(
        title="Video production status",
        output=_snapshot_output(snapshot or {}),
        metadata=snapshot or {},
    )


async def resolve_segment_model(db, production, segment, user_id: str) -> str | None:
    """The model this segment will generate with, resolved once and then frozen.

    Order: an explicit per-segment override, then the conversation's picked
    video model, then the deployment default (returned as None so the caller
    keeps using ``video_generation.model``).

    Resolution happens at submission and the answer is written back onto the
    segment, which is what makes switching safe: a segment already generating
    keeps the model it started with, and a new pick only reaches segments that
    have not been submitted yet. Without the write-back, a mid-flight switch
    would silently change what a retry or a reconciliation submits.
    """
    if segment.model:
        return segment.model
    from db.models.session import Session as SessionORM

    session_id = getattr(production, "session_id", None)
    if not session_id:
        return None
    session = await db.get(SessionORM, session_id)
    if not session or session.user_id != user_id:
        return None
    return session.video_model or None


async def _pending_segment_models(db, production, pending: list, user_id: str) -> list[str]:
    """Distinct models the pending segments will submit with, in plan order.

    Resolved the same way submission resolves them, so the approval card and
    the paid call can never disagree about what is being bought.
    """
    from core.config import get_config

    default = get_config().video_generation.model
    seen: list[str] = []
    for row in pending:
        model = await resolve_segment_model(db, production, row, user_id) or default
        if model not in seen:
            seen.append(model)
    return seen


async def prepare_segment_submission(ctx: ToolContext, production_id: str, segment_id: str) -> dict[str, Any]:
    """Return the immutable approved segment snapshot consumed by a paid submit."""
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        production = await _owned_production(db, production_id, ctx.user_id)
        if not production:
            raise RuntimeError("production is not owned by this user")
        segments = await _active_segments(db, production.id)
        segment = next((row for row in segments if row.id == segment_id), None)
        if not segment:
            raise RuntimeError("segment is not active in this production")
        expected_key = f"{production.id}:{segment.id}:generate"
        existing_job = (
            await db.get(VideoJob, segment.generation_job_id)
            if segment.generation_job_id
            else None
        )
        reconciling_existing = bool(
            existing_job
            and existing_job.user_id == ctx.user_id
            and existing_job.kind == "segment"
            and existing_job.production_id == production.id
            and existing_job.segment_id == segment.id
            and existing_job.idempotency_key == expected_key
        )
        if segment.status != "planned" and not reconciling_existing:
            raise RuntimeError(
                f"segment must be a newly planned revision before submission; current status is {segment.status}"
            )
        if not await _matching_approval(db, production.id, "script", production.script_hash):
            raise RuntimeError("current script is not approved")
        if not await _matching_approval(db, production.id, "segments", production.plan_hash):
            raise RuntimeError("current segment plan is not approved")
        spend = await _matching_approval(db, production.id, "spend", spend_scope(production, segments))
        if not spend:
            raise RuntimeError("current generation spend is not approved")
        if (
            not reconciling_existing
            and (spend.max_calls is None or spend.used_calls >= spend.max_calls)
        ):
            raise RuntimeError("approved generation call limit is exhausted")
        # Freeze the model onto the segment before the paid call, so a later
        # switch cannot retarget this submission or its reconciliation.
        chosen_model = await resolve_segment_model(db, production, segment, ctx.user_id)
        if chosen_model and segment.model != chosen_model:
            segment.model = chosen_model
            await db.commit()
        return {
            "production_id": production.id,
            "segment_id": segment.id,
            "prompt": segment.prompt,
            "model": chosen_model,
            "character_reference_asset": production.character_asset_id,
            "character_reference_type": production.character_reference_type,
            "character_identity_id": production.character_identity_id,
            "input_assets": list(segment.input_asset_ids or []),
            "resolution": production.resolution,
            "ratio": production.ratio,
            "duration": -1,
            "generate_audio": True,
            "watermark": False,
            "content_hash": segment.content_hash,
            "plan_hash": production.plan_hash,
            "spend_approval_id": spend.id,
            "reconciling_existing": reconciling_existing,
        }


async def consume_spend_approval(approval_id: str) -> None:
    from db.base import get_db_session
    from db.models.video_production import VideoApproval

    async with get_db_session() as db:
        result = await db.execute(
            update(VideoApproval)
            .where(
                VideoApproval.id == approval_id,
                VideoApproval.decision.in_(["approved", "override"]),
                VideoApproval.used_calls < VideoApproval.max_calls,
            )
            .values(used_calls=VideoApproval.used_calls + 1)
        )
        if result.rowcount != 1:
            raise RuntimeError("approved generation call limit was consumed concurrently")


async def mark_segment_job(
    segment_id: str,
    job_id: str,
    *,
    user_id: str,
    status: str,
    output_asset_id: str | None = None,
) -> None:
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_production import VideoProduction, VideoSegment

    values: dict[str, Any] = {"generation_job_id": job_id, "updated_at": datetime.now(timezone.utc)}
    if status == "completed" and output_asset_id:
        values.update(
            {
                "status": "generated",
                "output_asset_id": output_asset_id,
                "transcript_text": None,
                "transcript_data": {},
                "stt_similarity": None,
                "stt_verdict": None,
                "stt_notes": [],
                "stt_checked_at": None,
                "review_status": None,
                "review_note": None,
            }
        )
    elif status in {"failed", "cancelled"}:
        values["status"] = status
    else:
        values["status"] = "generating"
    async with get_db_session() as db:
        segment = (
            await db.execute(
                select(VideoSegment)
                .join(VideoProduction, VideoProduction.id == VideoSegment.production_id)
                .where(
                    VideoSegment.id == segment_id,
                    VideoProduction.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if not segment:
            raise RuntimeError("segment job target is not owned by this user")
        if status == "completed" and output_asset_id:
            owned_asset = (
                await db.execute(
                    select(FileAsset.id).where(
                        FileAsset.id == output_asset_id,
                        FileAsset.user_id == user_id,
                        FileAsset.status == "ready",
                        FileAsset.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if owned_asset is None:
                raise RuntimeError(
                    "completed segment output is not a ready asset owned by this user"
                )
        await db.execute(update(VideoSegment).where(VideoSegment.id == segment_id).values(**values))
        production = await db.get(VideoProduction, segment.production_id)
        if production is None:
            raise RuntimeError("segment job target production is missing")
        segments = await _active_segments(db, production.id)
        await _refresh_status(db, production, segments)


async def record_segment_transcript(
    segment_id: str,
    transcript_text: str,
    transcript_data: dict[str, Any],
    *,
    user_id: str,
    threshold: float,
) -> dict[str, Any]:
    from db.base import get_db_session
    from db.models.part import Part as PartORM
    from db.models.video_production import VideoProduction, VideoSegment

    updated_parts: list[dict[str, Any]] = []
    owner_id = ""
    async with get_db_session() as db:
        segment = (
            await db.execute(
                select(VideoSegment)
                .join(VideoProduction, VideoProduction.id == VideoSegment.production_id)
                .where(
                    VideoSegment.id == segment_id,
                    VideoProduction.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if not segment or not segment.is_active:
            raise RuntimeError(
                "transcription target is not an active segment owned by this user"
            )
        comparison = compare_transcript(segment.script_text, transcript_text, threshold)
        segment.transcript_text = transcript_text.strip()
        segment.transcript_data = transcript_data
        segment.stt_similarity = comparison["similarity"]
        segment.stt_verdict = comparison["verdict"]
        segment.stt_notes = comparison["notes"]
        segment.stt_checked_at = datetime.now(timezone.utc)
        segment.review_status = "accepted" if comparison["verdict"] == "ok" else None
        segment.review_note = None
        segment.updated_at = datetime.now(timezone.utc)
        production = await db.get(VideoProduction, segment.production_id)
        if production is None or production.user_id != user_id:
            raise RuntimeError("transcription target production is not owned by this user")
        owner_id = production.user_id
        segments = await _active_segments(db, production.id)
        await _refresh_status(db, production, segments)
        # The video file is attached before STT runs.  Refresh its semantic
        # envelope now so an older turn's segment card gains transcript and
        # QA state without depending on a later message being merged into
        # the same visual turn.
        if production.session_id and segment.output_asset_id:
            rows = (
                await db.execute(
                    select(PartORM).where(
                        PartORM.session_id == production.session_id,
                        PartORM.user_id == user_id,
                        PartORM.type == "file",
                    )
                )
            ).scalars().all()
            for row in rows:
                data = dict(row.data or {})
                if data.get("asset_id") != segment.output_asset_id:
                    continue
                relation = dict(data.get("relation") or {})
                metadata = dict(relation.get("metadata") or {})
                metadata.update(
                    {
                        "production_id": production.id,
                        "segment_id": segment.id,
                        "transcript": segment.transcript_text,
                        "stt_verdict": segment.stt_verdict,
                        "stt_similarity": segment.stt_similarity,
                    }
                )
                relation.update(
                    {
                        "source_part_id": relation.get("source_part_id"),
                        "group_id": f"video:{production.id}:segment:{segment.id}",
                        "role": "intermediate",
                        "kind": "video_segment",
                        "label": production.title,
                        "caption": segment.script_text,
                        "ordinal": segment.ordinal,
                        "revision": segment.revision,
                        "metadata": metadata,
                    }
                )
                data["relation"] = relation
                row.data = data
                updated_parts.append(data)

    if updated_parts and owner_id:
        from bus import bus
        from bus.events import PART_UPDATED

        for data in updated_parts:
            bus.publish(
                PART_UPDATED,
                {
                    "userId": owner_id,
                    "sessionId": data.get("session_id", ""),
                    "messageId": data.get("message_id", ""),
                    "part": {
                        key: value
                        for key, value in data.items()
                        if key not in ("session_id", "message_id", "state")
                    },
                },
            )
    return comparison


async def prepare_transcription(ctx: ToolContext, production_id: str, segment_id: str) -> dict[str, Any]:
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    async with get_db_session() as db:
        production = await _owned_production(db, production_id, ctx.user_id)
        if not production:
            raise RuntimeError("production is not owned by this user")
        segments = await _active_segments(db, production.id)
        segment = next((row for row in segments if row.id == segment_id), None)
        if not segment or segment.status != "generated" or not segment.output_asset_id:
            raise RuntimeError("segment must be generated before transcription")
        asset = await db.get(FileAsset, segment.output_asset_id)
        if not asset or asset.user_id != ctx.user_id or asset.status != "ready":
            raise RuntimeError("generated segment asset is not ready")
        return {"production": production, "segment": segment, "asset": asset}


async def prepare_render_submission(ctx: ToolContext, production_id: str) -> dict[str, Any]:
    from db.base import get_db_session

    async with get_db_session() as db:
        production = await _owned_production(db, production_id, ctx.user_id)
        if not production:
            raise RuntimeError("production is not owned by this user")
        segments = await _active_segments(db, production.id)
        if not segments or not all(row.status == "generated" and row.output_asset_id for row in segments):
            raise RuntimeError("every active segment must be generated before rendering")
        if production.quality_policy == "required" and not await _matching_approval(
            db, production.id, "quality", quality_scope(production, segments)
        ):
            raise RuntimeError("current STT evidence is not approved")
        render = await _matching_approval(db, production.id, "render", render_scope(production, segments))
        if not render:
            raise RuntimeError("current render form is not approved")
        subtitles = bool((render.metadata_data or {}).get("subtitles"))
        if subtitles and any(not (row.transcript_text or "").strip() for row in segments):
            raise RuntimeError("subtitled render requires accepted transcript text for every segment")
        return {
            "production_id": production.id,
            "segment_assets": [row.output_asset_id for row in segments],
            "captions": [row.transcript_text or "" for row in segments],
            "subtitles": subtitles,
            "channel_name": production.channel_name,
            "width": 720 if production.ratio == "9:16" else 1280,
            "height": 1280 if production.ratio == "9:16" else 720,
            "scope_hash": render.scope_hash,
        }


async def mark_render_complete(
    production_id: str,
    asset_id: str,
    *,
    user_id: str,
) -> None:
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_production import VideoProduction

    async with get_db_session() as db:
        production = (
            await db.execute(
                select(VideoProduction).where(
                    VideoProduction.id == production_id,
                    VideoProduction.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if not production:
            raise RuntimeError("render target is not owned by this user")
        owned_asset = (
            await db.execute(
                select(FileAsset.id).where(
                    FileAsset.id == asset_id,
                    FileAsset.user_id == user_id,
                    FileAsset.status == "ready",
                    FileAsset.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if owned_asset is None:
            raise RuntimeError(
                "completed render output is not a ready asset owned by this user"
            )
        production.render_asset_id = asset_id
        production.completed_at = datetime.now(timezone.utc)
        segments = await _active_segments(db, production.id)
        await _refresh_status(db, production, segments)


VIDEO_PROJECT_DESCRIPTION = """\
Create or resume a persistent spoken-video production, store an exact script and
linted segment plan, request hash-bound user approvals, create selective segment
revisions, or inspect recovery status. This is the control plane: video_generate,
video_transcribe, and video_render consume its approved snapshots. Load the
video-production skill before use."""


video_project_tool = define_tool(
    "video_project",
    description=VIDEO_PROJECT_DESCRIPTION,
    parameters=VideoProjectArgs,
    execute=execute_project,
    sandbox_required=False,
    parallel_safe=False,
    skill_only=True,
)
