"""Functional grouping must preserve the builtin surface and registry boundary."""
from types import SimpleNamespace

import pytest

from tool import catalog, registry


# Public IDs are persisted in Agent definitions, tool calls and team grants.
# Moving an implementation must neither lose nor rename any of them.
EXPECTED_IDS = frozenset("""
bash read write edit apply_patch glob grep multiedit view_image share_file
web_search web_fetch computer browser_mode desktop_login desktop_takeover
skill skill_search skill_manage creator_context todo_read todo_write plan_enter plan_exit
task batch agent_manage team_propose agent_catalog_search agent_catalog_get
team_member_start team_member_interrupt team_task_create team_task_update
team_view team_message_send team_wait team_finish question cron
image_gen video_generate video_transcribe video_compose video_analyze
hot_trends desktop_publish douyin_publish autopilot_run capability_search invalid
""".split())


def test_every_builtin_keeps_one_functional_home():
    groups = catalog.describe_builtin_groups()
    ids = [tool_id for group in groups for tool_id in group["tools"]]
    assert set(ids) == EXPECTED_IDS
    assert len(ids) == len(set(ids))
    assert len({group["id"] for group in groups}) == 11
    assert all(group["tools"] for group in groups)
    assert "integrations" not in {group["id"] for group in groups}


def test_inventory_does_not_register_or_authorize_tools(monkeypatch):
    sentinel = SimpleNamespace(id="external_tool")
    existing = {sentinel.id: sentinel}
    monkeypatch.setattr(registry, "_tools", existing)
    groups = catalog.describe_builtin_groups()
    assert groups
    assert registry._tools is existing
    assert registry.list_tools() == [sentinel]


def test_grouped_registration_preserves_custom_tools_and_executor_identity(monkeypatch):
    from tool.workspace.read import read_tool

    sentinel = SimpleNamespace(id="external_tool")
    monkeypatch.setattr(registry, "_tools", {sentinel.id: sentinel})
    monkeypatch.setattr(registry, "register_custom_tools", lambda: pytest.fail("custom loading was disabled"))
    registry.register_builtin_tools(load_custom=False)
    first = {tool.id: tool for tool in registry.list_tools()}
    registry.register_builtin_tools(load_custom=False)
    assert set(first) == EXPECTED_IDS | {sentinel.id}
    assert registry.get_tool("read") is read_tool
    assert all(registry.get_tool(tool_id) is tool for tool_id, tool in first.items())


@pytest.mark.parametrize("duplicate", ["group", "tool"])
def test_invalid_catalogue_cannot_partially_register(monkeypatch, duplicate):
    first = catalog.BUILTIN_GROUPS[0]
    second = first if duplicate == "group" else catalog.BUILTIN_GROUPS[1]
    tool = first.load()[0]
    monkeypatch.setattr(catalog, "BUILTIN_GROUPS", (first, second))
    monkeypatch.setattr(catalog.ToolGroup, "load", lambda self: (tool,))
    existing = {}
    monkeypatch.setattr(registry, "_tools", existing)
    with pytest.raises(ValueError, match="Duplicate builtin"):
        registry.register_builtin_tools(load_custom=False)
    assert registry._tools is existing
    assert not existing
