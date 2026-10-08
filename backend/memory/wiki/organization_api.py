"""Authenticated concept organization, review and explicit maintenance enrollment."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from memory.wiki import concepts, maintenance, organization
from memory.wiki.api import _call, _identity, _no_store

router = APIRouter(prefix="/api/memory-wiki", tags=["memory-wiki"],
                   dependencies=[Depends(get_workspace), Depends(_no_store)])
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrganizeBody(StrictBody):
    project_id: str | None = None
    input_hash: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=1, max_length=128)
    max_model_calls: int = Field(default=20, ge=1, le=4000)
    compile_pages: bool = True
    confirm_cost: bool = False


class RunActionBody(StrictBody):
    expected_revision: int = Field(ge=1)
    action: Literal["resume", "cancel"]
    max_model_calls: int | None = Field(default=None, ge=1, le=4000)
    confirm_cost: bool = False


class MaintenanceBody(StrictBody):
    project_id: str | None = None
    expected_revision: int = Field(ge=0)
    enabled: bool
    call_limit: int = Field(default=20, ge=1, le=4000)
    compile_pages: bool = True
    confirm_cost: bool = False


class ConceptEditBody(StrictBody):
    expected_revision: int = Field(ge=1)
    title: Title
    aliases: list[Title] = Field(default_factory=list, max_length=128)
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: str = Field(default="", max_length=800)


class ConceptMergeBody(StrictBody):
    source_revision: int = Field(ge=1)
    target_id: str = Field(min_length=1, max_length=64)
    target_revision: int = Field(ge=1)


class RelationBody(StrictBody):
    expected_revision: int = Field(ge=1)
    evidence_hash: str = Field(min_length=64, max_length=64)
    action: Literal["approve", "reject"]


@router.get("/concepts")
async def list_concepts(project_id: str | None = None, offset: int = Query(0, ge=0),
                        query: str = Query("", max_length=200), category: str | None = Query(None, max_length=80),
                        user: dict = Depends(get_current_user)):
    return await _call(organization.concepts(**_identity(user), project_id=project_id, offset=offset, query=query, category=category))


@router.post("/concepts/{concept_id}/edit")
async def edit_concept(concept_id: str, body: ConceptEditBody, user: dict = Depends(get_current_user)):
    return await _call(organization.edit_concept(**_identity(user), concept_id=concept_id, **body.model_dump()))


@router.post("/concepts/{concept_id}/merge")
async def merge_concept(concept_id: str, body: ConceptMergeBody, user: dict = Depends(get_current_user)):
    return await _call(concepts.merge(**_identity(user), source_id=concept_id, **body.model_dump()))


@router.get("/relations")
async def list_relations(project_id: str | None = None, offset: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    return await _call(concepts.relations(**_identity(user), project_id=project_id, offset=offset))


@router.post("/relations/{relation_id}/decision")
async def decide_relation(relation_id: str, body: RelationBody, user: dict = Depends(get_current_user)):
    return await _call(concepts.decide_relation(**_identity(user), relation_id=relation_id, **body.model_dump()))


@router.get("/organization/preview")
async def organization_preview(project_id: str | None = None, user: dict = Depends(get_current_user)):
    return await _call(organization.preview(**_identity(user), project_id=project_id))


@router.post("/organization/runs")
async def start_organization(body: OrganizeBody, user: dict = Depends(get_current_user)):
    if not body.confirm_cost:
        raise HTTPException(400, "explicit cost confirmation required")
    return await _call(organization.start(**_identity(user), **body.model_dump(exclude={"confirm_cost"})))


@router.get("/organization/runs")
async def list_runs(project_id: str | None = None, offset: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    return await _call(organization.runs(**_identity(user), project_id=project_id, offset=offset))


@router.get("/organization/runs/{run_id}")
async def read_run(run_id: str, user: dict = Depends(get_current_user)):
    return await _call(organization.runs(**_identity(user), run_id=run_id))


@router.post("/organization/runs/{run_id}/actions")
async def act_run(run_id: str, body: RunActionBody, user: dict = Depends(get_current_user)):
    if body.action == "resume" and not body.confirm_cost:
        raise HTTPException(400, "explicit cost confirmation required")
    return await _call(organization.act_run(**_identity(user), run_id=run_id, **body.model_dump(exclude={"confirm_cost"})))


@router.get("/maintenance")
async def read_maintenance(project_id: str | None = None, user: dict = Depends(get_current_user)):
    return await _call(maintenance.read(**_identity(user), project_id=project_id))


@router.put("/maintenance")
async def configure_maintenance(body: MaintenanceBody, user: dict = Depends(get_current_user)):
    if body.enabled and not body.confirm_cost:
        raise HTTPException(400, "explicit cost confirmation required")
    return await _call(maintenance.configure(**_identity(user), **body.model_dump(exclude={"confirm_cost"})))
