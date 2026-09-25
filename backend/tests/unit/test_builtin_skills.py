"""Packaged Skills stay complete across working directories, clients and images."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from api import metadata
from project.workspace import user_scope_for_identity
from skill import builtin
from skill.provider import ScopeKey, create_default_skill_registry
from tool.knowledge.skill_tool import SkillArgs, skill_tool
from tool.tool import ToolContext

NAMES = {"skill-creator", "scheduled-tasks", "imagegen", "video-production",
         "douyin-desktop-publish", "douyin-publish", "marketing-autopilot", "dev-browser", "agent-team"}


@pytest.fixture
def isolated_launch(tmp_path, monkeypatch):
    import skill.skill as legacy
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(legacy, "_skills", {})
    monkeypatch.setattr(legacy, "_loaded", False)
    monkeypatch.setattr(legacy, "_last_check", 0.0)
    monkeypatch.setattr(legacy, "_fingerprint", ())
    monkeypatch.setattr("skill.user_library.list_owned_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr("skill.user_library.annotate_installed_skills", AsyncMock(side_effect=lambda _u, rows, _w=None: rows))
    return tmp_path


def remote(rows):
    return SimpleNamespace(
        user_scope=user_scope_for_identity("skill-user"),
        list_skills=AsyncMock(return_value=rows),
        get_skill=AsyncMock(side_effect=lambda name: next(row for row in rows if name in (row["name"], row.get("install_dir")))),
    )


def test_manifest_covers_every_package_and_has_unique_identities():
    rows = builtin.validate_catalog()
    assert {row["name"] for row in rows} == NAMES
    assert len(rows) == 9
    assert len({row["group"] for row in rows}) == 7
    assert all(spec.group_title.keys() >= {"zh-CN", "en-US"} for spec in builtin.builtin_skills())


@pytest.mark.parametrize("corruption", ["duplicate", "escape", "missing", "unregistered", "identity"])
def test_catalog_rejects_invalid_or_unregistered_packages(tmp_path, monkeypatch, corruption):
    root = tmp_path / "builtins"
    package = root / "general/demo"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text('---\nname: demo\ndescription: Example\n---\nInstructions')
    manifest = {"version": 1, "groups": [{"id": "general", "title": {"zh-CN": "通用", "en-US": "General"}}],
                "skills": [{"name": "demo", "group": "general"}]}
    if corruption == "duplicate":
        manifest["skills"] *= 2
    elif corruption == "escape":
        manifest["skills"][0]["name"] = "../demo"
    elif corruption == "missing":
        manifest["skills"][0]["name"] = "missing"
    elif corruption == "unregistered":
        (root / "general/stray").mkdir()
        (root / "general/stray/SKILL.md").write_text('---\nname: stray\n---\nbody')
    else:
        (package / "SKILL.md").write_text('---\nname: other\ndescription: Example\n---\nbody')
    (root / "catalog.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(builtin, "BUILTIN_ROOT", root)
    with pytest.raises(ValueError):
        builtin.validate_catalog()


async def test_arbitrary_launch_directory_still_lists_and_loads_all_builtins(isolated_launch):
    from skill.skill import list_skills, get_skill
    skills = await list_skills()
    assert {skill.name for skill in skills} == NAMES
    assert all(skill.source == "builtin" and skill.builtin_group for skill in skills)
    registry = create_default_skill_registry(None)
    scope = ScopeKey(user_id="skill-user")
    try:
        snapshot = await registry.snapshot(scope)
        assert {skill.name for skill in snapshot.skills} == NAMES
        for name in NAMES:
            loaded = await registry.load(snapshot, name, scope=scope)
            assert loaded.content == (await get_skill(name)).content
            assert loaded.metadata["builtin_group"]
    finally:
        await registry.dispose()


async def test_agent_team_skill_example_compiles_with_minimal_team_capabilities(monkeypatch):
    import re
    from agent_catalog import catalog
    from team.journal import Actor
    from tool.collaboration.team_tools import Proposal
    from tests.unit.test_subagent_composition import _config

    content = (builtin.builtin_directory("agent-team") / "SKILL.md").read_text()
    example, = re.findall(r"```json\n(.*?)\n```", content, re.S)
    proposal = Proposal.model_validate_json(example)
    config = _config("openai/test", capabilities=["model", "tool_filter", "persona"], variants=[])
    config.team_generated_members_enabled = True
    monkeypatch.setattr(catalog, "get_config", lambda: config)
    prepared = await catalog.prepare_lineup(proposal.team, Actor("test-user", "test-workspace"), skills=[])
    assert len(prepared.members) == 2
    assert prepared.coordinator.authority.composition.persona
    assert prepared.spec.policy.max_members == 3
    assert prepared.spec.policy.member_selection == "explicit_only"
    assert all(member.authority.composition.reasoning is None for _, member, _ in prepared.members)


async def test_api_list_detail_and_agent_use_current_builtin_over_old_image(isolated_launch, monkeypatch):
    sandbox = remote([
        {"name": name, "source": "builtin", "description": "old " + name, "content": "OLD"}
        for name in ["dev-browser", "douyin-publish", "video-production"]
    ])
    monkeypatch.setattr("sandbox.manager.sandbox_manager.get_client_any", AsyncMock(return_value=sandbox))
    actor = {"user_id": "skill-user", "workspace_id": "w1"}
    listed = await metadata.list_skills(current_user=actor)
    assert {row["name"] for row in listed} == NAMES
    assert all(row["source"] == "builtin" and row["builtin_group"] for row in listed)
    assert not any(row["description"].startswith("old ") for row in listed)
    detail = await metadata.get_skill("douyin-publish", current_user=actor)
    assert "fallback" in detail["description"]
    registry = create_default_skill_registry(sandbox)
    scope = ScopeKey(user_id="skill-user")
    try:
        snapshot = await registry.snapshot(scope)
        selected = await registry.load(snapshot, "douyin-publish", scope=scope)
        assert selected.content == detail["content"]
        assert selected.provider_id == "builtin-package"
    finally:
        await registry.dispose()
    direct = await skill_tool.execute(SkillArgs(skill="douyin-publish"), ToolContext(sandbox=sandbox))
    assert detail["content"].strip() in direct.output
    assert "OLD" not in direct.output


async def test_offline_desktop_does_not_hide_builtin_or_details(isolated_launch, monkeypatch):
    sandbox = remote([])
    sandbox.list_skills.side_effect = ConnectionError("offline")
    sandbox.get_skill.side_effect = ConnectionError("offline")
    monkeypatch.setattr("sandbox.manager.sandbox_manager.get_client_any", AsyncMock(return_value=sandbox))
    actor = {"user_id": "skill-user"}
    assert {row["name"] for row in await metadata.list_skills(current_user=actor)} == NAMES
    assert (await metadata.get_skill("imagegen", current_user=actor))["content"]
    registry = create_default_skill_registry(sandbox)
    try:
        snapshot = await registry.snapshot(ScopeKey(user_id="skill-user"))
        assert snapshot.available and snapshot.stale
        assert {row.name for row in snapshot.skills} == NAMES
    finally:
        await registry.dispose()


async def test_user_install_and_unknown_remote_builtin_are_not_relabelled(isolated_launch, monkeypatch):
    rows = [
        {"name": "imagegen", "source": "container", "description": "My custom workflow", "content": "CUSTOM", "install_dir": "my-images"},
        {"name": "custom-runtime", "source": "builtin", "description": "Other builtin", "content": "OTHER"},
    ]
    sandbox = remote(rows)
    monkeypatch.setattr("sandbox.manager.sandbox_manager.get_client_any", AsyncMock(return_value=sandbox))
    listed = {row["name"]: row for row in await metadata.list_skills(current_user={"user_id": "skill-user"})}
    assert set(listed) == NAMES | {"custom-runtime"}
    assert listed["imagegen"] == rows[0]
    registry = create_default_skill_registry(sandbox)
    scope = ScopeKey(user_id="skill-user")
    try:
        snapshot = await registry.snapshot(scope)
        selected = await registry.load(snapshot, "imagegen", scope=scope)
        assert selected.content == "CUSTOM" and selected.source == "container"
    finally:
        await registry.dispose()


def test_browser_recovery_bundle_uses_canonical_skill_and_resources():
    from sandbox.browser_runtime import runtime_files
    sources = json.loads(runtime_files()["dev-browser-sources.json"])
    directory = builtin.builtin_directory("dev-browser")
    assert sources["SKILL.md"] == (directory / "SKILL.md").read_text()
    assert sources["references/scraping.md"] == (directory / "references/scraping.md").read_text()
    assert "src/client.ts" in sources
