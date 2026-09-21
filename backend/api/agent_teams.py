"""Workspace-scoped definition library and team read/control APIs."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select

from agent_catalog import repository
from agent_catalog.catalog import builtin_entries, prepare_lineup
from agent_catalog.validation import agent_summary
from agent_catalog.schemas import AgentSpec, Contract, TeamSpec
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.config import get_config
from db.base import get_db_session
from db.models.team import TeamRun
from team.errors import TeamError
from team.grants import GrantChange
from team.owner_messages import OwnerMessage
from team.journal import Actor, owned_run, read_state
from team import projection
from team.policy import tool_presets


class TeamRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def guarded(request: Request):
            try:
                return await handler(request)
            except TeamError as exc:
                return JSONResponse(status_code=exc.status, content={"detail": exc.to_dict()})
        return guarded


router = APIRouter(prefix="/api", tags=["agent-teams"], dependencies=[Depends(get_workspace)], route_class=TeamRoute)


def actor(user: dict = Depends(get_current_user)) -> Actor:
    return Actor(user["user_id"], user["workspace_id"])


def idempotency(value: str = Header(alias="Idempotency-Key", min_length=1, max_length=256)) -> str:
    return value


class AgentDraft(Contract):
    spec: AgentSpec


class GenerateDraft(Contract):
    prompt: str = Field(min_length=1, max_length=4000)
    spec: AgentSpec | None = None
    field: Literal["instruction", "when_to_use"] | None = None


class TeamDraft(Contract):
    spec: TeamSpec


class AgentChange(Contract):
    expected_revision: int = Field(ge=1)
    spec: AgentSpec | None = None


class TeamChange(Contract):
    expected_revision: int = Field(ge=1)
    spec: TeamSpec | None = None


class Revision(Contract):
    expected_revision: int = Field(ge=1)


class UndoAutoapproval(Revision):
    version_id: str = Field(min_length=1, max_length=64)


class Duplicate(Contract):
    name: str = Field(min_length=1, max_length=40)


class Trial(Contract):
    version_id: str | None = None
    project_id: str | None = None


@router.post("/agent-definitions/{definition_id}/test-runs", status_code=201)
async def trial(definition_id: str, body: Trial, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from agent_catalog.trials import create
    return await create(definition_id, owner, key, version_id=body.version_id, project_id=body.project_id)


@router.get("/agent-capabilities/mcp")
async def mcp_catalog(owner: Actor = Depends(actor)):
    from agent_catalog.mcp_catalog import catalog
    return await catalog(owner)


@router.get("/agent-capabilities/models")
async def model_catalog(owner: Actor = Depends(actor)):
    from agent_catalog.catalog import model_entries
    return {"models": model_entries()}


@router.get("/agent-definitions")
async def agents(search: str = "", status: str | None = None, cursor: str | None = None,
                 limit: int = Query(default=50, ge=1, le=100), owner: Actor = Depends(actor)):
    rows = await repository.list_definitions("agent", owner, search=search, status=status, cursor=cursor, limit=limit)
    builtin = [entry for entry in builtin_entries() if not search or search.casefold() in entry["name"].casefold()]
    from team.policy import delegated_plugins
    return {**rows, "builtin": builtin if not cursor else [], "tool_presets": tool_presets(get_config()),
            "plugin_tools": delegated_plugins(get_config())}


@router.post("/agent-definitions", status_code=201)
async def create_agent(body: AgentDraft, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    return await repository.create("agent", owner, key, body.spec, capability_summary=await agent_summary(body.spec, owner))


@router.post("/agent-definitions/{definition_id}/undo-autoapproval")
async def undo_autoapproval(definition_id: str, body: UndoAutoapproval, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from agent_catalog.chat_creation import undo_autoapproval as undo
    return await undo(owner, definition_id, body.version_id, body.expected_revision, key)


@router.post("/agent-definitions/draft")
async def generate_draft(body: GenerateDraft, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from agent_catalog.generation import generate
    spec = await generate(owner, key, body.prompt, spec=body.spec, field=body.field)
    return {"spec": spec.model_dump(mode="json"), "capability_summary": await agent_summary(spec, owner)}


@router.get("/agent-definitions/{definition_id}")
async def get_agent(definition_id: str, version_id: str | None = None, owner: Actor = Depends(actor)):
    builtin = next((entry for entry in builtin_entries() if entry["id"] == definition_id), None)
    return builtin or await repository.get("agent", definition_id, owner, version_id=version_id)


@router.get("/agent-definitions/{definition_id}/versions")
async def agent_versions(definition_id: str, before: int | None = None, owner: Actor = Depends(actor)):
    return await repository.versions("agent", definition_id, owner, before=before)


@router.put("/agent-definitions/{definition_id}/draft-version")
async def save_agent_draft(definition_id: str, body: AgentChange, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    return await repository.mutate("agent", definition_id, owner, key, "save_draft", body.expected_revision,
        spec=body.spec, capability_summary=await agent_summary(body.spec, owner) if body.spec else None)


@router.post("/agent-definitions/{definition_id}/activate")
@router.post("/agent-definitions/{definition_id}/versions")
async def publish_agent(definition_id: str, body: AgentChange, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    spec = body.spec or AgentSpec.model_validate((await repository.get("agent", definition_id, owner))["version"]["spec"])
    summary = await agent_summary(spec, owner, strict=True)
    return await repository.mutate("agent", definition_id, owner, key, "publish", body.expected_revision,
        spec=body.spec, capability_summary=summary)


@router.post("/agent-definitions/{definition_id}/archive")
async def archive_agent(definition_id: str, body: Revision, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    return await repository.mutate("agent", definition_id, owner, key, "archive", body.expected_revision)


@router.post("/agent-definitions/{definition_id}/duplicate", status_code=201)
async def duplicate_agent(definition_id: str, body: Duplicate, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    original = await get_agent(definition_id, owner=owner)
    spec = AgentSpec.model_validate({**original["version"]["spec"], "name": body.name})
    return await repository.create("agent", owner, key, spec, capability_summary=await agent_summary(spec, owner), provenance={"copied_from": definition_id})


@router.get("/team-definitions")
async def teams(search: str = "", status: str | None = None, cursor: str | None = None,
                limit: int = Query(default=50, ge=1, le=100), owner: Actor = Depends(actor)):
    return await repository.list_definitions("team", owner, search=search, status=status, cursor=cursor, limit=limit)


@router.post("/team-definitions", status_code=201)
async def create_team(body: TeamDraft, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    prepared = await prepare_lineup(body.spec, owner)
    return await repository.create("team", owner, key, body.spec, capability_summary=prepared.public())


@router.get("/team-definitions/{definition_id}")
async def get_team(definition_id: str, version_id: str | None = None, owner: Actor = Depends(actor)):
    return await repository.get("team", definition_id, owner, version_id=version_id)


@router.get("/team-definitions/{definition_id}/versions")
async def team_versions(definition_id: str, before: int | None = None, owner: Actor = Depends(actor)):
    return await repository.versions("team", definition_id, owner, before=before)


@router.put("/team-definitions/{definition_id}/draft-version")
async def save_team_draft(definition_id: str, body: TeamChange, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    prepared = await prepare_lineup(body.spec, owner) if body.spec else None
    return await repository.mutate("team", definition_id, owner, key, "save_draft", body.expected_revision, spec=body.spec,
        capability_summary=prepared.public() if prepared else None)


@router.post("/team-definitions/{definition_id}/versions")
async def publish_team(definition_id: str, body: TeamChange, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    spec = body.spec or TeamSpec.model_validate((await repository.get("team", definition_id, owner))["version"]["spec"])
    prepared = await prepare_lineup(spec, owner)
    return await repository.mutate("team", definition_id, owner, key, "publish", body.expected_revision, spec=body.spec, capability_summary=prepared.public())


@router.post("/team-definitions/{definition_id}/archive")
async def archive_team(definition_id: str, body: Revision, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    return await repository.mutate("team", definition_id, owner, key, "archive", body.expected_revision)


@router.post("/team-definitions/{definition_id}/duplicate", status_code=201)
async def duplicate_team(definition_id: str, body: Duplicate, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    original = await repository.get("team", definition_id, owner)
    spec = TeamSpec.model_validate({**original["version"]["spec"], "name": body.name})
    prepared = await prepare_lineup(spec, owner)
    return await repository.create("team", owner, key, spec, capability_summary=prepared.public(), provenance={"copied_from": definition_id})


@router.post("/team-runs/preview")
async def preview(body: TeamDraft, owner: Actor = Depends(actor)):
    return (await prepare_lineup(body.spec, owner)).public()


@router.get("/team-runs")
async def runs(status: str | None = None, project_id: str | None = None, template_id: str | None = None,
               session_id: str | None = None, cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100), owner: Actor = Depends(actor)):
    from team.history import list_runs
    return await list_runs(owner, status=status, project_id=project_id, template_id=template_id,
        session_id=session_id, cursor=cursor, limit=limit)


@router.get("/team-runs/{run_id}")
async def get_run(run_id: str, owner: Actor = Depends(actor)):
    async with get_db_session() as db:
        run = await owned_run(db, run_id, owner)
        result = projection.public_state(await read_state(db, run))
        result["run"].update(template_id=run.template_id, template_version_id=run.template_version_id)
        return result


@router.get("/team-runs/{run_id}/usage")
async def usage(run_id: str, owner: Actor = Depends(actor)):
    from team.history import usage as read_usage
    return await read_usage(run_id, owner)


@router.get("/team-runs/{run_id}/diff")
async def workspace_diff(run_id: str, full: bool = False, owner: Actor = Depends(actor)):
    from team.workspace_snapshots import diff
    return await diff(run_id, owner, full=full)


@router.get("/team-runs/{run_id}/events")
async def events(run_id: str, after_seq: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=500), owner: Actor = Depends(actor)):
    return await projection.events(run_id, owner, after_seq=after_seq, limit=limit)


@router.get("/team-runs/{run_id}/{collection}")
async def details(run_id: str, collection: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                  member: str | None = None, between: str | None = None, task_id: str | None = Query(default=None, max_length=64), owner: Actor = Depends(actor)):
    pair = between.split(",") if between else None
    if pair is not None and len(pair) != 2:
        raise TeamError("INVALID_MESSAGE_FILTER", "between requires two comma-separated member IDs.", status=422)
    return await projection.collection(run_id, owner, collection, offset=offset, limit=limit, member=member, between=tuple(pair) if pair else None, task_id=task_id)


class Control(Revision):
    reason: str = Field(default="", max_length=1000)


@router.post("/team-runs/{run_id}/messages", status_code=202)
async def owner_message(run_id: str, body: OwnerMessage, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from team.owner_messages import send
    return await send(run_id, owner, key, body)


@router.post("/team-runs/{run_id}/grant")
async def update_grant(run_id: str, body: GrantChange, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from team.grants import update
    return await update(run_id, owner, key, body)


class SaveTemplate(Contract):
    name: str = Field(min_length=1, max_length=80)
    member_ids: list[str] = Field(min_length=1, max_length=31)
    member_names: dict[str, Annotated[str, Field(min_length=1, max_length=40)]] = Field(default_factory=dict, max_length=31)


@router.post("/team-runs/{run_id}/members/{member_id}/save-definition", status_code=201)
async def save_member(run_id: str, member_id: str, body: Duplicate, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from team.templates import save_member as save
    return await save(run_id, member_id, owner, key, body.name)


@router.post("/team-runs/{run_id}/save-as-template", status_code=201)
async def save_template(run_id: str, body: SaveTemplate, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from team.templates import save_template as save
    return await save(run_id, owner, key, body.name, body.member_ids, body.member_names)


@router.post("/team-runs/{run_id}/pause", status_code=202)
async def pause(run_id: str, body: Control, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from team.service import control
    return await control(run_id, owner, key, "pause", body.expected_revision, body.reason)


@router.post("/team-runs/{run_id}/resume", status_code=202)
async def resume(run_id: str, body: Control, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from team.service import control
    return await control(run_id, owner, key, "resume", body.expected_revision, body.reason)


@router.post("/team-runs/{run_id}/cancel", status_code=202)
async def cancel(run_id: str, body: Control, owner: Actor = Depends(actor), key: str = Depends(idempotency)):
    from team.service import control
    return await control(run_id, owner, key, "cancel", body.expected_revision, body.reason)
