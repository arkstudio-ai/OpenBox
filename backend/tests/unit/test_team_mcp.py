"""Service-pattern delegation is enforced at discovery and at actual dispatch."""
from dataclasses import replace

import pytest

from agent_catalog.compiler import compile_agent
from agent.tool_resolution import merge_sandbox_tools
from permission import permission
from team import mcp, runtime_binding
from team.errors import TeamError
from team.journal import command, snapshot
from tests.unit.test_mcp_security import Sandbox, _ctx, _raw_tool
from tests.unit.test_subagent_composition import _config
from tests.unit.test_team_compiler import spec
from tests.unit.test_team_journal import seed_run
from tool.mcp_tool import create_mcp_tools, create_mcp_resource_tool, _canonical_tool_id, _make_mcp_executor


@pytest.fixture
def delegated(monkeypatch):
    config = _config("openai/test")
    config.team_tools_enabled = True
    monkeypatch.setattr(mcp, "get_config", lambda: config)
    monkeypatch.setattr(runtime_binding, "get_config", lambda: config)
    refs = [{"server": "srv", "tools": ["search_*"]}]
    binding = runtime_binding.RuntimeBinding("team-a", "session-a", "user-a", "workspace-a", "project-a",
        "member", {"tool_allowlist": [], "mcp_refs": refs}, {})
    grant = {"mcp_refs": refs.copy()}
    async def current_grant():
        return grant
    monkeypatch.setattr(runtime_binding, "current_grant", current_grant)
    token = runtime_binding._current.set(binding)
    try:
        yield config, binding, grant
    finally:
        runtime_binding._current.reset(token)


def test_compiler_freezes_meta_ids_and_requires_explicit_service_grant(delegated):
    config, _, _ = delegated
    definition = spec(mcp_refs=[{"server": "srv", "tools": ["search_*"]}])
    with pytest.raises(TeamError, match="not approved"):
        compile_agent(definition, config=config, grant={"delegable_tools": ["read", "grep"]})
    compiled = compile_agent(definition, config=config,
        grant={"delegable_tools": ["read", "grep"], "mcp_refs": [{"server": "srv"}]})
    assert mcp.META_TOOLS <= compiled.authority.tool_ids
    coordinator = compile_agent(definition, config=config, role="coordinator")
    assert not mcp.META_TOOLS & coordinator.authority.tool_ids
    config.team_tools_enabled = False
    with pytest.raises(TeamError) as error:
        compile_agent(definition, config=config)
    assert error.value.code == "TOOL_NOT_TEAM_READY"


@pytest.mark.parametrize("count", [1, 45])
async def test_small_and_large_catalogues_keep_patterns_and_recheck_revocation(delegated, count):
    _, _, grant = delegated
    sandbox = Sandbox([*[_raw_tool(i) for i in range(count)], _raw_tool(100, server="private"), _raw_tool(101, name="delete_everything")])
    tools = await create_mcp_tools(sandbox, agent_id="team_member")
    assert set(tools) == {"mcp_find_tool", "mcp_call_tool"}
    ctx = _ctx(sandbox, workspace_id="workspace-a", agent_id="team_member")
    found = await tools["mcp_find_tool"].execute({"query": "search_fixture_0"}, ctx)
    assert "search_fixture_0" in found.output and "private" not in found.output
    canonical = _canonical_tool_id("srv", "search_fixture_0")
    result = await tools["mcp_call_tool"].execute({"canonical_id": canonical, "arguments": {"value": "ok"}}, ctx)
    assert not result.metadata.get("blocked") and len(sandbox.calls) == 1
    denied = await tools["mcp_call_tool"].execute({"canonical_id": _canonical_tool_id("private", "search_fixture_100")}, ctx)
    assert denied.metadata["blocked"]
    grant["mcp_refs"] = []
    revoked = await tools["mcp_call_tool"].execute({"canonical_id": canonical}, ctx)
    assert revoked.metadata["blocked"] and len(sandbox.calls) == 1
    refreshed = await tools["mcp_find_tool"].execute({"query": "search_fixture_0"}, _ctx(sandbox, workspace_id="workspace-a"))
    assert "search_fixture_0" not in refreshed.output
    assert not await create_mcp_tools(sandbox)


