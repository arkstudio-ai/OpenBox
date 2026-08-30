"""Config folded over the built-in agents.

The same config entry retunes a built-in, hides it, removes it, or introduces
one of the user's own — matching opencode's `agent` block. Before this the
registry was a frozen dict and config could only reach a handful of run-time
fields, so an agent you defined simply did not exist.
"""
import pytest
from pydantic import BaseModel

import agent.agent as mod
from agent.tool_runtime import assemble_tool_runtime, effective_exposure_mode
from core.config import AgentOverride
from tool.tool import ToolInfo, ToolResult


class _Args(BaseModel):
    value: str = ""


async def _execute(_args, _ctx):
    return ToolResult(title="ok", output="ok")


def _runtime_tools() -> dict[str, ToolInfo]:
    return {
        tool_id: ToolInfo(
            id=tool_id,
            description=f"Use {tool_id}.",
            parameters=_Args,
            execute=_execute,
        )
        for tool_id in ("capability_search", "read")
    }


class Cfg:
    """Enough of the config object for the registry and permission rules."""
    def __init__(self, agent):
        self.agent = agent
        self.permission = {}
        self.default_agent = None


@pytest.fixture
def with_config(monkeypatch):
    def apply(agents: dict) -> Cfg:
        # One instance, so a test can set default_agent on what it gets back.
        cfg = Cfg(agents)
        monkeypatch.setattr(mod, "get_config", lambda: cfg, raising=False)
        import core.config
        monkeypatch.setattr(core.config, "get_config", lambda: cfg)
        return cfg
    return apply


# ── defining one of your own ──

def test_a_config_agent_is_created(with_config):
    with_config({"reviewer": AgentOverride(description="Reviews code")})
    assert mod.get_agent("reviewer").description == "Reviews code"


def test_inherited_discovery_slot_does_not_opt_custom_agent_out_of_shadow(with_config):
    with_config({"reviewer": AgentOverride()})
    reviewer = mod.get_agent("reviewer")

    assert "capability_search" in reviewer.tools
    assert reviewer.portable_opt_in is False
    mode = effective_exposure_mode(
        "portable",
        reviewer.name,
        portable_opt_in=reviewer.portable_opt_in,
    )
    assert mode == "shadow"
    runtime = assemble_tool_runtime(
        _runtime_tools(), mode=mode, agent_name=reviewer.name
    )
    assert set(runtime.provider_tools) == {"read"}
    assert runtime.candidate_plan is not None
    assert runtime.candidate_plan.direct_ids == ("capability_search",)
    assert runtime.candidate_plan.discovery_ids == ("read",)


@pytest.mark.parametrize("requested_mode", ["portable", "native_auto"])
def test_explicit_custom_discovery_whitelist_opts_into_deferred_runtime(
    with_config,
    requested_mode,
):
    with_config({
        "reviewer": AgentOverride(tools=["capability_search", "read"]),
    })
    reviewer = mod.get_agent("reviewer")

    assert reviewer.portable_opt_in is True
    mode = effective_exposure_mode(
        requested_mode,
        reviewer.name,
        portable_opt_in=reviewer.portable_opt_in,
    )
    assert mode == requested_mode
    runtime = assemble_tool_runtime(
        _runtime_tools(), mode=mode, agent_name=reviewer.name
    )
    assert set(runtime.provider_tools) == {"capability_search"}
    assert runtime.provider_plan.direct_ids == ("capability_search",)
    assert runtime.provider_plan.discovery_ids == ("read",)
    assert runtime.candidate_plan is None


def test_it_defaults_to_being_usable_both_ways(with_config):
    with_config({"reviewer": AgentOverride()})
    assert mod.get_agent("reviewer").mode == "all"
    names_pickable = {a.name for a in mod.list_agents()}
    names_spawnable = {a.name for a in mod.list_subagents()}
    assert "reviewer" in names_pickable
    assert "reviewer" in names_spawnable


def test_it_can_be_narrowed_to_a_subagent(with_config):
    with_config({"reviewer": AgentOverride(mode="subagent")})
    assert "reviewer" not in {a.name for a in mod.list_agents()}
    assert "reviewer" in {a.name for a in mod.list_subagents()}


def test_a_nonsense_mode_is_ignored_rather_than_stored(with_config):
    with_config({"reviewer": AgentOverride(mode="sideways")})
    assert mod.get_agent("reviewer").mode == "all"


# ── retuning a built-in ──

def test_a_built_in_can_be_retuned(with_config):
    with_config({"build": AgentOverride(model="anthropic/claude", temperature=0.7)})
    build = mod.get_agent("build")
    assert (build.model, build.temperature) == ("anthropic/claude", 0.7)


def test_retuning_does_not_leak_into_the_built_in_defaults(with_config):
    with_config({"build": AgentOverride(model="x")})
    mod.get_agent("build")
    assert mod.AGENTS["build"].model is None


def test_permission_rules_accumulate(with_config):
    before = len(mod.AGENTS["build"].permission)
    with_config({"build": AgentOverride(
        permission=[{"permission": "bash", "pattern": "*", "action": "deny"}]
    )})
    assert len(mod.get_agent("build").permission) == before + 1


def test_tools_are_replaced_not_widened(with_config):
    # A toolset is a whitelist; accumulating would widen an agent the user
    # just narrowed.
    with_config({"build": AgentOverride(tools=["read"])})
    assert mod.get_agent("build").tools == ["read"]


