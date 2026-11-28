"""A3: eligible built-in tool names are visible, their schemas stay deferred.

docs/KERNEL_DSH_COMPARISON_20261128.md section 21 ("关于 A3"), under the
rules of docs/TOOL_SCHEMA_DEFERRED_LOADING_PLAN.md: permission before
catalogue, no sandbox-plane names, a list that does not change with per-step
narrowing, a size that counts against the 24K resident budget, and an error
that never confirms a hidden or denied tool exists.
"""
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.config as config_mod
from agent import prompt_visibility as V
from agent.agent import AGENTS, AgentDef
from agent.prompt_visibility import (
    UNMATERIALIZED_TOOL_HINT,
    build_tool_visibility_fragment,
    deferred_builtin_listing,
    render_tool_listing,
    tool_name_listing,
)
from agent.tool_exposure import build_eligible_catalog, portable_plan
from agent.tool_exposure_budget import ExposureBudgets, apply_exposure_budget
from agent.tool_resolution import strip_denied
from core.config import AgentTuningConfig, OpenBoxConfig
from light.production_form import restrict_guided_tools
from permission.permission import Rule
from tool import registry
from tool.tool import ToolInfo, ToolResult

from tests.unit.test_agent_tuning_prompts import referenced_tools

MEMORY_IDS = {"memory_prepare", "memory_search", "memory_read_sources", "memory_read_more",
              "current_task_state", "memory_forget"}


@pytest.fixture
def tuning(monkeypatch):
    def install(**fields):
        cfg = OpenBoxConfig(agent_tuning=AgentTuningConfig(**fields))
        monkeypatch.setattr(config_mod, "_config", cfg)
        return cfg

    install()
    return install


@pytest.fixture
def build_tools(monkeypatch):
    """The build agent's static whitelist after permission filtering, as the loop builds it."""
    monkeypatch.setattr(registry, "_tools", {})
    registry.register_builtin_tools(load_custom=False)

    def make(rules=(), *, memory=False, session=None):
        agent = AGENTS["build"]
        tools = strip_denied(registry.get_tools_for_agent(agent.tools), list(rules), agent)
        tools = restrict_guided_tools(tools, session or SimpleNamespace(light_production=None), None)
        return tools if memory else {name: tool for name, tool in tools.items() if name not in MEMORY_IDS}

    return make


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- switch off: byte-identical ---------------------------------------------

#: sha256 of build_tool_visibility_fragment output at commit a9da02c8.
FRAGMENT_SHA256 = {
    (("read", "capability_search", "bash"), "portable", 5): "a38213e67ac8ae992ec70e7448051d19e6b974369571958363430fcfe9e44297",
    (("read", "bash"), "portable", 0): "f06021eb19ff65b722f927d36c6293d9ab9b8f263b464ece4e1bb80cc1a489ef",
    ((), "portable", 0): "fa1bd6398f1e3355e0e4d212ec5b240c0a99b885add56e94ebc10fc2eeb5f573",
    (("read", "cron", "web_search", "computer", "todo_write"), "legacy_eager", 0):
        "7723d5366e13dec75ae9a5e2ac05741b318dfe85fa3bc35fb75467b2b5b35469",
    (("read",), "native_openai", 5): "3e07b7ad3a7aca5c9e1b751f66b0b6c8027b2e82bdd36d9695accacfa3534a5f",
}


def test_switch_off_keeps_the_visibility_fragment_byte_identical(tuning, build_tools):
    listing = tool_name_listing(build_tools(), agent_name="build", model_id="openai/qwen3.8-max",
                                workspace_id="ws_any")
    assert listing == ()
    for (visible, strategy, deferred), digest in FRAGMENT_SHA256.items():
        fragment = build_tool_visibility_fragment(visible, strategy=strategy, deferred_count=deferred,
                                                  listing=listing)
        assert _sha(fragment) == digest
        assert _sha(build_tool_visibility_fragment(visible, strategy=strategy, deferred_count=deferred)) == digest


def test_switches_do_not_change_tool_definitions(tuning, build_tools):
    import json

    from agent.tool_payload import build_tool_definitions
    from agent.tool_runtime import assemble_tool_runtime

    def wire():
        runtime = assemble_tool_runtime(build_tools(), mode="portable", agent_name="build", editor_id="edit")
        return json.dumps(build_tool_definitions(dict(runtime.provider_tools), "litellm"), sort_keys=True)

    before = wire()
    tuning(relaxed_prompt=True, consistent_prompt_rules=True, tool_name_listing=True)
    assert wire() == before


