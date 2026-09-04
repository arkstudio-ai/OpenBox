"""Cron job management API."""
from fastapi import APIRouter, Depends, HTTPException
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from cron.types import CronJobCreate, CronJobUpdate
from cron.service import cron_service

router = APIRouter(
    prefix="/api/cron",
    tags=["cron"],
    dependencies=[Depends(get_workspace)],
)


@router.get("/status")
async def get_cron_status(current_user: dict = Depends(get_current_user)):
    """Get cron scheduler status (liveness for monitoring; requires auth)."""
    return await cron_service.status()


@router.get("/jobs")
async def list_cron_jobs(
    session_id: str | None = None,
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    """List cron jobs. Optionally filter by project or notify session."""
    user_id = current_user["user_id"]
    return await cron_service.list_jobs(
        user_id, session_id=session_id, project_id=project_id,
        workspace_id=current_user["workspace_id"]
    )


@router.post("/jobs")
async def create_cron_job(
    body: CronJobCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a new cron job."""
    user_id = current_user["user_id"]
    try:
        return await cron_service.add(
            user_id, body, workspace_id=current_user["workspace_id"]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/jobs/{job_id}")
async def update_cron_job(
    job_id: str,
    body: CronJobUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update a cron job."""
    user_id = current_user["user_id"]
    try:
        return await cron_service.update(
            job_id, user_id, body, workspace_id=current_user["workspace_id"]
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/jobs/{job_id}")
async def delete_cron_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a cron job."""
    user_id = current_user["user_id"]
    try:
        return await cron_service.remove(
            job_id, user_id, workspace_id=current_user["workspace_id"]
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/jobs/{job_id}/run")
async def run_cron_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger a cron job."""
    user_id = current_user["user_id"]
    try:
        return await cron_service.run(
            job_id, user_id, workspace_id=current_user["workspace_id"]
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/jobs/pause-all")
async def pause_all_cron_jobs(
    session_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Pause (disable) all cron jobs. Optionally filter by session."""
    user_id = current_user["user_id"]
    paused = await cron_service.pause_all(
        user_id, session_id=session_id, workspace_id=current_user["workspace_id"]
    )
    return {"ok": True, "paused": paused}


@router.post("/jobs/resume-all")
async def resume_all_cron_jobs(
    session_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Resume (enable) all cron jobs. Optionally filter by session."""
    user_id = current_user["user_id"]
    resumed = await cron_service.resume_all(
        user_id, session_id=session_id, workspace_id=current_user["workspace_id"]
    )
    return {"ok": True, "resumed": resumed}


@router.get("/jobs/{job_id}")
async def get_cron_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a specific cron job."""
    user_id = current_user["user_id"]
    job = await cron_service.get_job(
        job_id, user_id, workspace_id=current_user["workspace_id"]
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs/{job_id}/runs")
async def list_cron_runs(
    job_id: str,
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
):
    """Get execution history for a cron job."""
    user_id = current_user["user_id"]
    return await cron_service.list_runs(
        job_id, user_id, limit=limit, workspace_id=current_user["workspace_id"]
    )
