"""Bounded, authenticated historical completion preview; never enqueue work."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.config import get_config
from memory.jobs import backfill_dry_run
from memory.policy import MemoryAccessDenied

router = APIRouter(prefix="/api/memory-backfill", tags=["memory-backfill"], dependencies=[Depends(get_workspace)])


class BackfillDryRunBody(BaseModel):
    # Required nullable field: users must explicitly choose personal or a
    # concrete project instead of accidentally previewing every project.
    project_id: str | None
    start_at: datetime
    end_at: datetime
    limit: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def bounded_window(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("Backfill dates must include a timezone")
        self.start_at = self.start_at.astimezone(timezone.utc)
        self.end_at = self.end_at.astimezone(timezone.utc)
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=31):
            raise ValueError("Backfill preview requires an increasing window of at most 31 days")
        return self


@router.post("/dry-run")
async def dry_run(body: BackfillDryRunBody, current_user: dict = Depends(get_current_user)):
    config = get_config().memory
    if not config.enabled("backfill", current_user["user_id"]):
        raise HTTPException(403, {"code": "MEMORY_BACKFILL_DISABLED", "message": "Historical preview is disabled for the current user"})
    try:
        rows = await backfill_dry_run(user_id=current_user["user_id"], workspace_id=current_user["workspace_id"],
            project_id=body.project_id, start_at=body.start_at, end_at=body.end_at, limit=body.limit)
    except MemoryAccessDenied as exc:
        raise HTTPException(404, "memory backfill scope not found") from exc
    return {"dry_run": True, "enqueued": 0, "model_calls": 0,
            "scope": {"workspace_id": current_user["workspace_id"], "project_id": body.project_id, "visibility": "PERSONAL"},
            "window": {"start_at": body.start_at.isoformat(), "end_at": body.end_at.isoformat(), "time_basis": "completion_recorded_at"},
            "limit": body.limit, "returned": len(rows), "limit_reached": len(rows) == body.limit,
            "cost_estimate": None, "completions": rows,
            "coverage": "durable_successful_completion_receipts_only"}
