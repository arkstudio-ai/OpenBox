"""Main's host/sandbox Skill behavior through the scoped Agent entry point."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.tool_resolution import resolve_step_tools
from project.workspace import user_scope_for_identity
from tool.knowledge.skill_tool import SkillArgs, skill_tool
from tool.tool import ToolContext


class Catalogue:
    user_scope = user_scope_for_identity("skill-user")

    def __init__(self, *, online=True):
        self.online = online
        self.get_skill = AsyncMock(return_value={
            "name": "demo", "content": "REMOTE WORKFLOW",
            "base_dir": "/data/skills/demo", "files": ["scripts/run.py"],
        })

    async def get_catalogue_projection_state(self):
        return SimpleNamespace(
            availability="available" if self.online else "unavailable",
            snapshot={"generation": "1", "skills": [{"name": "demo", "description": "Remote"}],
                      "mcp_tools": [], "mcp_resources": []} if self.online else None,
        )


@pytest.fixture
def skill_environment(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.chdir(app)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("skill.user_library.list_owned_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr("agent.tool_resolution.get_tools_for_agent", lambda _: {"skill": skill_tool})
    return tmp_path


def write_skill(root, body, name="demo"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Example\n---\n{body}")


async def resolve(sandbox, root):
    context = ToolContext(user_id="skill-user", project_id="p", sandbox=sandbox,
                          workdir=str(root / "sandbox-project"))
    definitions = await resolve_step_tools(
        SimpleNamespace(tools=["skill"], permission=[], name="build"), sandbox, [],
        user_id=context.user_id, project_id=context.project_id, workdir=context.workdir,
    )
    return definitions, context


@pytest.mark.parametrize("directory", [".openbox", ".openagent", ".claude", ".agents"])
async def test_application_project_skill_still_overrides_remote(skill_environment, directory):
    root = skill_environment
    write_skill(root / "app" / directory / "skills", "CURRENT HOST WORKFLOW")
    sandbox = Catalogue()
    definitions, context = await resolve(sandbox, root)
    result = await definitions["skill"].execute(SkillArgs(skill="demo"), context)
    assert "CURRENT HOST WORKFLOW" in result.output
    assert "REMOTE WORKFLOW" not in result.output
    sandbox.get_skill.assert_not_awaited()


async def test_host_roots_keep_main_precedence_and_global_fallback(skill_environment):
    root = skill_environment
    for directory in ("openbox", "openagent"):
        write_skill(root / "config" / directory / "skills", directory, name="global-only")
    for directory in (".openbox", ".openagent", ".claude", ".agents"):
        write_skill(root / "app" / directory / "skills", directory)
    definitions, context = await resolve(Catalogue(), root)
    project = await definitions["skill"].execute(SkillArgs(skill="demo"), context)
    global_skill = await definitions["skill"].execute(SkillArgs(skill="global-only"), context)
    assert "\n.agents\n" in project.output
    assert "\nopenagent\n" in global_skill.output


async def test_remote_still_overrides_global_host_skill(skill_environment):
    root = skill_environment
    write_skill(root / "config/openbox/skills", "GLOBAL WORKFLOW")
    definitions, context = await resolve(Catalogue(), root)
    result = await definitions["skill"].execute(SkillArgs(skill="demo"), context)
    assert "REMOTE WORKFLOW" in result.output
    assert "/data/skills/demo" in result.output


@pytest.mark.parametrize("name", ["中文工作流", "Design Review"])
async def test_host_display_name_does_not_remove_the_skill_catalogue(skill_environment, name):
    root = skill_environment
    write_skill(root / "app/.openbox/skills", "HOST DISPLAY NAME", name=name)
    definitions, context = await resolve(Catalogue(), root)
    assert "demo" in definitions["skill"].description
    assert name in definitions["skill"].description
    result = await definitions["skill"].execute(SkillArgs(skill=name), context)
    assert "HOST DISPLAY NAME" in result.output


async def test_cold_sandbox_outage_does_not_hide_available_host_skill(skill_environment):
    root = skill_environment
    write_skill(root / "app/.openbox/skills", "HOST FALLBACK")
    sandbox = Catalogue(online=False)
    definitions, context = await resolve(sandbox, root)
    assert "skill" in definitions
    result = await definitions["skill"].execute(SkillArgs(skill="demo"), context)
    assert "HOST FALLBACK" in result.output
    missing = await definitions["skill"].execute(SkillArgs(skill="remote-only"), context)
    assert "not found" in missing.title.lower()
    sandbox.get_skill.assert_not_awaited()


async def test_scoped_skill_load_preserves_trajectory_event(skill_environment, monkeypatch):
    record = AsyncMock()
    monkeypatch.setattr("trajectory.record", record)
    definitions, context = await resolve(Catalogue(), skill_environment)
    context.trace_context = object()
    result = await definitions["skill"].execute(SkillArgs(skill="demo"), context)
    loaded = [call for call in record.await_args_list if call.args[0] == "skill.loaded"]
    assert len(loaded) == 1
    event, payload = loaded[0].args
    assert event == "skill.loaded"
    assert payload["content"] == "REMOTE WORKFLOW"
    assert payload["effective_content"] == result.output
    assert payload["source"] == "sandbox"
    assert loaded[0].kwargs["context"] is context.trace_context