async def test_dynamic_catalogue_can_add_matching_tools_without_widening_frozen_ids(delegated):
    _, _, grant = delegated
    grant["mcp_refs"] = [{"server": "srv", "tools": ["*_7"]}]
    sandbox = Sandbox([_raw_tool(0)])
    assert not await create_mcp_tools(sandbox)
    sandbox.tools.append(_raw_tool(7))
    tools = await create_mcp_tools(sandbox)
    assert set(tools) == {"mcp_find_tool", "mcp_call_tool"}
    ctx = _ctx(sandbox, workspace_id="workspace-a")
    result = await tools["mcp_find_tool"].execute({"query": "search_fixture"}, ctx)
    assert "search_fixture_7" in result.output and "search_fixture_0" not in result.output
    wrong_context = await tools["mcp_find_tool"].execute({"query": "search_fixture"}, _ctx(sandbox, user_id="intruder"))
    assert "search_fixture_7" not in wrong_context.output


async def test_direct_executor_cannot_bypass_patterns_or_permission_denies(delegated):
    sandbox = Sandbox([])
    ctx = _ctx(sandbox, workspace_id="workspace-a")
    denied = await _make_mcp_executor("srv", "delete_everything", _canonical_tool_id("srv", "delete_everything"))({}, ctx)
    assert denied.metadata["blocked"] and not sandbox.calls
    canonical = _canonical_tool_id("srv", "search_fixture_0")
    seen = []
    async def authorize(tool, args):
        seen.append(tool)
        await permission.ask(ctx.session_id, tool, ["*"], input_data=args,
            config_rules=[permission.Rule(permission="*", pattern="*", action="ask")])
    ctx._authorize_tool = authorize
    result = await _make_mcp_executor("srv", "search_fixture_0", canonical)({}, ctx)
    assert not result.metadata.get("blocked") and seen == [canonical] and len(sandbox.calls) == 1
    async def denied_policy(tool, args):
        await permission.ask(ctx.session_id, tool, ["*"], input_data=args,
            config_rules=[permission.Rule(permission="*", pattern="*", action="deny")])
    ctx._authorize_tool = denied_policy
    await _make_mcp_executor("srv", "search_fixture_0", canonical)({}, ctx)
    assert len(sandbox.calls) == 1 and not permission._pending
    assert not await mcp.preapproved(canonical, ["*"], {})


async def test_resource_patterns_and_live_deployment_gate(delegated):
    config, binding, grant = delegated
    refs = [{"server": "srv", "tools": ["resource:docs://public/*"]}]
    runtime_binding._current.set(replace(binding, spec={**binding.spec, "mcp_refs": refs}))
    grant["mcp_refs"] = [{"server": "srv", "tools": ["*"]}]
    class Resources(Sandbox):
        async def read_mcp_resource(self, server, uri):
            self.calls.append((server, uri))
            return {"contents": [{"text": "approved resource"}]}
    sandbox = Resources([])
    ctx = _ctx(sandbox, workspace_id="workspace-a")
    tool = create_mcp_resource_tool()
    result = await tool.execute({"server": "srv", "uri": "docs://public/guide"}, ctx)
    assert "approved resource" in result.output
    denied = await tool.execute({"server": "srv", "uri": "docs://private/secret"}, ctx)
    assert denied.metadata["blocked"] and len(sandbox.calls) == 1
    config.team_tools_enabled = False
    denied = await tool.execute({"server": "srv", "uri": "docs://public/guide"}, ctx)
    assert denied.metadata["blocked"] and len(sandbox.calls) == 1


async def test_unavailable_service_records_one_notice_and_keeps_other_tools(delegated):
    _, binding, grant = delegated
    run_id, actor, root = await seed_run()
    runtime_binding._current.set(replace(binding, run_id=run_id, member_id=root,
        owner_user_id=actor.owner_user_id, workspace_id=actor.workspace_id))
    before = await snapshot(run_id, actor)
    for _ in range(2):
        tools = await merge_sandbox_tools({"read": object()}, Sandbox([]), agent_id="team_member")
        assert set(tools) == {"read"}
    state = await snapshot(run_id, actor, rebuild=True)
    notices = [notice for notice in state["notices"] if notice.get("code") == "MCP_SERVICE_UNAVAILABLE"]
    assert len(notices) == 1 and state["seq"] == before["seq"] + 1
