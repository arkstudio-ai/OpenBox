"""Skill job REST surface (§12.1). Auth context owns the user; every
repository call is user-scoped — no endpoint accepts an owner parameter."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.middleware import get_current_user
from skill_runtime import repository as repo, service
from skill_runtime.embedded import notify_worker as _notify_local_worker
from skill_runtime.manifest import ManifestError
from skill_runtime.repository import IdempotencyConflict, InputNotAllowed, JobNotFound

router = APIRouter(prefix="/api/skill-jobs", tags=["SkillJobs"])


class StartJobRequest(BaseModel):
    skill: str
    operation: str
    input: dict = {}
    idempotency_key: str | None = None
    session_id: str | None = None
    project_id: str | None = None


@router.post("")
async def start_job(body: StartJobRequest, current_user: dict = Depends(get_current_user)):
    try:
        job, created = await service.start_job(
            user_id=current_user["user_id"],
            skill_key=body.skill,
            operation=body.operation,
            input_data=body.input,
            idempotency_key=body.idempotency_key or f"api:{uuid.uuid4().hex}",
            session_id=body.session_id,
            project_id=body.project_id,
        )
    except service.UnknownSkill:
        raise HTTPException(status_code=404, detail="unknown skill")
    except service.UnknownOperation as e:
        raise HTTPException(status_code=400, detail=str(e))
    except service.SkillDisabled as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ManifestError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    _notify_local_worker()
    return {"job": service.job_snapshot(job), "created": created}


@router.get("")
async def list_jobs(
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    jobs = await repo.list_jobs(
        current_user["user_id"],
        session_id=session_id,
        statuses=(status,) if status else None,
        limit=limit,
    )
    return {"jobs": [service.job_snapshot(j) for j in jobs]}


@router.get("/{job_id}")
async def get_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await repo.get_job(job_id, current_user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job": service.job_snapshot(job)}


@router.get("/{job_id}/events")
async def get_events(
    job_id: str, after_seq: int = 0, current_user: dict = Depends(get_current_user)
):
    job = await repo.get_job(job_id, current_user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    events = await repo.get_events(job_id, current_user["user_id"], after_seq=after_seq)
    return {
        "events": [
            {
                "seq": e.seq,
                "eventType": e.event_type,
                "payload": e.payload or {},
                "createdAt": service.iso_utc(e.created_at),
            }
            for e in events
        ],
        "lastSeq": job.last_event_seq,
    }


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, current_user: dict = Depends(get_current_user)):
    try:
        job = await repo.request_cancel(job_id, current_user["user_id"])
    except JobNotFound:
        raise HTTPException(status_code=404, detail="job not found")
    _notify_local_worker()
    return {"job": service.job_snapshot(job)}


class JobInputRequest(BaseModel):
    payload: dict = {}
    idempotency_key: str | None = None
    kind: str = "user_answer"


@router.post("/{job_id}/inputs")
async def add_input(
    job_id: str, body: JobInputRequest, current_user: dict = Depends(get_current_user)
):
    # The public surface only carries human answers; provider callbacks get
    # their own signed route when a push-capable provider lands (§12.1).
    if body.kind not in ("user_answer", "operator_resume"):
        raise HTTPException(status_code=400, detail="unsupported input kind")
    try:
        row, created = await repo.add_input(
            job_id,
            current_user["user_id"],
            kind=body.kind,
            payload=body.payload,
            idempotency_key=body.idempotency_key or f"api:{uuid.uuid4().hex}",
        )
    except JobNotFound:
        raise HTTPException(status_code=404, detail="job not found")
    except InputNotAllowed as e:
        raise HTTPException(status_code=409, detail=str(e))
    _notify_local_worker()
    return {"inputId": row.id, "created": created}


@router.get("/{job_id}/artifacts")
async def list_artifacts(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await repo.get_job(job_id, current_user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"artifacts": await repo.list_artifacts(job_id, current_user["user_id"])}
