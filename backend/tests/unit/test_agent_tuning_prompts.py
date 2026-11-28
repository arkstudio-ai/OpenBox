"""Rollout switches A1 (relaxed_prompt) and A2 (consistent_prompt_rules).

docs/KERNEL_DSH_COMPARISON_20261128.md sections 18, 20, 21 and 25.3. With
every switch off the base prompt must stay byte-identical to the variants
that shipped before the switches existed.
"""
import hashlib
import re

import pytest

import core.config as config_mod
from agent.agent import AGENTS, AgentDef
from agent.prompts import system as S
from agent.tool_resolution import strip_denied
from core.config import AgentTuningConfig, OpenBoxConfig
from tool import registry


#: sha256 of each variant at commit a9da02c8, before any switch existed.
BASELINE_SHA256 = {
    "qwen": ("PROMPT_QWEN", "1e6657e199facca28a9bd6bbe91aed1199909e5eab8dbd68cd04b0e52210f493"),
    "gemini": ("PROMPT_GEMINI", "4166607873c33022b47d98ded11160811e928fc192450d3e01fe0089c3ed0987"),
    "anthropic": ("PROMPT_ANTHROPIC", "7f1e4770d2df8598006c3ca7cf03b5e458dbceab3c85fbac3f8211b16dd363b5"),
    "openai": ("PROMPT_OPENAI", "8d16a2c8cbb3fde8558124c0a4c24b9d6865b6d25d71921e72188e9c6565846c"),
    "codex": ("PROMPT_CODEX", "e1c631ec0b16a910ed38539feecb2e208c262ea64281d3e8c74cbb7f0c7bbc17"),
    "trinity": ("PROMPT_TRINITY", "b108d30471d3b33cec23814763624fa5fa09ff562ab32ce0012f83642a18b08a"),
}

