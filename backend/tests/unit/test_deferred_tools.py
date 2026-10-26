"""Ordinary chat does not read cloud directories; real discovery still does."""
import asyncio
from dataclasses import replace

import pytest

from agent.agent import AgentDef
from agent.tool_exposure import build_eligible_catalog
from agent.tool_resolution import ResolvedStepTools, resolve_step_tools
from agent.deferred_tools import deferred_tools
from permission.permission import Rule
from tool.capability_search import capability_search_tool
from tool.skill_tool import skill_tool, skill_search_tool
from tool.tool import ToolContext, ToolResult


@pytest.fixture(autouse=True)
def registered_tools():
    from tool.registry import register_builtin_tools
    register_builtin_tools()


async def test_plain_resolution_does_not_read_remote_or_skill_directory(monkeypatch):
    from agent import tool_resolution as mod
    async def forbidden(*args, **kwargs):
        raise AssertionError("Ordinary conversation must not fetch a directory")
    monkeypatch.setattr(mod, "_catalogue_view", forbidden)
    monkeypatch.setattr(mod, "attach_skill_listing", forbidden)
    agent = AgentDef(name="build", description="test", tools=["capability_search", "skill", "skill_search", "bash"])
    result = await resolve_step_tools(agent, object(), [], user_id="owner", project_id="project",
        workdir="/workspace/project", defer_remote=True, return_catalogue_state=True)
    assert result.deferred and {"capability_search", "skill", "skill_search", "bash"} <= result.tools.keys()
    assert len(result.tools["skill"].description) < 450
    denied = await resolve_step_tools(agent, object(), [Rule(permission="skill", pattern="*", action="deny")],
        user_id="owner", defer_remote=True, return_catalogue_state=True)
    assert "skill" not in denied.tools


async def test_real_discovery_loads_once_and_never_grants_same_response_execution():
    calls, commits = [], []
    remote = replace(skill_tool, id="remote_example", description="Remote example", provider_name="remote_example")
    complete = ResolvedStepTools({"capability_search": capability_search_tool, "remote_example": remote}, "available")
    async def load():
        calls.append("load")
        return complete
    async def activate(resolved, ctx):
        ctx._capability_catalog = build_eligible_catalog(resolved.tools)
        ctx._capability_discovery_ids = frozenset({"remote_example"})
    async def commit(ids, generation, digests):
        commits.append((ids, generation, digests))
    ctx = ToolContext(available_tools=frozenset({"capability_search"}),
        _activate_tool_catalog=activate, _commit_tool_reveal=commit)
    tools = deferred_tools({"capability_search": capability_search_tool}, load)
    result = await tools["capability_search"].execute({"names": ["remote_example"]}, ctx)
    assert result.metadata["count"] == 1
    assert commits[0][0] == ("remote_example",)
    assert ctx.available_tools == frozenset({"capability_search"})
    await tools["capability_search"].execute({"names": ["absent"]}, ctx)
    assert calls == ["load"]


async def test_remote_unavailable_is_not_reported_as_no_matching_tools():
    async def load():
        return ResolvedStepTools({}, "unavailable")
    async def activate(*args):
        raise AssertionError("No unavailable catalogue can be activated")
    tools = deferred_tools({"capability_search": capability_search_tool}, load)
    result = await tools["capability_search"].execute({"query": "browser"},
        ToolContext(_activate_tool_catalog=activate))
    assert result.metadata["error_code"] == "catalogue_unavailable"


async def test_deferred_load_cancellation_stops_the_originating_request():
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def load():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def activate(*args):
        raise AssertionError("Cancelled read must never activate")
    tools = deferred_tools({"skill": skill_tool}, load)
    task = asyncio.create_task(tools["skill"].execute({"skill": "sample"},
        ToolContext(_activate_tool_catalog=activate)))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_skill_loading_uses_the_resolved_snapshot_and_arguments():
    seen = []
    async def selected(args, ctx):
        seen.append(args)
        return ToolResult(output="selected snapshot instructions")
    async def load():
        return ResolvedStepTools({"skill": replace(skill_tool, execute=selected)}, "stale")
    async def activate(*args):
        pass
    tools = deferred_tools({"skill": skill_tool, "skill_search": skill_search_tool}, load)
    result = await tools["skill"].execute({"skill": "permitted-name"}, ToolContext(_activate_tool_catalog=activate))
    assert result.output == "selected snapshot instructions"
    assert seen == [{"skill": "permitted-name"}]


async def test_compact_skill_description_keeps_permission_filtered_search(monkeypatch):
    from tool import skill_tool as mod
    async def permitted(*args):
        return [{"name": "allowed", "description": "video workflow", "source": "test"}]
    monkeypatch.setattr(mod, "_collect_permitted_skills", permitted)
    loader, search = await mod.build_skill_tools_with_listing(compact=True)
    assert "allowed" not in loader.description and len(loader.description) < 450
    result = await search.execute({"query": "video"}, ToolContext())
    assert "allowed" in result.output