def test_workspace_list_narrows_the_listing(tuning, build_tools):
    tuning(tool_name_listing=True, workspaces=["ws_internal"])
    tools = build_tools()
    assert tool_name_listing(tools, agent_name="build", model_id="m", workspace_id="ws_customer") == ()
    assert tool_name_listing(tools, agent_name="build", model_id="m", workspace_id="ws_internal")


# --- what is listed ----------------------------------------------------------

@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("model, editor", [("openai/qwen3.8-max", "edit"), ("openai/gpt-5.6-luna", "apply_patch")])
def test_listing_is_exactly_the_default_deferred_platform_builtins(build_tools, memory, model, editor):
    tools = build_tools(memory=memory)
    listing = deferred_builtin_listing(tools, agent_name="build", editor_id=editor)
    catalog = build_eligible_catalog(tools)
    plan = portable_plan(catalog, agent_name="build", editor_id=editor)
    expected = {tool_id for tool_id in plan.deferred_ids
                if catalog.entries[tool_id].source == "builtin" and catalog.entries[tool_id].plane == "platform"}
    assert {name for name, _hint in listing} == expected - {"skill", "skill_search"}
    assert editor not in {name for name, _hint in listing}
    assert all(hint and len(hint) <= V.LISTING_HINT_CHARS and "\n" not in hint for _name, hint in listing)
    assert tool_name_listing.__module__ == "agent.prompt_visibility"


def test_listing_for_the_build_agent_is_specific(build_tools):
    names = {name for name, _hint in deferred_builtin_listing(build_tools(), agent_name="build", editor_id="edit")}
    assert {"web_search", "web_fetch", "cron", "computer", "desktop_takeover", "todo_write", "video_generate",
            "image_gen", "share_file", "apply_patch"} <= names
    # Resident and loader tools are already visible; light-production tools need a production.
    assert not names & {"bash", "read", "write", "glob", "grep", "task", "question", "edit",
                        "skill", "skill_search", "capability_search", "light_wan_story", "light_production_form"}


def test_denied_tools_never_appear(tuning, build_tools):
    tuning(tool_name_listing=True)
    rules = [Rule(permission="cron", pattern="*", action="deny"),
             Rule(permission="web_search", pattern="*", action="deny")]
    tools = build_tools(rules)
    listing = tool_name_listing(tools, agent_name="build", model_id="openai/qwen3.8-max")
    fragment = build_tool_visibility_fragment(["bash", "read", "capability_search"], strategy="portable",
                                              deferred_count=9, listing=listing)
    assert "web_search" not in fragment and "- cron:" not in fragment
    assert "- web_fetch:" in fragment


def test_sandbox_plane_tools_and_skill_text_never_appear(build_tools):
    async def run(_args, _ctx):
        return ToolResult(title="", output="")

    tools = build_tools()
    tools["mcp_github_create_issue"] = ToolInfo(id="mcp_github_create_issue", description="Create a GitHub issue.",
                                                 parameters=None, execute=run)
    tools["sandboxed_helper"] = ToolInfo(id="sandboxed_helper", description="Sandbox helper.", parameters=None,
                                         execute=run, source="mcp", plane="sandbox")
    tools["skill"] = ToolInfo(id="skill", description="Load a skill. Available: sandbox-secret-skill.",
                              parameters=None, execute=run)
    # A config-defined agent has no resident Skill loader; it is still never listed.
    rows = deferred_builtin_listing(tools, agent_name="custom_agent", editor_id="edit")
    text = "\n".join(render_tool_listing(rows))
    for leaked in ("mcp_github_create_issue", "sandboxed_helper", "sandbox-secret-skill", "- skill:"):
        assert leaked not in text


def test_nothing_is_listed_without_capability_search(build_tools):
    tools = build_tools([Rule(permission="capability_search", pattern="*", action="deny")])
    assert "capability_search" not in tools
    assert deferred_builtin_listing(tools, agent_name="build", editor_id="edit") == ()


def test_listing_is_rendered_in_portable_mode_only(build_tools):
    listing = deferred_builtin_listing(build_tools(), agent_name="build", editor_id="edit")
    portable = build_tool_visibility_fragment(["read", "capability_search"], strategy="portable",
                                              deferred_count=3, listing=listing)
    assert "- web_search: " in portable and portable.endswith("</tool_visibility>")
    for strategy in ("legacy_eager", "shadow_wire_eager", "native_openai", "emergency_eager"):
        fragment = build_tool_visibility_fragment(["read", "capability_search"], strategy=strategy,
                                                  deferred_count=3, listing=listing)
        assert fragment == build_tool_visibility_fragment(["read", "capability_search"], strategy=strategy,
                                                          deferred_count=3)


