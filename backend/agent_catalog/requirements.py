"""Complete explicitly selected Skill dependencies before permission checks."""
from pydantic import ValidationError

from agent_catalog.schemas import AgentSpec
from skill.dependencies import required_mcp
from team.errors import TeamError


def selected_skills(spec: AgentSpec, skills: list[dict]) -> list[dict]:
    selected = []
    # all_accessible is live discovery, not an implicit request to enable every
    # installed integration. Explicit bindings have a fixed dependency closure.
    refs = spec.skill_refs if spec.skill_mode == "selected" else []
    for ref in refs:
        entry = next((skill for skill in skills if skill["name"] == ref.name
            and (ref.source is None or skill.get("source") == ref.source)), None)
        if entry is None:
            raise TeamError("AGENT_NOT_ACCESSIBLE", f"Skill {ref.name!r} is not accessible in the selected scope.", status=422)
        selected.append(entry)
    return selected


def complete_requirements(spec: AgentSpec, skills: list[dict]) -> tuple[AgentSpec, set[str], set[str]]:
    from tool.workspace import CORE_TOOL_IDS
    entries = selected_skills(spec, skills)
    tools = set(CORE_TOOL_IDS)
    if entries or spec.skill_mode == "all_accessible":
        tools.update(("skill", "skill_search"))
    for entry in entries:
        tools.update(entry.get("allowed_tools") or [])
    servers = {server for entry in entries for server in required_mcp(entry)}
    refs = {ref.server: ref.model_dump() for ref in spec.mcp_refs}
    for server in sorted(servers):
        # requires-mcp declares a service dependency, not individual patterns.
        # The run's approved patterns are still intersected at execution time.
        refs[server] = {"server": server, "tools": ["*"]}
    try:
        normalized = AgentSpec.model_validate({**spec.model_dump(),
            "tool_allowlist": list(dict.fromkeys([*spec.tool_allowlist, *sorted(tools)])),
            "mcp_refs": list(refs.values())})
    except ValidationError as exc:
        raise TeamError("CAPABILITY_UNSUPPORTED", "Selected Skills exceed the Agent capability limits.", status=422) from exc
    return normalized, tools, servers
