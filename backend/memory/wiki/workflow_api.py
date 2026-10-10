import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field
from sqlalchemy import select

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.wiki_workflow import WikiArtifact
from memory.policy import resolve_access_scope
from memory.wiki import profiles, records, workflow_adaptation, workflows
from memory.wiki.api import _call, _identity, _no_store
from memory.wiki.organization_api import StrictBody
from wiki_compiler.profile_templates import KNOWLEDGE_REVIEW
from wiki_compiler.profiles import ProfileError

router = APIRouter(prefix="/api/memory-wiki", tags=["memory-wiki"],
                   dependencies=[Depends(get_workspace), Depends(_no_store)])


async def call(operation):
    try:
        return await _call(operation)
    except ProfileError as exc:
        raise HTTPException(422, {"code": exc.code.upper(), "message": exc.code, "path": exc.path}) from exc


class ProfileBody(StrictBody):
    project_id: str | None = None
    profile_id: str | None = None
    expected_revision: int = Field(ge=0)
    definition: dict


class StartBody(StrictBody):
    profile_id: str
    expected_profile_revision: int = Field(ge=1)
    workflow_id: str
    inputs: dict
    request_id: str = Field(min_length=1, max_length=128)


class ActionBody(StrictBody):
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=128)
    action: Literal["submit", "approve", "advance", "prepare", "fail", "resume", "cancel", "adapt", "retry_task"]
    payload: dict = Field(default_factory=dict)


class RecordBody(StrictBody):
    profile_revision: int = Field(ge=1)
    kind: Literal["page", "relation", "lifecycle", "artifact"]
    payload: dict


@router.get("/profile-templates")
async def templates(user: dict = Depends(get_current_user)):
    return {"templates": [KNOWLEDGE_REVIEW]}


@router.get("/profiles")
async def list_profiles(project_id: str | None = None, offset: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    return await call(profiles.list_profiles(**_identity(user), project_id=project_id, offset=offset))


@router.post("/profiles")
async def save_profile(body: ProfileBody, user: dict = Depends(get_current_user)):
    return await call(profiles.save(**_identity(user), **body.model_dump()))


@router.get("/profiles/{profile_id}")
async def profile(profile_id: str, user: dict = Depends(get_current_user)):
    return await call(profiles.detail(**_identity(user), profile_id=profile_id))


@router.get("/profiles/{profile_id}/records")
async def list_records(profile_id: str, offset: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    return await call(profiles.records(**_identity(user), profile_id=profile_id, offset=offset))


@router.post("/profiles/{profile_id}/records")
async def record_write(profile_id: str, body: RecordBody, user: dict = Depends(get_current_user)):
    return await call(records.mutate(**_identity(user), profile_id=profile_id, **body.model_dump()))


@router.get("/profiles/{profile_id}/statistics")
async def statistics(profile_id: str, user: dict = Depends(get_current_user)):
    return await call(profiles.statistics(**_identity(user), profile_id=profile_id))


@router.post("/workflows")
async def start(body: StartBody, user: dict = Depends(get_current_user)):
    return await call(workflows.start(**_identity(user), **body.model_dump()))


@router.get("/workflows")
async def list_runs(project_id: str | None = None, offset: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    return await call(workflows.list_runs(**_identity(user), project_id=project_id, offset=offset))


@router.get("/workflows/{run_id}")
async def run(run_id: str, event_offset: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    return await call(workflows.detail(**_identity(user), run_id=run_id, event_offset=event_offset))


@router.post("/workflows/{run_id}/actions")
async def action(run_id: str, body: ActionBody, user: dict = Depends(get_current_user)):
    return await call(workflows.act(**_identity(user), run_id=run_id, **body.model_dump()))


@router.get("/workflows/{run_id}/adaptation")
async def adaptation(run_id: str, user: dict = Depends(get_current_user)):
    return await call(workflow_adaptation.preview(**_identity(user), run_id=run_id))


@router.get("/workflows/{run_id}/history")
async def history(run_id: str, user: dict = Depends(get_current_user)):
    result = await call(workflows.detail(**_identity(user), run_id=run_id))
    events, offset = list(result.pop("events")), result.pop("events_next_offset")
    while offset is not None:
        batch = await call(workflows.detail(**_identity(user), run_id=run_id, event_offset=offset))
        # A concurrent mutation must not mix versions in a history export.
        if batch["revision"] != result["revision"]:
            raise HTTPException(409, {"code": "WIKI_WORKFLOW_CHANGED", "message": "workflow changed"})
        events.extend(batch["events"])
        offset = batch["events_next_offset"]
    result["events"] = events
    return Response(json.dumps(result, ensure_ascii=False, indent=2), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="wiki-workflow-history.json"', "Cache-Control": "no-store"})


async def read_artifact(user, artifact_id):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, **_identity(user), include_all_projects=True)
        row = await db.scalar(select(WikiArtifact).where(WikiArtifact.id == artifact_id, *scope.predicates(WikiArtifact)))
        if row is None:
            return None
        local = await resolve_access_scope(db, **_identity(user), project_id=row.project_id)
        profile = await profiles.get_profile(db, local, row.profile_id)
        await records.current_artifact(db, local, profile, row)
        return row


@router.get("/artifacts/{artifact_id}")
async def artifact(artifact_id: str, user: dict = Depends(get_current_user)):
    row = await call(read_artifact(user, artifact_id))
    suffix = "json" if row.media_type == "application/json" else "md" if row.media_type == "text/markdown" else "txt"
    return Response(row.body, media_type=row.media_type, headers={"Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="wiki-artifact.{suffix}"'})
