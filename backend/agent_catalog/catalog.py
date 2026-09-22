"""Reusable catalog entries and the single lineup compiler used by UI/tools."""
from __future__ import annotations

from dataclasses import dataclass

from agent_catalog import repository
from agent_catalog.compiler import CompiledAgent, compile_agent
from agent_catalog.schemas import AgentSpec, MemberSpec, TeamPolicy, TeamSpec
from core.config import get_config
from team.errors import TeamError
from team.journal import Actor, digest
from team.policy import tool_policy


BUILTINS = {
    "builtin:team-coordinator": AgentSpec(name="团队协调者", description="分解目标、组织成员并验收交付", when_to_use="需要多个独立角色协作",
        instruction="Coordinate the team's goal, dependencies and review. Delegate execution, communicate real blockers, and finish only after all deliverables are accepted.", tool_allowlist=[]),
}


def builtin_entries() -> list[dict]:
    return [{"id": identifier, "name": spec.name, "source": "builtin", "status": "active", "readonly": True,
        "revision": 1, "current_version_id": "builtin-v1", "draft_version_id": None,
        "version": {"id": "builtin-v1", "version": 1, "spec": spec.model_dump(mode="json"), "content_digest": digest(spec.model_dump(mode="json")), "capability_summary": {}}}
        for identifier, spec in BUILTINS.items() if identifier != "builtin:team-coordinator"]


def model_entries(config=None) -> list[dict]:
    """Shared UI/tool choices use exactly the compiler's provider declaration."""
    from agent.model_resolve import configured_models
    from agent.subagent_composition import provider_capabilities, SubagentCompositionError
    config = config or get_config()
    names = {model.id: getattr(model, "name", None) or model.id for model in config.models}
    entries = []
    for model in configured_models(config):
        item = {"id": model, "name": names.get(model, model), "reasoning_variants": []}
        try:
            provider = provider_capabilities(model, config)
            item.update(reasoning_variants=sorted(provider.reasoning_variants), capabilities=sorted(provider.capabilities))
        except SubagentCompositionError:
            item["unavailable_for_team"] = True
        entries.append(item)
    return entries


async def resolve_agent(identifier: str, actor: Actor, version_id: str | None = None) -> tuple[AgentSpec, str | None]:
    if identifier in BUILTINS:
        if version_id and version_id != "builtin-v1":
            raise TeamError("AGENT_NOT_ACCESSIBLE", "This builtin version is unavailable.", status=404)
        return BUILTINS[identifier].model_copy(deep=True), "builtin-v1"
    row = await repository.get("agent", identifier, actor, version_id=version_id, active_only=True)
    return AgentSpec.model_validate(row["version"]["spec"]), row["version"]["id"]


@dataclass(frozen=True)
class PreparedLineup:
    spec: TeamSpec
    grant: dict
    coordinator: CompiledAgent
    members: list[tuple[MemberSpec, CompiledAgent, str | None]]

    def saved(self) -> dict:
        return {"spec": self.spec.model_dump(mode="json"), "grant": self.grant,
            "coordinator": self.coordinator.snapshot(), "members": [
                {"member": member.model_dump(mode="json"), "compiled": compiled.snapshot(), "version_id": version_id}
                for member, compiled, version_id in self.members]}

    def public(self) -> dict:
        return {"spec": self.spec.model_dump(mode="json"), "grant": self.grant,
            "coordinator": self.coordinator.summary, "members": [
                {"alias": member.alias, "name": compiled.spec.name, "description": compiled.spec.description,
                 "source": "library" if member.agent_ref else "coordinator", "responsibility": member.responsibility,
                 "definition_id": member.agent_ref, "version_id": version_id, **compiled.summary}
                for member, compiled, version_id in self.members],
            "concurrent_slots": min(len(self.members), self.spec.policy.max_concurrent_members), "issues": []}


async def prepare_lineup(spec: TeamSpec, actor: Actor, *, skills: list[dict] | None = None, scope=None) -> PreparedLineup:
    grant, coordinator, members = await _prepare(spec, actor, skills=skills, scope=scope, include_coordinator=True)
    assert coordinator is not None
    return PreparedLineup(spec, grant, coordinator, members)


async def prepare_member(member: MemberSpec, policy: TeamPolicy, actor: Actor, *, skills: list[dict] | None = None, scope=None) -> tuple[MemberSpec, CompiledAgent, str | None]:
    """Compile an addition without resolving or replacing the frozen coordinator."""
    if not member.enabled:
        raise TeamError("INVALID_MEMBER", "Only enabled members can be admitted.", status=422)
    _, _, members = await _prepare(TeamSpec(name="New member", preset_members=[member], policy=policy),
        actor, skills=skills, scope=scope, include_coordinator=False)
    return members[0]


