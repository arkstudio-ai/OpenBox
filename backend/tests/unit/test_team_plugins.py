"""Only administrator metadata may delegate a platform plugin to members."""
import json
from dataclasses import replace

import pytest

from agent_catalog.compiler import compile_agent
from team import policy, runtime_binding
from team.errors import TeamError
from tests.unit.test_platform_plugins import TOOL_SOURCE, write_manifest
from tests.unit.test_subagent_composition import _config
from tests.unit.test_team_compiler import spec
from tool import registry
from tool.integrations.platform_plugins import load_platform_plugin, read_plugin_manifest, platform_plugin_fingerprint, PlatformPluginError, stage_platform_plugin_generation


def plugin(tmp_path, metadata=None):
    path = write_manifest(tmp_path)
    source = TOOL_SOURCE.replace("parallel_safe=True,", "parallel_safe=True, team_allowed=True, team_exclusive_group='desktop',")
    (path.parent / "tools.py").write_text(source)
    if metadata is not None:
        body = json.loads(path.read_text())
        body["team_tools"] = {"plugin_echo": metadata}
        path.write_text(json.dumps(body))
    return path


async def test_code_cannot_self_delegate_but_reviewed_manifest_can(tmp_path, monkeypatch):
    path = plugin(tmp_path)
    before = read_plugin_manifest(path)
    tool = load_platform_plugin(before, reserved_ids=set())["plugin_echo"]
    config = _config("openai/test")
    config.team_tools_enabled = True
    monkeypatch.setitem(registry._tools, tool.id, tool)
    assert not tool.team_allowed and tool.team_exclusive_group is None
    with pytest.raises(TeamError):
        policy.tool_policy(tool.id, config)
    body = json.loads(path.read_text())
    body["team_tools"] = {"plugin_echo": {"team_allowed": True, "exclusive_group": "desktop"}}
    path.write_text(json.dumps(body))
    after = read_plugin_manifest(path)
    assert platform_plugin_fingerprint(before) != platform_plugin_fingerprint(after)
    generation = await stage_platform_plugin_generation(after, reserved_ids=set())
    reviewed = generation.as_dict()[tool.id]
    monkeypatch.setitem(registry._tools, tool.id, reviewed)
    definition = spec()
    definition.tool_allowlist = [tool.id]
    from tool.workspace import CORE_TOOL_IDS
    compiled = compile_agent(definition, config=config, grant={"delegable_tools": [*CORE_TOOL_IDS, tool.id]})
    assert compiled.summary["exclusive_group"] == "desktop"
    assert policy.delegated_plugins(config) == [tool.id]
    # Revocation in the current registry applies even to an old frozen definition.
    monkeypatch.setitem(registry._tools, tool.id, replace(reviewed, team_allowed=False))
    with pytest.raises(TeamError):
        policy.tool_policy(tool.id, config)
    await generation.dispose()


@pytest.mark.parametrize("metadata", [{"team_allowed": True}, {"team_allowed": "yes", "exclusive_group": None},
    {"team_allowed": True, "exclusive_group": "unmanaged"}, {"team_allowed": True, "exclusive_group": []},
    {"team_allowed": True, "exclusive_group": None, "permission": "allow"}])
def test_invalid_admin_policy_fails_before_import(tmp_path, metadata):
    path = plugin(tmp_path, metadata)
    with pytest.raises(PlatformPluginError):
        read_plugin_manifest(path)


async def test_hot_replacement_cannot_add_a_desktop_lane_to_an_admitted_member(tmp_path, monkeypatch):
    path = plugin(tmp_path, {"team_allowed": True, "exclusive_group": "desktop"})
    tool = load_platform_plugin(read_plugin_manifest(path), reserved_ids=set())["plugin_echo"]
    config = _config("openai/test")
    config.team_tools_enabled = True
    monkeypatch.setitem(registry._tools, tool.id, tool)
    monkeypatch.setattr(runtime_binding, "get_config", lambda: config)
    monkeypatch.setattr("team.mcp.get_config", lambda: config)
    async def grant():
        return {"delegable_tools": [tool.id]}
    monkeypatch.setattr(runtime_binding, "current_grant", grant)
    binding = runtime_binding.RuntimeBinding("run", "member", "user", "workspace", "project", "member",
        {"tool_allowlist": [tool.id]}, {"capability_summary": {"exclusive_group": None}})
    runtime_binding._current.set(binding)
    assert not await runtime_binding.restrict_current_tools({tool.id: tool})
    runtime_binding._current.set(replace(binding, admission={"capability_summary": {"exclusive_group": "desktop"}}))
    assert tool.id in await runtime_binding.restrict_current_tools({tool.id: tool})
