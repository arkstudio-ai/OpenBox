from dataclasses import replace

import pytest

from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import AgentSpec
from agent.subagent_authority import parse_subagent_authority
from team.errors import TeamError
from team.policy import COORDINATOR_TOOLS, MEMBER_TOOLS
from tool.workspace import CORE_TOOL_IDS
from tests.unit.test_subagent_composition import _config


def spec(**changes):
    return AgentSpec(name="Researcher", description="Compare evidence", when_to_use="Research tasks",
        instruction="Read the sources and distinguish facts from inferences.", tool_allowlist=["read", "grep"], **changes)


def test_frozen_member_authority_is_independent_of_later_definition_changes():
    definition = spec()
    compiled = compile_agent(definition, config=_config("openai/test"), grant={"delegable_tools": list(CORE_TOOL_IDS)})
    frozen = compiled.snapshot()
    definition.instruction = "Changed after admission"
    restored = parse_subagent_authority(frozen["authority"])
    assert restored.composition.persona == "Read the sources and distinguish facts from inferences."
    assert restored.tool_ids == set(CORE_TOOL_IDS) | MEMBER_TOOLS
    assert "task" not in restored.tool_ids and "question" not in restored.tool_ids


@pytest.mark.parametrize("tool", ["task", "question", "desktop_login", "douyin_publish", "skill_manage", "agent_manage", "unknown"])
def test_member_cannot_acquire_interactive_or_unreviewed_tools(tool):
    definition = spec()
    definition.tool_allowlist = [tool]
    with pytest.raises(TeamError) as exc:
        compile_agent(definition, config=_config("openai/test"))
    assert exc.value.code == "TOOL_NOT_TEAM_READY"


def test_model_lock_and_team_allowlist_are_both_enforced():
    config = _config("openai/first", "openai/second")
    with pytest.raises(TeamError) as exc:
        compile_agent(spec(default_model="openai/first", model_locked=True), config=config, model_override="openai/second")
    assert exc.value.code == "MODEL_LOCKED"
    with pytest.raises(TeamError) as exc:
        compile_agent(spec(default_model="openai/second"), config=config, allowed_models=["openai/first"])
    assert exc.value.code == "MODEL_NOT_ALLOWED"


def test_invalid_reasoning_names_the_model_and_exact_supported_variants():
    config = _config("openai/test")
    from agent.subagent_composition import provider_capabilities
    with pytest.raises(TeamError) as error:
        compile_agent(spec(reasoning="standard"), config=config)
    assert error.value.code == "CAPABILITY_UNSUPPORTED"
    assert error.value.current == {"model": "openai/test",
        "reasoning_variants": sorted(provider_capabilities("openai/test", config).reasoning_variants)}
    assert "Omit reasoning" in str(error.value)


@pytest.mark.parametrize("variants", [[], ["low", "high"]])
def test_catalog_model_options_match_compiler_and_do_not_expose_provider_secrets(variants):
    from agent_catalog.catalog import model_entries
    config = _config("openai/gpt-5", variants=variants,
        capabilities=["model", "tool_filter", "persona", "output_schema", *(["reasoning"] if variants else [])])
    models = model_entries(config)
    assert models[0]["reasoning_variants"] == sorted(variants)
    assert "test-key" not in str(models)
    assert "provider.invalid" not in str(models)
    for variant in [None, *variants]:
        assert compile_agent(spec(reasoning=variant), config=config).authority.composition.reasoning == variant
    if not variants:
        with pytest.raises(TeamError):
            compile_agent(spec(reasoning="high"), config=config)


def test_explicit_skill_binding_adds_dependencies_and_coordinator_does_not_execute():
    definition = spec(skill_refs=[{"name": "analysis"}])
    compiled = compile_agent(definition, config=_config("openai/test"), skills=[{"name": "analysis", "allowed_tools": ["web_search"]}])
    assert "web_search" in compiled.authority.tool_ids
    assert compiled.summary["warnings"] == []
    coordinator = compile_agent(spec(), config=_config("openai/test"), role="coordinator")
    assert COORDINATOR_TOOLS <= coordinator.authority.tool_ids
    assert not {"bash", "computer", "write", "skill"} & coordinator.authority.tool_ids


async def test_noninteractive_permission_preapproval_checks_only_unresolved_patterns(monkeypatch):
    from permission import permission
    from team import runtime_binding
    seen = []
    async def preapproved(name, patterns, input_data=None):
        seen.append((name, patterns))
        return patterns == ["pending"]
    monkeypatch.setattr(runtime_binding, "preapproved", preapproved)
    monkeypatch.setattr(permission, "_get_user_approved", lambda user_id: [])
    await permission.ask("member", "read", ["allowed", "pending"], config_rules=[
        permission.Rule(permission="read", pattern="allowed", action="allow")])
    assert seen == [("read", ["pending"])]
    with pytest.raises(TeamError) as exc:
        await permission.ask("member", "write", ["unapproved"])
    assert exc.value.code == "PERMISSION_REQUIRES_USER"
    assert not permission._pending
