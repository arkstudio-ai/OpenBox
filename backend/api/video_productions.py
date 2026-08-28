"""Read surface + user-authoritative segment feedback for video productions.

The agent drives productions through the `video_project` tool; this router
gives the frontend a way to list productions, read the full snapshot, and
record per-segment feedback directly (the tool path is agent-relayed, this
one carries the user's own auth). Ownership per query: foreign rows are 404.
"""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from auth.middleware import get_current_user
from core.log import create_logger
from db.base import get_db_session
from db.models.video_production import VideoProduction

log = create_logger("api.video_productions")

router = APIRouter(
    prefix="/api/video-productions",
    tags=["video-productions"],
    dependencies=[Depends(get_current_user)],
)


class SegmentFeedbackBody(BaseModel):
    feedback: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


@router.get("")
async def list_productions(
    session_id: str | None = None,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    async with get_db_session() as db:
        stmt = select(VideoProduction).where(
            VideoProduction.user_id == current_user["user_id"]
        )
        if session_id:
            stmt = stmt.where(VideoProduction.session_id == session_id)
        if status:
            stmt = stmt.where(VideoProduction.status == status)
        stmt = stmt.order_by(VideoProduction.updated_at.desc()).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
        return {
            "productions": [
                {
                    "production_id": row.id,
                    "title": row.title,
                    "status": row.status,
                    "session_id": row.session_id,
                    "project_id": row.project_id,
                    "resolution": row.resolution,
                    "ratio": row.ratio,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ]
        }


@router.get("/{production_id}")
async def get_production(production_id: str, current_user: dict = Depends(get_current_user)):
    from tool.video_workflow import production_snapshot

    snapshot = await production_snapshot(production_id, current_user["user_id"])
    if snapshot is None:
        raise HTTPException(status_code=404, detail="production not found")
    return snapshot


@router.post("/{production_id}/segments/{segment_id}/feedback")
async def set_segment_feedback(
    production_id: str,
    segment_id: str,
    body: SegmentFeedbackBody,
    current_user: dict = Depends(get_current_user),
):
    from tool.video_workflow import _owned_production, _active_segments, _refresh_status

    if body.feedback == "rejected" and not (body.note or "").strip():
        raise HTTPException(status_code=422, detail="rejected feedback requires a note")
    async with get_db_session() as db:
        production = await _owned_production(db, production_id, current_user["user_id"])
        if not production:
            raise HTTPException(status_code=404, detail="production not found")
        segments = await _active_segments(db, production.id)
        target = next((row for row in segments if row.id == segment_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="active segment not found")
        if target.status != "generated" or not target.output_asset_id:
            raise HTTPException(
                status_code=409, detail="only generated segments accept feedback"
            )
        target.review_status = (
            "user_approved" if body.feedback == "approved" else "user_rejected"
        )
        target.review_note = (body.note or "").strip() or None
        target.updated_at = datetime.now(timezone.utc)
        status = await _refresh_status(db, production, segments)
        return {
            "production_id": production.id,
            "segment_id": target.id,
            "review_status": target.review_status,
            "review_note": target.review_note,
            "status": status,
        }
