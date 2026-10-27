"""Old installed browser docs cannot suppress current routing or grant tools."""
from types import SimpleNamespace

import pytest

from tool.bash import bash_tool
from tool.browser_action import browser_action_tool
from tool.skill_tool import SkillArgs, _execute_skill
from tool.tool import ToolContext
from tool.view_image import view_image_tool


@pytest.fixture(params=["legacy", "snapshot"])
def load_browser(request, monkeypatch):
    content = (
        "---\nname: dev-browser\ndescription: Browser\n"
        "allowed-tools: [browser_action, bash, view_image]\n---\n"
        "OLD WORKFLOW: run a shell script, then call view_image in another step."
    )

    async def no_host(_name):
        return None

    async def ready(_ctx):
        return "<browser_mode>local</browser_mode>"

    class Sandbox:
        async def get_skill(self, _name):
            return {"content": content, "base_dir": "/opt/openbox/skills/dev-browser", "files": []}

    async def load(_snapshot, name, *, scope):
        assert name == "dev-browser" and scope.user_id == "fixture-user"
        return SimpleNamespace(content=content, base_dir="/opt/openbox/skills/dev-browser",
            files=[], path="", provider_id="fixture", provider_revision="old-runtime")

    monkeypatch.setattr("skill.skill.get_skill", no_host)
    monkeypatch.setattr("tool.skill_tool._browser_readiness", ready)
    lookup = {"browser_action": browser_action_tool, "bash": bash_tool, "view_image": view_image_tool}
    ctx = ToolContext(user_id="fixture-user", session_id="s", sandbox=Sandbox(),
        available_tools=frozenset(lookup), _tool_execution_lookup=lookup)

    async def call():
        kwargs = {} if request.param == "legacy" else {
            "registry": SimpleNamespace(load=load), "snapshot": SimpleNamespace(revision="fixture-snapshot")}
        return await _execute_skill(SkillArgs(skill="dev-browser"), ctx, **kwargs)

    return ctx, call


async def test_both_loaders_append_current_platform_guidance_after_old_skill_body(load_browser):
    ctx, call = load_browser
    original_tools, original_lookup = ctx.available_tools, dict(ctx._tool_execution_lookup)
    result = await call()
    assert "OLD WORKFLOW" in result.output
    assert result.output.index("</skill_content>") < result.output.index("<browser_workflow>")
    assert result.output.index("</browser_mode>") < result.output.index("<browser_workflow>")
    assert "prefer browser_action" in result.output
    assert "page, client and pageName" in result.output
    assert "All existing tool permissions still apply" in result.output
    assert ctx.available_tools == original_tools and ctx._tool_execution_lookup == original_lookup
    assert not ctx._capability_revealed_ids


@pytest.mark.parametrize("missing", ["browser_action", "bash", "view_image"])
async def test_skill_declarations_cannot_add_deferred_tools_or_emit_routing_guidance(load_browser, missing):
    ctx, call = load_browser
    ctx.available_tools -= {missing}  # It remains in the broader catalogue lookup.
    before = ctx.available_tools
    result = await call()
    assert "allowed-tools" in result.output and "OLD WORKFLOW" in result.output
    assert "<browser_workflow>" not in result.output
    assert ctx.available_tools == before and missing not in ctx.available_tools
    assert not ctx._capability_revealed_ids
    assert not any(key in result.metadata for key in ("activated_tools", "revealed_tools", "allowed_tools"))


@pytest.mark.parametrize("invalid", ["no_frontier", "no_lookup", "missing_definition", "mismatched_definition"])
async def test_guidance_requires_matching_frozen_step_definitions(load_browser, invalid):
    ctx, call = load_browser
    if invalid == "no_frontier":
        ctx.available_tools = None
    elif invalid == "no_lookup":
        ctx._tool_execution_lookup = None
    elif invalid == "missing_definition":
        ctx._tool_execution_lookup.pop("browser_action")
    else:
        ctx._tool_execution_lookup["browser_action"] = bash_tool
    result = await call()
    assert "<browser_workflow>" not in result.output
    assert not ctx._capability_revealed_ids
