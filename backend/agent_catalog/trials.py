"""Definition-version bindings for ordinary, interactive preview sessions."""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select

from agent_catalog import repository
from core.config import get_config
from db.base import get_db_session
from db.models.team import AgentDefinition, AgentDefinitionVersion
from team.errors import TeamError
from team.journal import Actor, digest, write_transaction

PREFIX = "definition:"


async def authority_for_version(version_id: str, actor: Actor):
    from agent.subagent_authority import parse_subagent_authority
    from agent.subagent_composition import parse_subagent_composition, validate_composition_availability
    from dataclasses import replace
    async with get_db_session() as db:
        row = (await db.execute(select(AgentDefinitionVersion).join(AgentDefinition).where(
            AgentDefinitionVersion.id == version_id,
            AgentDefinition.owner_user_id == actor.owner_user_id,
            AgentDefinition.workspace_id == actor.workspace_id))).scalar_one_or_none()
        if row is None:
            raise TeamError("AGENT_NOT_ACCESSIBLE", "The trial definition version is not accessible.", status=404)
        material = row.capability_summary.get("_compiled_trial")
        if material:
            authority = parse_subagent_authority(material["authority"])
            # Version ids do not exist until persistence; bind the same preset
            # to the stable name held in Session.agent without a global entry.
            composition = replace(authority.composition,
                agent_preset=replace(authority.composition.agent_preset, name=PREFIX + version_id), digest="")
            authority = replace(authority, composition=parse_subagent_composition(composition.to_json()))
        else:
            raise TeamError("TRIAL_VERSION_INVALID", "Save a valid Agent version before starting a trial; this version has no frozen configuration.")
        validate_composition_availability(authority.composition, get_config())
        return authority, deepcopy(row.spec_json), deepcopy(row.capability_summary)


async def create(definition_id: str, actor: Actor, key: str, *, version_id: str | None = None, project_id: str | None = None):
    from db.models.project import Project
    from db.models.session import Session
    from db.models.user import User
    from project.workspace import resolve_for_session
    from session.session import _new_session_record
    definition = await repository.get("agent", definition_id, actor, version_id=version_id)
    if definition["status"] == "archived":
        raise TeamError("AGENT_NOT_ACCESSIBLE", "Archived definitions cannot start a new trial.")
    selected = definition["version"]["id"]
    authority, _, summary = await authority_for_version(selected, actor)
    project_id = await resolve_for_session(project_id, actor.owner_user_id, actor.workspace_id)
    identifier = "trial_" + digest([actor.owner_user_id, actor.workspace_id, key])[:50]
    async with write_transaction() as db:
        await db.execute(select(User.id).where(User.id == actor.owner_user_id).with_for_update())
        existing = await db.get(Session, identifier)
        if existing is not None:
            if existing.agent != PREFIX + selected or existing.project_id != project_id:
                raise TeamError("IDEMPOTENCY_CONFLICT", "This key already started a different trial.")
        else:
            project = await db.get(Project, project_id)
            if project is None or project.is_deleted or project.user_id != actor.owner_user_id or project.workspace_id != actor.workspace_id:
                raise TeamError("TEAM_NOT_FOUND", "Project is not accessible.", status=404)
            row, _ = _new_session_record(model=authority.composition.model, agent=PREFIX + selected,
                variant=authority.composition.reasoning, title=definition["name"], parent_id=None,
                user_id=actor.owner_user_id, workspace_id=actor.workspace_id, project_id=project_id,
                kind="agent_trial", session_id=identifier)
            db.add(row)
            await db.flush()
    return {"session_id": identifier, "version_id": selected,
        "capability_summary": {key: value for key, value in summary.items() if not key.startswith("_")}}
