"""Skill job REST surface (§12.1). Auth context owns the user; every
repository call is user-scoped — no endpoint accepts an owner parameter."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.middleware import get_current_user, require_admin
from core.log import create_logger
from skill_runtime import repository as repo, service
from skill_runtime.embedded import notify_worker as _notify_local_worker
from skill_runtime.manifest import ManifestError
from skill_runtime.repository import IdempotencyConflict, InputNotAllowed, JobNotFound

router = APIRouter(prefix="/api/skill-jobs", tags=["SkillJobs"])
log = create_logger("api.skill_jobs")


class StartJobRequest(BaseModel):
    skill: str
    operation: str
    input: dict = Field(default_factory=dict)
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
    except service.InvalidScope as e:
        raise HTTPException(status_code=404, detail=str(e))
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
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    kind: str = "user_answer"


@router.post("/{job_id}/inputs")
async def add_input(
    job_id: str, body: JobInputRequest, current_user: dict = Depends(get_current_user)
):
    # The public surface only carries human answers; provider callbacks get
    # their own signed route when a push-capable provider lands (§12.1), and
    # operator recovery has a separate administrator-only route below.
    if body.kind == "operator_resume":
        raise HTTPException(
            status_code=403,
            detail="operator_resume requires the administrator operator endpoint",
        )
    if body.kind != "user_answer":
        raise HTTPException(status_code=400, detail="unsupported input kind")
    job = await repo.get_job(job_id, current_user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    input_schema = (job.progress_data or {}).get("input_schema") or {}
    if input_schema.get("x-operator-only") is True:
        raise HTTPException(
            status_code=403,
            detail="this job is waiting for platform operator review",
        )
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


class OperatorInputRequest(BaseModel):
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    # Required for cross-tenant operations. In single-user mode it may be
    # omitted and resolves to the administrator's own account.
    owner_user_id: str | None = None


@router.post("/{job_id}/operator-input")
async def add_operator_input(
    job_id: str,
    body: OperatorInputRequest,
    current_user: dict = Depends(require_admin),
):
    """Admit a privileged, audited recovery decision for a waiting job.

    Provider task handles live in a shared platform account and are therefore
    capabilities, not ordinary user answers. The administrator must name the
    owning tenant; the repository still performs its normal ``(id, user_id)``
    ownership predicate.
    """
    owner_user_id = body.owner_user_id or current_user["user_id"]
    try:
        row, created = await repo.add_input(
            job_id,
            owner_user_id,
            kind="operator_resume",
            payload=body.payload,
            idempotency_key=body.idempotency_key or f"operator:{uuid.uuid4().hex}",
            # The input row's user_id is the owning tenant. Preserve the
            # privileged actor separately so the durable ledger can answer
            # which administrator authorized this reconciliation after logs
            # have rotated.
            source_event_id=f"admin:{current_user['user_id']}",
        )
    except JobNotFound:
        raise HTTPException(status_code=404, detail="job not found for specified owner")
    except InputNotAllowed as e:
        raise HTTPException(status_code=409, detail=str(e))
    log.info(
        "Administrator %s submitted operator recovery for job %s owned by %s",
        current_user["user_id"],
        job_id,
        owner_user_id,
    )
    _notify_local_worker()
    return {"inputId": row.id, "created": created}


@router.get("/{job_id}/artifacts")
async def list_artifacts(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await repo.get_job(job_id, current_user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"artifacts": await repo.list_artifacts(job_id, current_user["user_id"])}
