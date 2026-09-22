from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import AgentSpec, TeamPolicy
from agent_catalog.validation import agent_summary
from skill.dependencies import required_mcp
from team.errors import TeamError
from tool.workspace import CORE_TOOL_IDS, load_tools
from tests.unit.test_subagent_composition import _config


def definition(**changes):
    return AgentSpec(name="Research", description="Check evidence", when_to_use="Research",
        instruction="Use the selected workflow", tool_allowlist=[], **changes)


def test_core_tools_are_exactly_the_workspace_domain_and_cannot_be_removed_from_definitions():
    assert set(CORE_TOOL_IDS) == {tool.id for tool in load_tools()}
    assert set(definition().tool_allowlist) == set(CORE_TOOL_IDS)
    assert set(CORE_TOOL_IDS) <= set(TeamPolicy().delegable_tools)
    assert TeamPolicy(delegable_tools=[]).delegable_tools == []
    assert definition(execution_policy={"tool_categories": ["T2"]}).execution_policy.tool_categories == ["T2", "T0"]
    compiled = compile_agent(definition(), role="trial", config=_config("openai/test"))
    assert set(CORE_TOOL_IDS) == compiled.authority.tool_ids


def test_skill_dependencies_are_frozen_before_grant_checks_and_do_not_bypass_them():
    spec = definition(skill_refs=[{"name": "research", "source": "project"}])
    skills = [
        {"name": "research", "source": "global", "allowed_tools": ["computer"]},
        {"name": "research", "source": "project", "allowed_tools": ["web_search"]},
    ]
    compiled = compile_agent(spec, role="trial", config=_config("openai/test"), skills=skills)
    assert {"skill", "skill_search", "web_search"} <= compiled.authority.tool_ids
    assert "computer" not in compiled.authority.tool_ids
    assert "web_search" in compiled.spec.tool_allowlist
    with pytest.raises(TeamError) as error:
        compile_agent(spec, config=_config("openai/test"), skills=skills, grant={"delegable_tools": list(CORE_TOOL_IDS)})
    assert error.value.code == "PERMISSION_REQUIRES_USER"
    assert "web_search" in error.value.current["tools"]


def test_mcp_dependency_cannot_be_removed_or_narrowed_in_definition_but_grant_still_applies():
    config = _config("openai/test")
    config.team_tools_enabled = True
    spec = definition(skill_refs=[{"name": "docs"}], mcp_refs=[{"server": "docs", "tools": []}],
        execution_policy={"tool_categories": ["T0"]})
    skills = [{"name": "docs", "requires_mcp": ["docs"]}]
    compiled = compile_agent(spec, role="trial", config=config, skills=skills)
    assert compiled.spec.mcp_refs[0].model_dump() == {"server": "docs", "tools": ["*"]}
    assert "MCP" in compiled.spec.execution_policy.tool_categories
    grant = {"delegable_tools": compiled.spec.tool_allowlist, "mcp_refs": []}
    with pytest.raises(TeamError) as error:
        compile_agent(spec, config=config, skills=skills, grant=grant)
    assert error.value.code == "PERMISSION_REQUIRES_USER"
    assert error.value.current["mcp_servers"] == ["docs"]


def test_discovery_does_not_request_every_available_integration_or_allow_forbidden_dependencies():
    compiled = compile_agent(definition(skill_mode="all_accessible"), role="trial",
        config=_config("openai/test"), skills=[{"name": "publishing", "allowed_tools": ["desktop_publish"]}])
    assert "desktop_publish" not in compiled.authority.tool_ids
    assert {"skill", "skill_search"} <= compiled.authority.tool_ids
    with pytest.raises(TeamError) as error:
        compile_agent(definition(skill_refs=[{"name": "publishing"}]), role="trial",
            config=_config("openai/test"), skills=[{"name": "publishing", "allowed_tools": ["desktop_publish"]}])
    assert error.value.code == "TOOL_NOT_TEAM_READY"


async def test_summary_persists_the_validated_dependency_closure(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: _config("openai/test"))
    monkeypatch.setattr("agent_catalog.validation.freeze_specs", AsyncMock(return_value=[
        {"name": "research", "allowed_tools": ["web_search"]}]))
    spec = definition(skill_refs=[{"name": "research"}])
    summary = await agent_summary(spec, SimpleNamespace(), strict=True)
    assert "web_search" in spec.tool_allowlist
    assert summary["_compiled_trial"]["spec"] == spec.model_dump(mode="json")


def test_mcp_declarations_are_bounded_and_support_frontmatter_spelling():
    assert required_mcp({"requires-mcp": [" docs ", "docs", None, "bad\nname", "x" * 129]}) == ("docs",)


def test_host_skill_list_exposes_service_dependencies(tmp_path):
    from skill.skill import _scan_directory
    from skill.presentation import skill_row
    (tmp_path / "SKILL.md").write_text("---\nname: docs\ndescription: Search docs\nrequires-mcp:\n  - docs-service\n---\nRead docs.")
    row = skill_row(_scan_directory(tmp_path, "project")[0])
    assert row["requires_mcp"] == ["docs-service"]


async def test_dependency_services_survive_skill_snapshot_storage(monkeypatch):
    from skill.provider import SkillDefinition
    from skill.snapshot import freeze_specs
    from team.journal import Actor
    from tests.unit.test_team_skills import Registry
    from trajectory.storage import MemoryBlobStore
    monkeypatch.setattr("skill.snapshot.get_blob_store", lambda: MemoryBlobStore())
    registry = Registry(SkillDefinition("docs", "Search docs", "user", "Body", metadata={"requires-mcp": ["docs-service"]}))
    entries = await freeze_specs([definition(skill_refs=[{"name": "docs"}])], Actor("owner", "workspace"), registry=registry)
    assert entries[0]["requires_mcp"] == ["docs-service"]
