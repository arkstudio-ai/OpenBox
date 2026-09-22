"""Explicit delegation policy, separate from legacy Task's deny list."""
from dataclasses import dataclass

from agent_catalog.schemas import READ_TOOLS
from team.errors import TeamError
from tool.workspace import CORE_TOOL_IDS

MEMBER_TOOLS = frozenset({"team_view", "team_message_send", "team_task_update", "team_wait"})
COORDINATOR_TOOLS = MEMBER_TOOLS | frozenset({"team_propose", "agent_catalog_search", "agent_catalog_get", "team_member_start", "team_task_create", "team_member_interrupt", "team_finish"})
COORDINATOR_READ_TOOLS = frozenset({"read", "glob", "grep", "view_image", "web_fetch", "skill_search", "question"})
T0 = frozenset(READ_TOOLS) | {"write", "edit", "multiedit", "apply_patch", "bash", "creator_context", "share_file"}
T1 = frozenset({"browser_mode", "computer", "hot_trends"})
T2 = frozenset({"image_gen", "video_generate", "video_transcribe", "video_compose", "video_analyze"})
FORBIDDEN = frozenset({"douyin_publish", "desktop_publish", "desktop_login", "desktop_takeover", "skill_manage", "agent_manage", "autopilot_run", "cron", "task", "plan_enter", "plan_exit", "question"}) | COORDINATOR_TOOLS


@dataclass(frozen=True)
class ToolDelegationPolicy:
    tool_id: str
    tier: str
    exclusive_group: str | None = None
    effect_adapter: str | None = None


def tool_policy(tool_id: str, config) -> ToolDelegationPolicy:
    if tool_id in FORBIDDEN:
        raise TeamError("TOOL_NOT_TEAM_READY", f"{tool_id} requires the user-facing root session.", status=422)
    if tool_id in T0:
        result = ToolDelegationPolicy(tool_id, "T0")
    elif tool_id in T1 and getattr(config, "team_tools_enabled", False):
        result = ToolDelegationPolicy(tool_id, "T1", "desktop")
    elif tool_id in T2 and getattr(config, "team_tools_enabled", False):
        result = ToolDelegationPolicy(tool_id, "T2", effect_adapter="external_effect" if tool_id == "image_gen" else "video_job")
    else:
        from tool.registry import get_tool
        tool = get_tool(tool_id)
        if (not getattr(config, "team_tools_enabled", False) or tool is None
                or tool.source != "custom" or tool.plane != "platform" or not tool.team_allowed):
            raise TeamError("TOOL_NOT_TEAM_READY", f"{tool_id} is not enabled for team delegation.", status=422)
        result = ToolDelegationPolicy(tool_id, "T1" if tool.team_exclusive_group else "T0", tool.team_exclusive_group)
    whitelist = getattr(config, "team_delegable_tools", [])
    if whitelist and tool_id not in whitelist:
        raise TeamError("TOOL_NOT_TEAM_READY", f"{tool_id} is outside this deployment's team allowlist.", status=422)
    return result


def delegated_plugins(config) -> list[str]:
    from tool.registry import list_tools
    result = []
    for tool in list_tools():
        if tool.source == "custom":
            try:
                tool_policy(tool.id, config)
            except TeamError:
                continue
            result.append(tool.id)
    return sorted(result)


def tool_presets(config) -> dict[str, list[str]]:
    def available(values):
        result = []
        for tool_id in sorted(values):
            try:
                tool_policy(tool_id, config)
            except TeamError:
                continue
            result.append(tool_id)
        return result
    return {"research": available(set(READ_TOOLS) | set(CORE_TOOL_IDS)), "files": available(T0),
            "desktop": available(T0 | T1), "media": available(T0 | T2)}
