"""Compile reusable definitions into the existing frozen Agent execution shape."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent.agent import SUBAGENT_ALL_CAPABILITIES
from agent.model_resolve import configured_models
from agent.subagent_authority import SubagentAuthority, AUTHORITY_SNAPSHOT_VERSION
from agent.subagent_composition import (
    FrozenAgentPreset, SubagentComposition, SubagentCompositionError,
    parse_subagent_composition, provider_capabilities,
)
from agent_catalog.schemas import AgentSpec
from permission.permission import Rule
from team.errors import TeamError
from team.journal import digest
from team.policy import COORDINATOR_READ_TOOLS, COORDINATOR_TOOLS, MEMBER_TOOLS, tool_policy
from team.prompts import COORDINATOR, MEMBER


@dataclass(frozen=True)
class CompiledAgent:
    spec: AgentSpec
    authority: SubagentAuthority
    summary: dict

    def snapshot(self) -> dict:
        return {"spec": self.spec.model_dump(mode="json"), "composition": self.authority.composition.to_json(),
                "authority": self.authority.to_json(), "capability_summary": self.summary,
                "config_digest": digest({"spec": self.spec.model_dump(mode="json"), "authority": self.authority.to_json()})}


def compile_agent(
    spec: AgentSpec,
    *,
    config: Any,
    role: Literal["member", "coordinator", "trial"] = "member",
    model_override: str | None = None,
    allowed_models: list[str] | None = None,
    grant: dict | None = None,
    agent_name: str | None = None,
    available_tool_ids: set[str] | None = None,
    skills: list[dict] | None = None,
) -> CompiledAgent:
    deployment = set(configured_models(config))
    allowed = deployment.intersection(allowed_models) if allowed_models is not None else deployment
    if spec.allowed_models:
        if not set(spec.allowed_models).issubset(deployment):
            raise TeamError("MODEL_NOT_ALLOWED", "The definition includes models unavailable in this deployment.", current={"allowed_models": sorted(deployment)}, status=422)
        allowed.intersection_update(spec.allowed_models)
    selected = model_override or spec.default_model or config.model
    if spec.model_locked and selected != spec.default_model:
        raise TeamError("MODEL_LOCKED", "This definition's model is locked.", current={"allowed_models": [spec.default_model]}, status=422)
    if selected not in allowed:
        raise TeamError("MODEL_NOT_ALLOWED", "The selected model is outside the definition, team or deployment range.", current={"allowed_models": sorted(allowed)}, status=422)
    try:
        provider = provider_capabilities(selected, config)
    except SubagentCompositionError as exc:
        raise TeamError("CAPABILITY_UNSUPPORTED", str(exc), status=422) from exc
    if spec.reasoning is not None and spec.reasoning not in provider.reasoning_variants:
        raise TeamError("CAPABILITY_UNSUPPORTED",
            f"Model {selected!r} does not support reasoning={spec.reasoning!r}. Omit reasoning to use its default, or choose an allowed variant.",
            current={"model": selected, "reasoning_variants": sorted(provider.reasoning_variants)}, status=422)
    from team.execution import output_limit
    output_limit(spec.model_dump(mode="json"), selected, spec.reasoning, None)

    if role == "coordinator":
        tool_ids = set(COORDINATOR_TOOLS | COORDINATOR_READ_TOOLS)
        protocol = COORDINATOR
        exclusive_group = None
    else:
        tool_ids = set(spec.tool_allowlist)
        policies = [tool_policy(tool_id, config) for tool_id in tool_ids]
        categories = set(spec.execution_policy.tool_categories)
        outside = [policy.tool_id for policy in policies if categories and policy.tier not in categories]
        if outside or (categories and spec.mcp_refs and "MCP" not in categories):
            raise TeamError("TOOL_NOT_ALLOWED", "The definition's tool categories exclude some requested capabilities.",
                current={"tools": sorted(outside), "mcp_excluded": bool(spec.mcp_refs and "MCP" not in categories)}, status=422)
        if available_tool_ids is not None and not tool_ids.issubset(available_tool_ids):
            raise TeamError("TOOL_NOT_TEAM_READY", "Some requested tools are not installed.", current={"missing_tools": sorted(tool_ids - available_tool_ids)}, status=422)
        if grant is not None:
            denied = tool_ids - set(grant.get("delegable_tools", []))
            denied.update(p.tool_id for p in policies if p.tier == "T2" and p.tool_id not in grant.get("paid_tools", {}))
            if denied:
                raise TeamError("PERMISSION_REQUIRES_USER", "Requested tools exceed the run's approved capabilities.", current={"tools": sorted(denied)}, status=422)
        exclusive_group = "desktop" if any(p.exclusive_group == "desktop" for p in policies) else None
        from team.mcp import META_TOOLS, validate_refs
        validate_refs(spec, config, grant)
        if spec.mcp_refs:
            tool_ids.update(META_TOOLS)
        if role == "member":
            tool_ids.update(MEMBER_TOOLS)
        protocol = MEMBER if role == "member" else "You are a user-defined OpenBox Agent in an interactive trial. Follow platform permissions."

    skill_index = {(entry["name"], entry.get("source")): entry for entry in (skills or [])}
    selected_skills = []
    warnings = []
    from agent_catalog.schemas import SkillRef
    refs = spec.skill_refs if spec.skill_mode == "selected" else [SkillRef(name=entry["name"], source=entry.get("source")) for entry in (skills or [])]
    for ref in refs:
        entry = skill_index.get((ref.name, ref.source))
        if entry is None and ref.source is None:
            entry = next((value for key, value in skill_index.items() if key[0] == ref.name), None)
        if entry is None:
            raise TeamError("AGENT_NOT_ACCESSIBLE", f"Skill {ref.name!r} is not accessible in the selected scope.", status=422)
        selected_skills.append({key: entry.get(key) for key in ("name", "source", "description", "content_digest", "blob_key", "scope")})
        missing = set(entry.get("allowed_tools", [])) - set(spec.tool_allowlist)
        if missing:
            warnings.append({"code": "SKILL_TOOLS_MISSING", "skill": ref.name, "tools": sorted(missing)})

    permissions = [Rule(permission="*", pattern="*", action="ask")]
    for tool_id in sorted(tool_ids - {"bash", "write", "edit", "multiedit", "apply_patch", "computer", "browser_mode"}):
        permissions.append(Rule(permission=tool_id, pattern="*", action="allow"))
    preset = FrozenAgentPreset(
        name=agent_name or ("team" if role == "coordinator" else "team_member"),
        mode="all", tools=tuple(sorted(tool_ids)), max_steps=spec.execution_policy.max_steps,
        model=selected, temperature=0,
        prompt=protocol, permission=tuple(rule.model_dump() for rule in permissions),
        portable_opt_in=True, capabilities=SUBAGENT_ALL_CAPABILITIES,
    )
    composition = SubagentComposition(model=selected, reasoning=spec.reasoning, agent_preset=preset,
        persona=spec.instruction, tool_allowlist=frozenset(tool_ids), output_schema=spec.output_schema,
        provider=provider, seed_mode="fresh", digest="")
    try:
        composition = parse_subagent_composition(composition.to_json())
    except SubagentCompositionError as exc:
        raise TeamError("CAPABILITY_UNSUPPORTED", str(exc), status=422) from exc
    authority = SubagentAuthority(tool_ids=composition.tool_allowlist, permission_planes=(tuple(permissions),),
        guard_planes=(), composition=composition, snapshot_version=AUTHORITY_SNAPSHOT_VERSION)
    return CompiledAgent(spec, authority, {"model": selected, "allowed_models": sorted(allowed),
        "provider": provider.to_json(), "tool_ids": sorted(tool_ids), "skills": selected_skills,
        "tool_tiers": {tool: tool_policy(tool, config).tier for tool in spec.tool_allowlist} if role != "coordinator" else {},
        "skill_mode": spec.skill_mode, "mcp_refs": [ref.model_dump() for ref in spec.mcp_refs],
        "exclusive_group": exclusive_group, "warnings": warnings})
