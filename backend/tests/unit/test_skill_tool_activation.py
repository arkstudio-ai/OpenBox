"""Skill-only tools stay out of ordinary schemas and appear after loading."""
import pytest

from agent.agent import AGENTS
from agent.tool_resolution import resolve_step_tools
from permission.permission import Rule
from tool.registry import register_builtin_tools


@pytest.mark.asyncio
async def test_image_gen_schema_is_absent_until_a_skill_activates_it():
    register_builtin_tools()

    ordinary = await resolve_step_tools(AGENTS["build"], None, [])
    assert "skill" in ordinary
    assert "image_gen" not in ordinary

    loaded = await resolve_step_tools(
        AGENTS["build"], None, [], activated_tools={"image_gen"}
    )
    assert "image_gen" in loaded
    assert loaded["image_gen"].skill_only is True


@pytest.mark.asyncio
async def test_skill_manage_schema_is_absent_until_skill_creator_activates_it():
    register_builtin_tools()

    ordinary = await resolve_step_tools(AGENTS["build"], None, [])
    assert "skill_manage" not in ordinary

    loaded = await resolve_step_tools(
        AGENTS["build"], None, [], activated_tools={"skill_manage"}
    )
    assert loaded["skill_manage"].skill_only is True


@pytest.mark.asyncio
async def test_a_skill_cannot_activate_an_ordinary_tool_outside_the_agent_whitelist():
    register_builtin_tools()

    tools = await resolve_step_tools(
        AGENTS["build"], None, [], activated_tools={"plan_exit"}
    )
    assert "plan_exit" not in tools


@pytest.mark.asyncio
async def test_permissions_can_still_hide_an_activated_skill_tool():
    register_builtin_tools()
    deny = [Rule(permission="image_gen", pattern="*", action="deny")]

    tools = await resolve_step_tools(
        AGENTS["build"], None, deny, activated_tools={"image_gen"}
    )
    assert "image_gen" not in tools