async def _prepare(spec: TeamSpec, actor: Actor, *, skills: list[dict] | None, scope, include_coordinator: bool):
    from agent.model_resolve import configured_models
    config = get_config()
    policy = spec.policy
    allowed_models = policy.allowed_models or configured_models(config)
    for tool_id in policy.delegable_tools:
        tool_policy(tool_id, config)
    from permission.permission import EDIT_TOOLS
    permissions = {"edit" if tool_id in EDIT_TOOLS else tool_id for tool_id in policy.delegable_tools}
    if any(rule.permission not in permissions for rule in policy.permission_rules):
        raise TeamError("INVALID_PERMISSION_GRANT", "An operation scope must belong to an already delegated tool. File modifications use edit permission.", status=422)
    coordinator_spec = (await resolve_agent(spec.coordinator.agent_ref, actor))[0] if include_coordinator else None
    grant = {"version": 1, "paid_tools": {key: limit.model_dump(mode="json") for key, limit in policy.paid_tools.items()},
        "delegable_tools": policy.delegable_tools, "allowed_models": list(allowed_models),
        "allowed_agent_ids": policy.allowed_agent_ids, "member_selection": policy.member_selection,
        "allowed_skills": [ref.model_dump() for ref in policy.allowed_skills] if policy.allowed_skills is not None else None,
        "member_creation": policy.member_creation, "mcp_refs": [ref.model_dump() for ref in policy.mcp_refs],
        "permission_rules": [rule.model_dump(mode="json") for rule in policy.permission_rules]}
    resolved = []
    for member in spec.preset_members:
        if not member.enabled:
            continue
        version_id = None
        if member.inline:
            if not config.team_generated_members_enabled:
                raise TeamError("AUTHORITY_REVOKED", "This deployment has disabled temporary members. Select accessible saved agent_ref members instead.", status=422)
            if policy.member_creation == "disabled":
                raise TeamError("INVALID_TEAM_POLICY", "Inline members require policy.member_creation='run_scoped'. To fix this proposed roster, retain member_selection='explicit_only'. Or replace inline members with accessible saved agent_ref members.", status=422)
            definition = member.inline.model_copy(deep=True)
        else:
            if policy.allowed_agent_ids and member.agent_ref not in policy.allowed_agent_ids:
                raise TeamError("AGENT_NOT_ACCESSIBLE", "Member is outside the template's Agent allowlist.", status=422)
            definition, version_id = await resolve_agent(member.agent_ref, actor, member.version_id if member.version_policy == "pinned" else None)
        refs = [*definition.skill_refs, *member.additional_skills]
        unique = {(ref.name, ref.source): ref for ref in refs}
        if policy.allowed_skills is not None:
            if any(not any(allowed.name == ref.name and (allowed.source is None or ref.source is None or allowed.source == ref.source)
                           for allowed in policy.allowed_skills) for ref in unique.values()):
                raise TeamError("AGENT_NOT_ACCESSIBLE", "Member skills exceed the team's approved range.", status=422)
        definition.skill_refs = list(unique.values())
        if member.responsibility:
            combined = definition.instruction + "\n\nTeam responsibility: " + member.responsibility
            if len(combined) > 8192:
                raise TeamError("CAPABILITY_UNSUPPORTED", "Combined member instructions and responsibility exceed 8192 characters.", status=422)
            definition.instruction = combined
        resolved.append((member, definition, version_id))
    if skills is None:
        from skill.snapshot import freeze_specs
        skills = await freeze_specs([*([coordinator_spec] if coordinator_spec else []), *(definition for _, definition, _ in resolved)], actor, scope=scope)
    if policy.allowed_skills is not None:
        skills = [entry for entry in skills if any(ref.name == entry["name"] and (ref.source is None or ref.source == entry.get("source")) for ref in policy.allowed_skills)]
    coordinator = compile_agent(coordinator_spec, config=config, role="coordinator", model_override=spec.coordinator.model,
        allowed_models=allowed_models, skills=skills) if coordinator_spec else None
    members = [(member, compile_agent(definition, config=config, model_override=member.model_override,
        allowed_models=list(allowed_models), grant=grant, skills=skills), version_id) for member, definition, version_id in resolved]
    return grant, coordinator, members


def restore_lineup(saved: dict, *, amendment: bool = False) -> PreparedLineup:
    """Restore server-owned question data; never resolve mutable definitions."""
    from agent.subagent_authority import parse_subagent_authority
    from agent.subagent_composition import SubagentCompositionError, validate_composition_availability
    def compiled(data):
        authority = parse_subagent_authority(data["authority"])
        try:
            validate_composition_availability(authority.composition, get_config())
        except SubagentCompositionError as exc:
            raise TeamError("CAPABILITY_UNSUPPORTED", "Provider capabilities changed after this proposal. Request a fresh lineup confirmation.", status=422) from exc
        spec = AgentSpec.model_validate(data["spec"])
        return CompiledAgent(spec, authority, data["capability_summary"])
    return PreparedLineup(TeamSpec.model_validate(saved["spec"], context={"amendment": amendment}), saved["grant"], compiled(saved["coordinator"]),
        [(MemberSpec.model_validate(entry["member"]), compiled(entry["compiled"]), entry["version_id"]) for entry in saved["members"]])