def test_listing_does_not_change_with_a_narrowed_step(build_tools):
    listing = deferred_builtin_listing(build_tools(), agent_name="build", editor_id="edit")
    ordinary = build_tool_visibility_fragment(
        ["bash", "read", "write", "glob", "grep", "edit", "task", "question", "skill", "skill_search",
         "capability_search"], strategy="portable", deferred_count=32, listing=listing)
    narrowed = build_tool_visibility_fragment(
        ["capability_search", "video_generate", "video_compose"], strategy="portable", deferred_count=2,
        listing=listing)
    assert ordinary == narrowed


def test_listing_fits_the_resident_budget(build_tools):
    budgets = ExposureBudgets()
    for memory in (False, True):
        tools = build_tools(memory=memory)
        listing = deferred_builtin_listing(tools, agent_name="build", editor_id="edit")
        listing_chars = len("\n".join(render_tool_listing(listing))) + 1
        catalog = build_eligible_catalog(tools)
        resident = apply_exposure_budget(catalog, portable_plan(catalog, agent_name="build", editor_id="edit"))
        print(f"memory={memory} listed={len(listing)} listing_chars={listing_chars} "
              f"resident_chars={resident.resident_chars} total={resident.resident_chars + listing_chars}")
        assert listing_chars <= V.LISTING_MAX_CHARS
        if not memory:
            # The default deployment: resident definitions plus the listing stay under 24K.
            assert resident.resident_chars + listing_chars <= budgets.resident_hard_chars


def test_an_oversized_listing_falls_back_to_names(monkeypatch, build_tools):
    listing = deferred_builtin_listing(build_tools(), agent_name="build", editor_id="edit")
    monkeypatch.setattr(V, "LISTING_MAX_CHARS", 500)
    lines = render_tool_listing(listing)
    assert lines[1:] == [f"- {name}" for name, _hint in listing]


def test_assembled_prompt_with_listing_names_only_eligible_tools(tuning, build_tools):
    """A2 and A3 together: the listing adds names, all of them eligible."""
    from agent.prompts.system import compose_system_prompt

    tuning(tool_name_listing=True)
    known = set(registry._tools)
    for rules in ((), [Rule(permission="computer", pattern="*", action="deny")]):
        tools = build_tools(rules, memory=True)
        listing = tool_name_listing(tools, agent_name="build", model_id="openai/qwen3.8-max")
        text = compose_system_prompt("openai/qwen3.8-max", relaxed=True, consistent=True, tool_ids=frozenset(tools))
        text += "\n" + build_tool_visibility_fragment(["bash", "read", "capability_search"], strategy="portable",
                                                      deferred_count=10, listing=listing)
        assert referenced_tools(text, known) <= set(tools)


# --- the error for a tool whose schema is not materialized -------------------

class _Info:
    def __init__(self):
        self.id = "msg_assistant"
        self.error = None


class _Ctx:
    message_id = ""
    part_id = ""
    workspace_id = "ws_any"


class _NotAborted:
    @staticmethod
    def is_set():
        return False

    @staticmethod
    async def wait():
        await asyncio.Event().wait()


class _Hooks:
    async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
        return await execute_fn(args, ctx)


async def _call_unmaterialized(monkeypatch, tool_name, *, visible=("read", "capability_search")):
    from agent import processor as P

    saved, executed = [], []

    async def capture(part, *args, **kwargs):
        saved.append(part)

    async def anoop(*_args, **_kwargs):
        return None

    async def execute(args, ctx):
        executed.append(args)
        return ToolResult(title="unexpected", output="unexpected")

    async def stream(**_kwargs):
        yield {"type": "tool_call", "tool": tool_name, "args": {}, "call_id": "call_x", "invalid": True}
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}

    for name, value in (("save_part", capture), ("update_message_info", anoop), ("update_session", anoop),
                        ("create_compaction", anoop), ("_history_for_compaction", anoop), ("stream_llm", stream)):
        monkeypatch.setattr(P, name, value)
    monkeypatch.setattr(P.bus, "publish", lambda *a, **k: None)
    # image_gen is eligible but hidden; denied_tool and not_a_tool are outside the catalogue.
    lookup = {name: SimpleNamespace(execute=execute) for name in (*visible, "image_gen")}
    await P.process_step(
        session_id="s1", user_id="u1", session=None, agent_def=AgentDef(name="build", description="test"),
        system=[], llm_messages=[], tools={name: lookup[name] for name in visible},
        execution_lookup=lookup, step_executable_ids=frozenset(visible),
        provider_to_canonical={name: name for name in lookup},
        model_id="test/model", ctx=_Ctx(), hooks=_Hooks(), assistant_info=_Info(),
        sandbox=None, abort=_NotAborted(), doom_loop_history=[],
    )
    blocked = next(part for part in reversed(saved) if getattr(part, "tool", "") == tool_name)
    assert executed == []
    return blocked.error


