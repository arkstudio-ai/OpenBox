"""Read model for historical video productions.

The ``video_project`` tool and its approval state machine were retired when
video generation became a standalone primitive; the rows they wrote are kept
so old conversations still render, and this module is what reads them. It is
query-only apart from ``_refresh_status``, which recomputes the derived status
of a production the frontend is looking at.

Extracted verbatim from the retired ``tool/video_workflow.py``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

# Preserve the renderer generation in historical idempotency keys. The worker
# is retired, but existing production snapshots still expose this value.
RENDER_PIPELINE_REVISION = "bossip-wrap-bottom-v5"


def content_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def render_idempotency_key(production_id: str, scope_hash: str) -> str:
    if not scope_hash:
        return ""
    return f"{production_id}:render:{scope_hash[:16]}:{RENDER_PIPELINE_REVISION}"


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
        return "spend_ok"
    if not all(row.status == "generated" and row.output_asset_id for row in segments):
        return "needs_segment_revision"
    # Transcription QA verifies that what was spoken matches the script. A
    # b-roll shot has no speech, so waiting for its verdict would park the
    # production forever — and forcing one through produces a meaningless
    # "suspect" at similarity 0.
    if not all(row.stt_verdict for row in segments if row.role != "broll"):
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


async def production_snapshot(production_id: str, user_id: str) -> dict[str, Any] | None:
    """Full production snapshot; the public name is imported by the HTTP API."""
    from db.base import get_db_session
    from db.models.video_production import VideoApproval

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
        matching_approvals = {
            kind: (
                await _matching_approval(db, production.id, kind, scope)
                if scope
                else None
            )
            for kind, scope in scopes.items()
        }
        approvals = {kind: bool(row) for kind, row in matching_approvals.items()}
        approval_rows = (
            await db.execute(
                select(VideoApproval)
                .where(VideoApproval.production_id == production.id)
                .order_by(VideoApproval.created_at.desc())
            )
        ).scalars().all()
        latest_approvals: dict[str, Any] = {}
        for row in approval_rows:
            latest_approvals.setdefault(row.kind, row)
        approval_details = {}
        for kind, scope in scopes.items():
            matched = matching_approvals[kind]
            evidence = matched or latest_approvals.get(kind)
            approval_details[kind] = {
                "current_scope_hash": scope,
                "approval_scope_hash": evidence.scope_hash if evidence else "",
                "decision": evidence.decision if evidence else None,
                # Hash identity is evidence, not a gate decision. A rejection
                # can refer to the exact current scope while still leaving the
                # corresponding value in ``approvals`` false.
                "matches_current_hash": bool(
                    scope and evidence and evidence.scope_hash == scope
                ),
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
            "plan_hash": production.plan_hash,
            "render_asset_id": production.render_asset_id,
            "render_idempotency_key": render_idempotency_key(
                production.id, scopes["render"]
            ),
            "approvals": approvals,
            "approval_details": approval_details,
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
