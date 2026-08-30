"""Deciding which tools a step may call.

Three things happen here that used to be interleaved in the loop body: the
agent's declared toolset is looked up, tools the sandbox offers are merged in,
and anything the permission rules deny is removed *before the model sees the
schema*.

That last step is the point. Denying a tool at call time still lets the model
propose it, burn a turn and get refused; removing it from the schema means the
option never exists. Mirrors opencode's PermissionNext.disabled() feeding
resolveTools().
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from core.log import create_logger
from permission.permission import Rule, disabled_tools
from tool.registry import get_tools_for_agent

log = create_logger("agent.tool_resolution")

CatalogueAvailability = Literal["available", "stale", "unavailable"]


@dataclass(frozen=True)
class ResolvedStepTools:
    """Resolved definitions plus the authority of the sandbox directory."""

    tools: dict
    catalogue_availability: CatalogueAvailability


class _CatalogueSnapshotSandbox:
    """Freeze all directory reads to one aggregate catalogue generation."""

    def __init__(self, sandbox, snapshot: dict):
        self._sandbox = sandbox
        self._snapshot = snapshot

    def __getattr__(self, name):
        return getattr(self._sandbox, name)

    async def list_skills(self) -> list[dict]:
        return deepcopy(self._snapshot.get("skills", []))

    async def list_mcp_tools(self) -> list[dict]:
        return deepcopy(self._snapshot.get("mcp_tools", []))

    async def list_mcp_resources(self) -> list[dict]:
        return deepcopy(self._snapshot.get("mcp_resources", []))


async def _catalogue_view(sandbox) -> tuple[object | None, CatalogueAvailability]:
    """Resolve one coherent sandbox view, retaining old-client compatibility."""
    if sandbox is None:
        return None, "available"
    get_state = getattr(sandbox, "get_catalogue_projection_state", None)
    if not callable(get_state):
        # A pre-projection client has no explicit outage signal. Materialize
        # all three legacy lists here so a swallowed error cannot masquerade as
        # an authoritative empty sandbox catalogue. The multi-request view is
        # necessarily stale/non-destructive because it has no atomic ETag.
        try:
            snapshot = {
                "skills": await sandbox.list_skills(),
                "mcp_tools": await sandbox.list_mcp_tools(),
                "mcp_resources": await sandbox.list_mcp_resources(),
            }
        except Exception as exc:
            log.debug(
                "Legacy sandbox catalogue unavailable error_type=%s",
                type(exc).__name__,
            )
            return None, "unavailable"
        if not all(isinstance(snapshot[key], list) for key in snapshot):
            return None, "unavailable"
        return _CatalogueSnapshotSandbox(sandbox, snapshot), "stale"
    try:
        state = await get_state()
    except Exception as exc:
        log.debug(
            "Sandbox catalogue status unavailable error_type=%s",
            type(exc).__name__,
        )
        return None, "unavailable"
    availability = getattr(state, "availability", "unavailable")
    snapshot = getattr(state, "snapshot", None)
    required_lists = ("skills", "mcp_tools", "mcp_resources")
    if (
        availability not in {"available", "stale"}
        or not isinstance(snapshot, dict)
        or not all(isinstance(snapshot.get(key), list) for key in required_lists)
    ):
        return None, "unavailable"
    return _CatalogueSnapshotSandbox(sandbox, snapshot), availability


def agent_ruleset(agent_def) -> list[Rule]:
    """Permission rules declared on the agent, as Rule objects.

    Entries that are not dicts are skipped rather than raising: an agent
    definition is user-editable config, and one malformed rule should not take
    the whole run down.
    """
    return [
        Rule(
            permission=r.get("permission", "*"),
            pattern=r.get("pattern", "*"),
            action=r.get("action", "ask"),
        )
        for r in (agent_def.permission or [])
        if isinstance(r, dict)
    ]


def strip_denied(tools: dict, config_rules: list, agent_def) -> dict:
    """Drop tools denied by config or agent rules. Pure; returns a new dict."""
    merged = list(config_rules) + agent_ruleset(agent_def)
    denied = set(disabled_tools(list(tools.keys()), merged))
    return {name: t for name, t in tools.items() if name not in denied}


async def merge_sandbox_tools(
    tools: dict,
    sandbox,
    ruleset: list | None = None,
    *,
    agent_id: str = "",
    catalogue_availability: CatalogueAvailability = "available",
) -> dict:
    """Add the MCP tools a sandbox exposes.

    Every failure here is downgraded to a debug log: a sandbox without MCP
    configured is the normal case, not an error, and a run with fewer tools is
    far better than a run that cannot start.
    """
    if not sandbox or catalogue_availability == "unavailable":
        return tools

    try:
        from tool.mcp_tool import create_mcp_tools, create_mcp_resource_tool

        mcp_tools = await create_mcp_tools(
            sandbox,
            ruleset,
            agent_id=agent_id,
        )
        meta_names = {"mcp_find_tool", "mcp_call_tool"}
        if meta_names <= mcp_tools.keys() and meta_names & tools.keys():
            # A partial meta pair is not useful and could route one half to an
            # unrelated platform tool. Preserve the platform namespace and
            # fail this MCP catalogue closed.
            log.error("MCP meta-tool namespace collides with an existing tool")
        else:
            for name, tool in mcp_tools.items():
                if name in tools:
                    log.error("MCP provider name collides with an existing tool: %s", name)
                    continue
                tools[name] = tool
        try:
            if await sandbox.list_mcp_resources():
                rt = create_mcp_resource_tool()
                if rt.id in tools:
                    log.error("MCP resource tool collides with an existing tool")
                else:
                    tools[rt.id] = rt
        except Exception as e:
            log.debug(
                "MCP resources not available error_type=%s",
                type(e).__name__,
            )
    except Exception as e:
        log.debug(
            "MCP tools not available error_type=%s",
            type(e).__name__,
        )

    return tools


async def attach_skill_listing(tools: dict, sandbox, ruleset: list | None = None) -> dict:
    """Advertise Skills and conditionally materialize their search companion.

    Separate from merge_sandbox_tools because skills are not a sandbox tool:
    they are discovered on the backend host as well as in the container. This
    is a structural split, not a bug fix — run_loop gets its sandbox from
    get_client, which returns a client or raises, so `sandbox` is never None
    here today and the old placement was unreachable rather than wrong.
    """
    search_is_eligible = "skill_search" in tools
    # The registry entry is only an AgentDef companion marker. Never expose its
    # empty static index; the per-step permission-filtered index replaces it
    # below only when the complete listing exceeds the hard cap.
    tools.pop("skill_search", None)
    if "skill" not in tools:
        return tools
    try:
        from tool.skill_tool import (
            SkillListingCompanionRequired,
            build_skill_tools_with_listing,
        )

        skill, search = await build_skill_tools_with_listing(
            sandbox,
            ruleset,
            enable_search=search_is_eligible,
        )
        tools["skill"] = skill
        if search is not None:
            tools["skill_search"] = search
    except SkillListingCompanionRequired:
        # The listing and its search index are an atomic capability. Keeping
        # ``skill`` here would either send an over-budget directory or hide a
        # permission-filtered tail behind a guessable loader. Remove both
        # definitions and fail this capability closed for the step.
        tools.pop("skill", None)
        tools.pop("skill_search", None)
        log.error(
            "Skill capability disabled: directory exceeds its hard cap "
            "without an eligible skill_search companion"
        )
    except Exception as e:
        log.debug(f"Failed to enrich skill tool: {e}")
    return tools


async def resolve_step_tools(
    agent_def,
    sandbox,
    config_rules: list,
    *,
    include_discovery: bool = True,
    return_catalogue_state: bool = False,
) -> dict | ResolvedStepTools:
    """The full set of tools a step may call, schema included."""
    tools = get_tools_for_agent(agent_def.tools)
    if not include_discovery:
        # The logical discovery slot is explicit in AgentDef but has no role
        # in legacy/shadow provider wire. Keeping it out preserves the eager
        # migration baseline byte-for-byte.
        tools.pop("capability_search", None)
    # An explicit empty tool allowlist is a hard boundary. Sandbox discovery
    # must not repopulate it with MCP/resource wrappers after the registry has
    # correctly resolved it to nothing.
    if not agent_def.tools:
        resolved = ResolvedStepTools(tools, "available")
        return resolved if return_catalogue_state else resolved.tools
    catalogue_sandbox, catalogue_availability = await _catalogue_view(sandbox)
    # The same rules that strip tools also decide which skills are worth listing.
    ruleset = list(config_rules) + agent_ruleset(agent_def)
    tools = await merge_sandbox_tools(
        tools,
        catalogue_sandbox,
        ruleset,
        agent_id=str(getattr(agent_def, "name", "") or ""),
        catalogue_availability=catalogue_availability,
    )
    # Resolve whole-tool denials before deciding whether the search companion
    # exists. Otherwise a denied `skill_search` could be stripped only after
    # the listing had already discarded names, breaking the atomic fallback.
    tools = strip_denied(tools, config_rules, agent_def)
    tools = await attach_skill_listing(tools, catalogue_sandbox, ruleset)
    resolved = ResolvedStepTools(tools, catalogue_availability)
    return resolved if return_catalogue_state else resolved.tools
