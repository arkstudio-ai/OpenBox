"""Explicit reuse of admitted configurations, independent of source history."""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent_catalog import repository
from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import AgentSpec, CoordinatorSpec, MemberSpec, TeamPolicy, TeamSpec
from db.models.team import TeamDefinitionVersion, TeamEvent
from team.errors import TeamError
from team.journal import Actor, digest, owned_run, read_state, write_transaction
from team.state import TERMINAL


async def _admissions(db, run_id):
    rows = (await db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id,
        TeamEvent.kind == "team.member.admitted"))).scalars().all()
    return {row.entity_id: row.payload["data"] for row in rows}


async def _save_member(db, run, actor, key, admission, name, *, publish, for_template=False):
    from core.config import get_config
    material = {**admission["spec"], "name": name, "default_model": admission["model"]}
    suffix = "\n\nTeam responsibility: " + admission.get("responsibility", "")
    if for_template and admission.get("responsibility") and material["instruction"].endswith(suffix):
        material["instruction"] = material["instruction"][:-len(suffix)]
    spec = AgentSpec.model_validate(material)
    compiled = compile_agent(spec, config=get_config(), role="trial", skills=admission["capability_summary"].get("skills", []))
    return await repository.create_locked(db, "agent", actor, key, spec,
        capability_summary={**compiled.summary, "_compiled_trial": compiled.snapshot()}, source="team",
        provenance={"source_run_id": run.id, "source_member_id": admission["id"]}, publish=publish)


async def save_member(run_id: str, member_id: str, actor: Actor, key: str, name: str):
    try:
        async with write_transaction() as db:
            run = await owned_run(db, run_id, actor, lock=True)
            admission = (await _admissions(db, run_id)).get(member_id)
            if admission is None or admission["role"] == "coordinator":
                raise TeamError("TEAM_MEMBER_NOT_FOUND", "Choose an admitted worker to save.", status=404)
            return await _save_member(db, run, actor, key, admission, name, publish=False)
    except IntegrityError as exc:
        raise TeamError("DEFINITION_NAME_TAKEN", "An Agent already uses this name. Choose another name.") from exc


async def save_template(run_id: str, actor: Actor, key: str, name: str, member_ids: list[str], member_names: dict[str, str]):
    try:
        async with write_transaction() as db:
            run = await owned_run(db, run_id, actor, lock=True)
            if run.state not in TERMINAL:
                raise TeamError("TEAM_ACTIVE", "Finish or cancel this run before saving its final roster as a template.")
            state = await read_state(db, run)
            admissions = await _admissions(db, run_id)
            if not member_ids or len(set(member_ids)) != len(member_ids) or any(mid not in admissions or mid == run.root_session_id for mid in member_ids):
                raise TeamError("INVALID_MEMBERS", "Select at least one distinct worker from this run.", status=422)
            members = []
            for member_id in member_ids:
                admission = admissions[member_id]
                reference, version = admission.get("definition_id"), admission.get("version_id")
                if not reference:
                    saved = await _save_member(db, run, actor, "reuse-member:" + digest([key, member_id]), admission,
                        member_names.get(member_id) or (admission["name"][:29] + " · " + run.id[-6:]), publish=True, for_template=True)
                    reference, version = saved["id"], saved["version"]["id"]
                elif not reference.startswith("builtin:"):
                    definition = await repository.owned(db, "agent", reference, actor)
                    if definition.status != "active":
                        raise TeamError("AGENT_NOT_ACCESSIBLE", "A source Agent was archived. Save that member separately before reusing it.")
                members.append(MemberSpec(alias=admission["alias"], agent_ref=reference, version_id=version,
                    version_policy="pinned", model_override=admission["model"], responsibility=admission.get("responsibility", ""),
                    additional_skills=AgentSpec.model_validate(admission["spec"]).skill_refs))
            original = await db.get(TeamDefinitionVersion, run.template_version_id) if run.template_version_id else None
            configuration = (run.summary or {}).get("team_configuration") or (original.spec_json if original else {})
            coordinator_ref = configuration.get("coordinator", {}).get("agent_ref", "builtin:team-coordinator")
            policy = TeamPolicy.model_validate({**state["policy"], "member_selection": "explicit_only", "member_creation": "disabled",
                "allowed_agent_ids": [member.agent_ref for member in members]})
            spec = TeamSpec(name=name, description=run.goal[:1000], preset_members=members, policy=policy,
                coordinator=CoordinatorSpec(agent_ref=coordinator_ref, model=admissions[run.root_session_id]["model"]),
                goal_input_schema=configuration.get("goal_input_schema"), result_schema=configuration.get("result_schema"),
                acceptance_mode=configuration.get("acceptance_mode", "coordinator"), resource_refs=configuration.get("resource_refs", []))
            return await repository.create_locked(db, "team", actor, key, spec, publish=True, source="team",
                provenance={"source_run_id": run.id}, capability_summary={"member_count": len(members)})
    except IntegrityError as exc:
        raise TeamError("DEFINITION_NAME_TAKEN", "A member or template name is already in use. Choose distinct names; nothing was saved.") from exc