@pytest.mark.parametrize("tool_name", ["image_gen", "denied_tool", "not_a_tool"])
async def test_error_text_is_unchanged_with_the_switch_off(tuning, monkeypatch, tool_name):
    error = await _call_unmaterialized(monkeypatch, tool_name)
    assert error == f"Tool '{tool_name}' is not materialized for this step. Available: capability_search, read"


async def test_error_points_at_capability_search_without_confirming_anything(tuning, monkeypatch):
    tuning(tool_name_listing=True)
    errors = {name: await _call_unmaterialized(monkeypatch, name) for name in ("image_gen", "denied_tool", "not_a_tool")}
    for name, error in errors.items():
        assert error == (f"Tool '{name}' is not materialized for this step. "
                         f"Available: capability_search, read {UNMATERIALIZED_TOOL_HINT}")
    # A hidden eligible tool, a denied one and an unknown one read the same.
    assert len({error.replace(name, "X") for name, error in errors.items()}) == 1
    assert "image_gen" not in UNMATERIALIZED_TOOL_HINT


async def test_error_adds_nothing_when_capability_search_is_not_callable(tuning, monkeypatch):
    tuning(tool_name_listing=True)
    error = await _call_unmaterialized(monkeypatch, "image_gen", visible=("read",))
    assert error == "Tool 'image_gen' is not materialized for this step. Available: read"


# --- the real loop: one listing across a narrowed step ------------------------

async def test_loop_keeps_prompt_tools_and_listing_across_a_narrowed_step(ensure_test_db, monkeypatch):
    import assistant.review_repair as repair
    from agent import loop
    from agent.processor import StepOutcome, StepResult
    from agent.tool_resolution import ResolvedStepTools
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime, _seed_session

    config = _loop_config()
    config.tool_exposure.mode = "portable"
    config.agent_tuning = AgentTuningConfig(consistent_prompt_rules=True, tool_name_listing=True)
    user_id, _project_id, session_id, _user, lease = await _seed_session(model=config.model, output_format=None)
    steps = []

    async def step(**kwargs):
        steps.append({
            "tools": frozenset(kwargs["tools"]),
            "visibility": next(part for part in kwargs["system"] if part.startswith("<tool_visibility>")),
        })
        return StepResult(outcome=StepOutcome.CONTINUE, finish_reason="tool_calls" if len(steps) == 1 else "stop",
                          text="", duration=0.1)

    _patch_real_loop_runtime(monkeypatch, config=config, process_step=step)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock())
    monkeypatch.setattr(registry, "_tools", {})
    registry.register_builtin_tools(load_custom=False)
    tools = {name: tool for name, tool in registry.get_tools_for_agent(AGENTS["build"].tools).items()
             if name not in MEMORY_IDS}

    async def resolve(*_args, **_kwargs):
        return ResolvedStepTools(dict(tools), "available")

    prompt_tool_ids = []

    async def system(_agent_def, _model_id, **kwargs):
        prompt_tool_ids.append(kwargs.get("prompt_tool_ids"))
        return ["base system"]

    monkeypatch.setattr(loop, "resolve_step_tools", resolve)
    monkeypatch.setattr(loop, "_build_system_prompt", system)
    # The second step is narrowed the way revision repair narrows it.
    repair_calls = []

    def repair_mode(_history):
        repair_calls.append(1)
        return "reply" if len(repair_calls) >= 2 else None

    monkeypatch.setattr(repair, "repair_mode", repair_mode)
    monkeypatch.setattr(repair, "permits_tool",
                        lambda mode, tool_id, *_a: not mode or tool_id in {"bash", "read", "capability_search", "web_search"})
    monkeypatch.setattr(repair, "instruction", lambda _mode: "Repair step.")

    await loop.run_loop(session_id, user_id=user_id, lease=lease)

    assert len(steps) == 2
    first, narrowed = steps
    assert narrowed["tools"] == {"bash", "read", "capability_search"}
    assert first["tools"] != narrowed["tools"]
    assert prompt_tool_ids[0] == prompt_tool_ids[1]
    assert {"cron", "web_search", "video_generate"} <= prompt_tool_ids[0]
    assert "light_wan_story" not in prompt_tool_ids[0]
    assert "- cron: " in first["visibility"]
    assert first["visibility"] == narrowed["visibility"]