#: One or more model ids per variant; production's default chat model routes to qwen.
MODELS = {
    "openai/qwen3.8-max": "qwen",
    "openai/qwen3.8-flash": "qwen",
    "openai/MiniMax-M3.1-Flash": "qwen",
    "deepseek/deepseek-chat": "qwen",
    "moonshot/kimi-k2": "qwen",
    "zhipu/glm-5": "qwen",
    "openai/gemini-3.8-flash": "gemini",
    "anthropic/claude-x": "anthropic",
    "openai/gpt-4o": "openai",
    "openai/gpt-5.6-luna": "codex",
    "openai/trinity-large": "trinity",
}
FAMILY_MODEL = {"qwen": "openai/qwen3.8-max", "gemini": "openai/gemini-3.8-flash", "anthropic": "anthropic/claude-x",
                "openai": "openai/gpt-4o", "codex": "openai/gpt-5.6-luna", "trinity": "openai/trinity-large"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def tuning(monkeypatch):
    def install(**fields):
        cfg = OpenBoxConfig(agent_tuning=AgentTuningConfig(**fields))
        monkeypatch.setattr(config_mod, "_config", cfg)
        return cfg

    install()
    return install


@pytest.fixture
def builtins(monkeypatch):
    monkeypatch.setattr(registry, "_tools", {})
    registry.register_builtin_tools(load_custom=False)
    return registry


@pytest.fixture
def eligible(builtins):
    """Static whitelist after permission filtering, as the loop passes it."""
    def for_agent(name):
        agent = AGENTS[name]
        return frozenset(strip_denied(builtins.get_tools_for_agent(agent.tools), [], agent))

    return {name: for_agent(name) for name in ("build", "plan")}


_ALIASES = {"Read": "read", "Edit": "edit", "Write": "write", "Glob": "glob", "Grep": "grep", "Bash": "bash",
            "Task": "task", "TodoWrite": "todo_write", "TodoRead": "todo_read", "WebFetch": "web_fetch",
            "WebSearch": "web_search", "MultiEdit": "multiedit"}


def referenced_tools(text: str, known: set[str]) -> set[str]:
    """Every registered tool the text names, by id, call syntax or alias."""
    found = set()
    for tool_id in known:
        name = re.escape(tool_id)
        patterns = [rf"`{name}`", rf"`{name}\(", rf"\b{name} tool\b"]
        if "_" in tool_id:
            patterns.append(rf"(?<![\w.]){name}(?!\w)")
        if any(re.search(pattern, text) for pattern in patterns):
            found.add(tool_id)
    for alias, tool_id in _ALIASES.items():
        if re.search(rf"\b{alias}\b", text):
            found.add(tool_id)
    if "dev-browser" in text:
        found.add("skill")
    return found


# --- switches off: byte-identical -------------------------------------------

def test_switches_off_keep_every_variant_byte_identical(tuning, eligible):
    for model, family in MODELS.items():
        assert S.prompt_family(model) == family, model
        constant, digest = BASELINE_SHA256[family]
        expected = getattr(S, constant)
        assert _sha(expected) == digest, constant
        assert S.get_system_prompt(model) == expected
        for tools in (None, eligible["build"], eligible["plan"], frozenset()):
            assert S.get_system_prompt(model, workspace_id="ws_any", tool_ids=tools) == expected


async def test_switches_off_keep_the_assembled_system_prompt(tuning, eligible):
    from agent.loop import _build_system_prompt

    for family, model in FAMILY_MODEL.items():
        for agent in ("build", "plan"):
            agent_def = AgentDef(name=agent, description="")
            plain = await _build_system_prompt(agent_def, model)
            plumbed = await _build_system_prompt(agent_def, model, workspace_id="ws_any",
                                                 prompt_tool_ids=eligible[agent])
            assert plumbed == plain
            assert plain[0] == getattr(S, BASELINE_SHA256[family][0])


def test_workspace_list_narrows_the_prompt_switches(tuning):
    tuning(relaxed_prompt=True, consistent_prompt_rules=True, workspaces=["ws_internal"])
    assert S.get_system_prompt("openai/qwen3.8-max", workspace_id="ws_customer") == S.PROMPT_QWEN
    assert S.get_system_prompt("openai/qwen3.8-max") == S.PROMPT_QWEN
    assert S.get_system_prompt("openai/qwen3.8-max", workspace_id="ws_internal") != S.PROMPT_QWEN


# --- A1: relaxed prompt ------------------------------------------------------

QWEN_REMOVED = (
    "Use exactly one tool per assistant message",
    "fewer than 4 lines",
    "Minimize output tokens",
    "## Verbosity Examples",
    "is 11 a prime number?",
    "keep your response to 1-2 sentences",
    "Use the question tool to clarify vague or ambiguous requests before taking action",
)
GEMINI_REMOVED = (
    "fewer than 3 lines",
    "extreme conciseness",
    "Wait for user approval before proceeding",
    "**User Approval:**",
    "Do not take significant actions beyond the clear scope of the request without confirming",
    "When in doubt, ask the user",
    "After user approval",
    "After confirmation",
    "2-3 sentence",
    "Ask clarifying questions if the requirements are ambiguous",
)
#: Section 21, "不改的部分": refusals, confirmations, destructive-command care, the captcha hand-off.
SAFETY_KEPT = (
    "## Captcha / risk control: hand off to the user",
    "Do NOT drag sliders, guess codes, click the challenge",
    "then call `desktop_takeover`",
    "Confirmations that tools or skills require (paid generation, deletion, another person's account) still apply.",
)
QWEN_KEPT = SAFETY_KEPT + (
    "IMPORTANT: Refuse to write code or explain code that may be used maliciously.",
    "NEVER commit changes unless the user explicitly asks you to.",
    "NEVER use interactive commands",
    "NEVER generate or guess URLs",
)
GEMINI_KEPT = SAFETY_KEPT + (
    "**Respect User Confirmations:** When a tool or action requires user confirmation, wait for it.",
    "**Explain Critical Commands:**",
    "ALWAYS refuse requests to create malware",
    "Never introduce code that exposes, logs, or commits secrets",
    "3. **Implementation:**",
    "5. **Solicit Feedback:**",
)


def test_relaxed_prompt_drops_the_rules_that_hold_the_model_back(tuning):
    tuning(relaxed_prompt=True)
    for model, family in MODELS.items():
        text = S.get_system_prompt(model)
        if family == "qwen":
            removed, kept = QWEN_REMOVED, QWEN_KEPT
        elif family == "gemini":
            removed, kept = GEMINI_REMOVED, GEMINI_KEPT
        else:
            assert text == getattr(S, BASELINE_SHA256[family][0]), "A1 only changes QWEN and GEMINI"
            continue
        for sentence in removed:
            assert sentence not in text, (model, sentence)
        for sentence in kept:
            assert sentence in text, (model, sentence)
        # A1 alone leaves A2's rule in place: the switches are independent.
        assert "Never use relative paths" in text
    assert "6. **Solicit Feedback:**" not in S.get_system_prompt("openai/gemini-3.8-flash")


def test_original_constants_still_carry_the_removed_rules():
    for sentence in QWEN_REMOVED:
        assert sentence in S.PROMPT_QWEN
    for sentence in GEMINI_REMOVED:
        assert sentence in S.PROMPT_GEMINI


def test_a_model_family_can_keep_the_one_tool_rule(tuning, monkeypatch):
    tuning(relaxed_prompt=True)
    monkeypatch.setattr(S, "ONE_TOOL_PER_MESSAGE_MODELS", ("kimi",))
    kept = S.get_system_prompt("moonshot/kimi-k2")
    assert "Use exactly one tool per assistant message" in kept
    assert "## Verbosity Examples" not in kept
    assert "Use exactly one tool per assistant message" not in S.get_system_prompt("openai/qwen3.8-max")


async def test_relaxed_prompt_changes_only_the_base_prompt(tuning):
    """Instruction files (the AGENTS.md deletion boundary), env and policy parts are untouched."""
    from agent.loop import _build_system_prompt

    for model in ("openai/qwen3.8-max", "openai/gemini-3.8-flash"):
        agent_def = AgentDef(name="build", description="")
        tuning()
        before = await _build_system_prompt(agent_def, model)
        tuning(relaxed_prompt=True)
        after = await _build_system_prompt(agent_def, model)
        assert after[0] != before[0]
        assert after[1:] == before[1:]


# --- A2: rules consistent with the kernel ------------------------------------

def test_consistent_rules_drop_the_absolute_path_rule(tuning):
    tuning(consistent_prompt_rules=True)
    for model in ("openai/qwen3.8-max", "openai/gemini-3.8-flash", "openai/MiniMax-M3.1-Flash"):
        text = S.get_system_prompt(model)
        assert "Never use relative paths" not in text
        assert "Always use absolute" not in text
        assert "relative to the working directory" in text
        # A2 alone keeps A1's rules: the switches are independent.
    assert "Use exactly one tool per assistant message" in S.get_system_prompt("openai/qwen3.8-max")
    assert "fewer than 3 lines" in S.get_system_prompt("openai/gemini-3.8-flash")


def test_detector_finds_the_old_inconsistency(builtins, eligible):
    """Without the switch the plan agent is told to use tools it does not have."""
    known = set(builtins._tools)
    stray = referenced_tools(S.PROMPT_QWEN, known) - eligible["plan"]
    assert {"skill", "computer", "cron", "desktop_takeover", "capability_search"} <= stray
    assert "todo_write" in referenced_tools(S.PROMPT_ANTHROPIC, known) - eligible["plan"]


@pytest.mark.parametrize("family", sorted(FAMILY_MODEL))
@pytest.mark.parametrize("agent", ["build", "plan"])
@pytest.mark.parametrize("relaxed", [False, True])
def test_every_tool_the_prompt_names_is_eligible(builtins, eligible, family, agent, relaxed):
    text = S.compose_system_prompt(FAMILY_MODEL[family], relaxed=relaxed, consistent=True,
                                   tool_ids=eligible[agent])
    assert referenced_tools(text, set(builtins._tools)) <= eligible[agent]
    # The captcha rule survives whichever hand-off tool is available.
    assert "Do NOT drag sliders" in text or family == "trinity"


@pytest.mark.parametrize("family", sorted(FAMILY_MODEL))
def test_only_baseline_tools_leaves_only_baseline_mentions(builtins, family):
    tools = S.PROMPT_BASELINE_TOOLS
    text = S.compose_system_prompt(FAMILY_MODEL[family], relaxed=True, consistent=True, tool_ids=tools)
    assert referenced_tools(text, set(builtins._tools)) <= tools
    if family != "trinity":
        assert "stop and ask the user to complete it themselves" in text


def test_baseline_tools_are_eligible_for_every_agent_using_these_prompts(eligible):
    for agent in ("build", "plan"):
        assert S.PROMPT_BASELINE_TOOLS <= eligible[agent]


def test_build_agent_text_only_loses_the_path_rule(eligible):
    """Every gated tool is eligible for build, so only the path rule changes."""
    for family, model in FAMILY_MODEL.items():
        base = getattr(S, BASELINE_SHA256[family][0])
        text = S.compose_system_prompt(model, consistent=True, tool_ids=eligible["build"])
        if family in {"qwen", "gemini"}:
            assert "Never use relative paths" in base
            assert "Never use relative paths" not in text
            assert text == S.compose_system_prompt(model, consistent=True)
        else:
            assert text == base


def test_partial_browser_tools_keep_the_takeover_route(builtins):
    tools = S.PROMPT_BASELINE_TOOLS | {"desktop_takeover", "web_search", "web_fetch"}
    text = S.compose_system_prompt("openai/qwen3.8-max", consistent=True, tool_ids=tools)
    assert "then call `desktop_takeover`. It suspends the run" in text
    assert "`computer`" not in text and "capability_search" not in text and "dev-browser" not in text
    assert referenced_tools(text, set(builtins._tools)) <= tools
