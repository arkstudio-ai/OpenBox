"""Authenticated explicit Wiki compilation/approval; GET endpoints stay read-only."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.config import get_config
from memory.policy import MemoryAccessDenied
from memory.wiki import service

router = APIRouter(prefix="/api/memory-wiki", tags=["memory-wiki"], dependencies=[Depends(get_workspace)])


class CompileBody(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    title: str = Field(min_length=1, max_length=160)
    project_id: str | None = None
    memory_ids: list[str] | None = Field(default=None, max_length=12)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    confirm_cost: bool = False


class CandidateBody(BaseModel):
    candidate_revision: int = Field(ge=1)
    candidate_hash: str = Field(min_length=64, max_length=64)
    expected_target_revision: int = Field(default=0, ge=0)
    expected_target_hash: str | None = Field(default=None, min_length=64, max_length=64)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


def _identity(user):
    return {"user_id": user["user_id"], "workspace_id": user.get("workspace_id")}


async def _call(operation):
    try:
        result = await operation
    except MemoryAccessDenied as exc:
        raise HTTPException(404, "wiki object or scope not found") from exc
    except service.WikiStateError as exc:
        status = 403 if exc.code == "wiki_disabled" else 409
        raise HTTPException(status, {"code": exc.code.upper(), "message": exc.code}) from exc
    if result is None:
        raise HTTPException(404, "wiki object not found")
    return result


@router.get("/capabilities")
async def capabilities(user: dict = Depends(get_current_user)):
    memory = get_config().memory
    return {"enabled": memory.enabled("wiki", user["user_id"]), "publication_requires_approval": True,
            "model": memory.extract_model or get_config().model, "estimated_cost": None,
            "max_memories": 12, "max_sources": 12, "max_source_characters": 16000}


@router.get("/pages")
async def pages(project_id: str | None = None, user: dict = Depends(get_current_user)):
    result = await _call(service.list_wiki(**_identity(user), project_id=project_id))
    return {"pages": result["pages"]}


@router.get("/candidates")
async def candidates(project_id: str | None = None, user: dict = Depends(get_current_user)):
    result = await _call(service.list_wiki(**_identity(user), project_id=project_id))
    return {"candidates": result["candidates"]}


@router.post("/compile")
async def compile_wiki(body: CompileBody, user: dict = Depends(get_current_user)):
    if not body.confirm_cost:
        raise HTTPException(422, {"code": "WIKI_COST_CONFIRMATION_REQUIRED", "message": "Explicit cost confirmation is required"})
    return await _call(service.schedule_compile(**_identity(user), project_id=body.project_id, slug=body.slug,
        title=body.title, memory_ids=body.memory_ids, request_id=body.request_id))


@router.get("/jobs/{job_id}")
async def job(job_id: str, user: dict = Depends(get_current_user)):
    return await _call(service.get_job(**_identity(user), job_id=job_id))


@router.post("/candidates/{candidate_id}/approve")
async def approve(candidate_id: str, body: CandidateBody, user: dict = Depends(get_current_user)):
    return await _call(service.approve_candidate(**_identity(user), candidate_id=candidate_id,
        candidate_revision=body.candidate_revision, approved_hash=body.candidate_hash,
        expected_target_revision=body.expected_target_revision, expected_target_hash=body.expected_target_hash,
        request_id=body.request_id))


@router.post("/candidates/{candidate_id}/reject")
async def reject(candidate_id: str, body: CandidateBody, user: dict = Depends(get_current_user)):
    return await _call(service.reject_candidate(**_identity(user), candidate_id=candidate_id,
        candidate_revision=body.candidate_revision, approved_hash=body.candidate_hash))