def test_a_built_in_agent_honours_an_explicit_empty_tool_allowlist(with_config):
    with_config({"build": AgentOverride(tools=[])})

    assert mod.get_agent("build").tools == []


def test_a_built_in_can_be_hidden(with_config):
    with_config({"plan": AgentOverride(hidden=True)})
    assert "plan" not in {a.name for a in mod.list_agents()}


# ── removing one ──

def test_a_built_in_can_be_disabled(with_config):
    with_config({"plan": AgentOverride(disable=True)})
    with pytest.raises(ValueError):
        mod.get_agent("plan")
    assert "plan" not in {a.name for a in mod.list_agents()}


def test_disabling_leaves_the_others_alone(with_config):
    with_config({"plan": AgentOverride(disable=True)})
    assert "build" in {a.name for a in mod.list_agents()}


# ── mode "all" in both lists ──

def test_an_all_agent_is_both_pickable_and_spawnable(with_config):
    with_config({"helper": AgentOverride(mode="all")})
    assert "helper" in {a.name for a in mod.list_agents()}
    assert "helper" in {a.name for a in mod.list_subagents()}


def test_an_all_agent_is_not_treated_as_a_subagent_only(with_config):
    with_config({"helper": AgentOverride(mode="all")})
    assert not mod.is_subagent("helper")


# ── what a custom agent is allowed to do ──

BUILD_ONLY_WORKFLOW_TOOLS = {
    "image_gen",
    "video_identity",
    "video_project",
    "video_generate",
    "video_transcribe",
    "video_render",
    "creator_context",
    "skill_manage",
}


def test_a_custom_agent_does_not_inherit_build_only_workflows(with_config):
    with_config({"reviewer": AgentOverride()})
    reviewer = mod.get_agent("reviewer")

    assert reviewer.mode == "all"
    assert "reviewer" in {agent.name for agent in mod.list_subagents()}
    assert BUILD_ONLY_WORKFLOW_TOOLS.isdisjoint(reviewer.tools)


def test_a_custom_agent_may_explicitly_opt_into_a_workflow_tool(with_config):
    with_config({"reviewer": AgentOverride(tools=["read", "image_gen"])})

    assert mod.get_agent("reviewer").tools == ["read", "image_gen"]


def test_a_custom_agent_honours_an_explicit_empty_tool_allowlist(with_config):
    with_config({"reviewer": AgentOverride(tools=[])})

    assert mod.get_agent("reviewer").tools == []


def _stripped(agent):
    """Tools removed from the schema before the model ever sees them."""
    from agent.loop import _get_permission_rules
    from agent.tool_resolution import agent_ruleset
    from core.config import get_config
    from permission.permission import disabled_tools

    rules = _get_permission_rules(get_config()) + agent_ruleset(agent)
    return disabled_tools(agent.tools, rules)


def test_a_custom_agent_cannot_enter_plan_mode_by_default(with_config):
    # opencode denies plan_enter/plan_exit/question in its defaults and only
    # build and plan re-allow them. Entering plan mode is a privilege, not
    # something an agent gets by existing.
    with_config({"reviewer": AgentOverride()})
    assert "plan_enter" in _stripped(mod.get_agent("reviewer"))


def test_it_can_be_granted_plan_mode_explicitly(with_config):
    with_config({"reviewer": AgentOverride(
        permission=[{"permission": "plan_enter", "pattern": "*", "action": "allow"}]
    )})
    assert "plan_enter" not in _stripped(mod.get_agent("reviewer"))


def test_build_can_enter_plan_mode(with_config):
    with_config({})
    assert "plan_enter" not in _stripped(mod.get_agent("build"))


def test_plan_can_leave_it(with_config):
    with_config({})
    assert "plan_exit" not in _stripped(mod.get_agent("plan"))


# ── colour ──

def test_a_hex_colour_is_kept(with_config):
    with_config({"reviewer": AgentOverride(color="#a1b2c3")})
    assert mod.get_agent("reviewer").color == "#a1b2c3"


def test_a_named_colour_is_kept(with_config):
    with_config({"reviewer": AgentOverride(color="accent")})
    assert mod.get_agent("reviewer").color == "accent"


def test_anything_else_is_dropped(with_config):
    # It reaches the browser as an inline style; a value the UI cannot resolve
    # is not worth forwarding.
    with_config({"reviewer": AgentOverride(color="javascript:alert(1)")})
    assert mod.get_agent("reviewer").color is None


# ── which agent a new conversation starts in ──

def test_the_default_is_build(with_config):
    with_config({})
    assert mod.default_agent_name() == "build"


def test_config_can_choose_another_primary(with_config):
    cfg = with_config({"reviewer": AgentOverride(mode="primary")})
    cfg.default_agent = "reviewer"
    assert mod.default_agent_name() == "reviewer"


def test_a_subagent_cannot_be_the_default(with_config):
    # It has no conversational prompt; honouring this would strand every new
    # chat in an agent that cannot answer.
    with_config({}).default_agent = "explore"
    assert mod.default_agent_name() == "build"


def test_an_unknown_name_falls_back(with_config):
    with_config({}).default_agent = "nonesuch"
    assert mod.default_agent_name() == "build"


def test_a_hidden_agent_cannot_be_the_default(with_config):
    with_config({"plan": AgentOverride(hidden=True)}).default_agent = "plan"
    assert mod.default_agent_name() == "build"
