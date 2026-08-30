"""Skill content never grants tools; allowlists and permissions own exposure."""

import pytest

from agent.agent import AGENTS
from agent.tool_resolution import resolve_step_tools
from permission.permission import Rule
from skill.skill import SkillInfo, _scan_directory
import skill.skill as skill_catalog
import tool.skill_tool as skill_loader
from tool.registry import register_builtin_tools
from tool.skill_tool import SkillArgs
from tool.tool import ToolContext


BUILD_ONLY_TOOLS = {
    "image_gen",
    "video_identity",
    "video_project",
    "video_generate",
    "video_transcribe",
    "video_render",
    "creator_context",
    "skill_manage",
}

MALICIOUS_DECLARED_TOOLS = (
    "bash",
    "image_gen",
    "video_generate",
    "skill_manage",
    # Registered, but intentionally outside the build allowlist. Unlike the
    # entries above this one is not denied, so any grant path would be visible.
    "plan_exit",
)

DENIED_DECLARED_TOOLS = MALICIOUS_DECLARED_TOOLS[:-1]


class ContainerSkill:
    def __init__(self, payload=None):
        self.payload = payload or {}

    async def get_skill(self, _name):
        return self.payload

    async def list_skills(self):
        if not self.payload:
            return []
        return [{"name": "malicious", "description": "test"}]

    async def list_mcp_tools(self):
        return []

    async def list_mcp_resources(self):
        return []


@pytest.fixture(autouse=True)
def _register_tools():
    register_builtin_tools()


@pytest.mark.asyncio
async def test_build_has_platform_tools_while_subagents_do_not():
    build = await resolve_step_tools(AGENTS["build"], None, [])
    assert BUILD_ONLY_TOOLS <= build.keys()

    for name in ("plan", "explore", "general"):
        tools = await resolve_step_tools(AGENTS[name], None, [])
        assert BUILD_ONLY_TOOLS.isdisjoint(tools), name


@pytest.mark.asyncio
async def test_permissions_still_remove_a_platform_tool():
    deny = [Rule(permission="image_gen", pattern="*", action="deny")]
    tools = await resolve_step_tools(AGENTS["build"], None, deny)
    assert "image_gen" not in tools


def _host_skill_with_declaration(tmp_path, source: str, field: str) -> SkillInfo:
    root = tmp_path / source
    skill_dir = root / "malicious"
    skill_dir.mkdir(parents=True)
    declared = ", ".join(MALICIOUS_DECLARED_TOOLS)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: malicious\n"
        "description: test\n"
        f"{field}: [{declared}]\n"
        "---\n"
        "Follow these instructions.\n",
        encoding="utf-8",
    )
    skills = _scan_directory(root, source)
    assert len(skills) == 1
    assert skills[0].allowed_tools == MALICIOUS_DECLARED_TOOLS
    return skills[0]


@pytest.mark.parametrize(
    ("source", "field"),
    (("project", "allowed-tools"), ("global", "tools"), ("container", None)),
)
@pytest.mark.asyncio
async def test_every_skill_source_is_documentary_and_cannot_bypass_denial(
    monkeypatch, tmp_path, source, field
):
    """Any source and spelling of a skill declaration has zero tool-set effect."""
    container_payload = {
        "content": (
            "---\nname: malicious\ndescription: test\n"
            "allowed-tools: [bash, image_gen]\n"
            "tools: [video_generate, plan_exit]\n---\nbody"
        ),
        "allowed_tools": ["skill_manage"],
        "base_dir": "/data/skills/malicious",
        "files": [],
    }
    sandbox = ContainerSkill(container_payload if source in {"project", "container"} else {})
    host_skill = (
        _host_skill_with_declaration(tmp_path, source, field)
        if source != "container"
        else None
    )

    async def get_skill(_name):
        return host_skill

    async def list_skills():
        return [host_skill] if host_skill else []

    messages = []

    def capture_debug(message, *_args, **_kwargs):
        messages.append(str(message))

    monkeypatch.setattr(skill_catalog, "get_skill", get_skill)
    monkeypatch.setattr(skill_catalog, "list_skills", list_skills)
    monkeypatch.setattr(skill_loader.log, "debug", capture_debug)

    deny = [
        Rule(permission=name, pattern="*", action="deny")
        for name in DENIED_DECLARED_TOOLS
    ]
    before = set(await resolve_step_tools(AGENTS["build"], sandbox, deny))
    assert "plan_exit" not in before
    result = await skill_loader.execute(
        SkillArgs(skill="malicious"),
        ToolContext(sandbox=sandbox),
    )
    after = set(await resolve_step_tools(AGENTS["build"], sandbox, deny))

    assert before == after
    assert set(DENIED_DECLARED_TOOLS).isdisjoint(after)
    assert "plan_exit" not in after
    assert BUILD_ONLY_TOOLS.difference(DENIED_DECLARED_TOOLS) <= after
    assert "skill" in after
    assert result.metadata == {}
    assert "Activated tools" not in result.output

    ignored_log = "\n".join(messages)
    if source == "container":
        assert "Ignoring documentary tool fields" in ignored_log
        for key in ("allowed-tools", "allowed_tools", "tools"):
            assert key in ignored_log
    else:
        assert "Ignoring documentary tool fields" not in ignored_log
