"""Fail-loud Task Provider/Agent composition and snapshot bounds."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.agent import (
    AgentDef,
    SUBAGENT_ALL_CAPABILITIES,
    SUBAGENT_BASE_CAPABILITIES,
)
from agent.subagent_authority import (
    SubagentAuthorityError,
    compose_subagent_authority,
    parse_subagent_authority,
    with_subagent_composition,
)
from agent.subagent_composition import (
    SubagentCompositionError,
    build_subagent_composition,
    parse_subagent_composition,
    validate_composition_availability,
    validate_output_schema,
    validate_structured_result,
)


def _config(
    *models: str,
    default: str | None = None,
    provider_name: str | None = None,
    capabilities: list[str] | None = None,
    variants: list[str] | None = None,
):
    selected = default or models[0]
    slot = provider_name or selected.split("/", 1)[0]
    declared = capabilities if capabilities is not None else [
        "model", "tool_filter", "reasoning", "persona", "output_schema",
    ]
    return SimpleNamespace(
        model=selected,
        models=[SimpleNamespace(
            id=model,
            provider=provider_name,
            subagent_capabilities=None,
            subagent_reasoning_variants=None,
        ) for model in models],
        provider={
            slot: {
                "api_key": "test-key",
                "base_url": "https://provider.invalid/v1",
                "options": {},
                "subagent_capabilities": declared,
                "subagent_reasoning_variants": (
                    variants if variants is not None
                    else ["low", "medium", "high"]
                ),
            }
        },
    )


def _agent(
    *,
    tools: list[str] | None = None,
    model: str | None = None,
    capabilities=frozenset(SUBAGENT_ALL_CAPABILITIES),
) -> AgentDef:
    return AgentDef(
        name="bounded",
        description="bounded test child",
        tools=tools or ["read", "grep"],
        mode="subagent",
        model=model,
        prompt="Base frozen prompt",
        permission=[{"permission": "*", "pattern": "*", "action": "allow"}],
        subagent_capabilities=frozenset(capabilities),
    )


def _build(**overrides):
    values = {
        "agent_def": _agent(),
        "parent_tool_ids": {"task", "read", "grep"},
        "config": _config("openai/gpt-5.4"),
        "inherited_model": "openai/gpt-5.4",
        "requested_model": "openai/gpt-5.4",
        "reasoning": "high",
        "persona": "Be exact.",
        "requested_tools": ["read"],
        "output_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        },
        "seed_mode": "fresh",
    }
    values.update(overrides)
    return build_subagent_composition(**values)


def test_allowed_composition_is_exact_bounded_and_digest_protected():
    composition = _build()
    snapshot = composition.to_json()
    restored = parse_subagent_composition(snapshot)

    assert restored.model == "openai/gpt-5.4"
    assert restored.reasoning == "high"
    assert restored.persona == "Be exact."
    assert restored.tool_allowlist == frozenset({"read"})
    assert restored.output_schema == {
        "properties": {"answer": {"type": "string"}},
        "type": "object",
    }
    assert restored.provider.provider_id == "openai"
    assert "reasoning" in restored.provider.capabilities
    assert "output_schema" in restored.provider.capabilities
    frozen = restored.frozen_agent()
    assert frozen.tools == ["read", "grep"]
    assert "Base frozen prompt" in (frozen.prompt or "")
    assert "Be exact." in (frozen.prompt or "")

    tampered = deepcopy(snapshot)
    tampered["model"] = "openai/gpt-5.4-mini"
    with pytest.raises(SubagentCompositionError, match="digest"):
        parse_subagent_composition(tampered)

    with pytest.raises(SubagentCompositionError, match="no longer configured"):
        validate_composition_availability(
            restored,
            _config("openai/gpt-5.4-mini"),
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        (
            {"requested_model": "openai/not-configured"},
            "not configured",
        ),
        (
                {
                    "config": _config(
                        "custom/private-model",
                        capabilities=[],
                        variants=[],
                    ),
                "inherited_model": "custom/private-model",
                "requested_model": "custom/private-model",
                "reasoning": None,
                "output_schema": None,
            },
                "omits mandatory Task capabilities",
        ),
        (
            {
                    "config": _config(
                        "openai/plain-chat",
                        capabilities=[
                            "model", "tool_filter", "persona", "output_schema",
                        ],
                        variants=[],
                    ),
                "inherited_model": "openai/plain-chat",
                "requested_model": "openai/plain-chat",
            },
            "does not support reasoning",
        ),
        (
            {"requested_tools": ["bash"]},
            "parent authority",
        ),
        (
            {"requested_tools": ["task"]},
            "child preset",
        ),
        (
            {
                "agent_def": _agent(
                    capabilities=SUBAGENT_BASE_CAPABILITIES,
                ),
            },
            "does not support reasoning composition",
        ),
        (
            {
                "agent_def": _agent(tools=["read", "image_gen"]),
                "parent_tool_ids": {"task", "read", "image_gen"},
                "requested_tools": ["image_gen"],
            },
            "build-only",
        ),
    ],
)
def test_unsupported_model_provider_capability_or_tool_fails_loud(overrides, match):
    with pytest.raises(SubagentCompositionError, match=match):
        _build(**overrides)


def test_agent_fixed_model_cannot_be_overridden():
    with pytest.raises(SubagentCompositionError, match="fixes model"):
        _build(
            agent_def=_agent(model="openai/gpt-5.4"),
            config=_config("openai/gpt-5.4", "openai/gpt-5.4-mini"),
            requested_model="openai/gpt-5.4-mini",
        )


def test_output_schema_structural_and_byte_budgets_fail_before_acceptance():
    with pytest.raises(SubagentCompositionError, match="object with properties"):
        validate_output_schema({"type": "array", "items": {"type": "string"}})

    deep: dict = {"type": "string"}
    for _ in range(20):
        deep = {"type": "object", "properties": {"x": deep}}
    with pytest.raises(SubagentCompositionError, match="structural bounds"):
        validate_output_schema(deep)

    oversized = {
        "type": "object",
        "properties": {
            f"field_{index}": {"type": "string", "description": "x" * 200}
            for index in range(200)
        },
    }
    with pytest.raises(SubagentCompositionError, match="byte budget"):
        validate_output_schema(oversized)


def test_real_json_schema_validation_rejects_wrong_types_and_extra_fields():
    schema = validate_output_schema({
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    })
    assert schema is not None
    assert validate_structured_result(schema, {"answer": "yes"}) == {
        "answer": "yes"
    }
    with pytest.raises(SubagentCompositionError, match="violates output_schema"):
        validate_structured_result(schema, {"answer": 3})
    with pytest.raises(SubagentCompositionError, match="Additional properties"):
        validate_structured_result(schema, {"answer": "yes", "extra": True})


def test_model_provider_binding_is_explicit_and_cold_drift_fails_closed():
    config = _config(
        "gateway/private-chat",
        provider_name="openai",
    )
    composition = _build(
        config=config,
        inherited_model="gateway/private-chat",
        requested_model="gateway/private-chat",
    )
    assert composition.provider.provider_id == "openai"
    assert len(composition.provider.binding_digest) == 64

    config.provider["openai"]["base_url"] = "https://changed.invalid/v1"
    with pytest.raises(SubagentCompositionError, match="capabilities changed"):
        validate_composition_availability(composition, config)

    # The OpenAI adapter remaps `max` for some models; it is not an exact
    # variant unless the deployment explicitly declares it (this one does not).
    with pytest.raises(SubagentCompositionError, match="does not support reasoning"):
        _build(reasoning="max")


@pytest.mark.asyncio
async def test_frozen_build_prompt_and_persona_reach_actual_system_prompt():
    import agent.subagent_authority as authority_mod
    from agent.loop import _build_system_prompt

    composition = _build(
        agent_def=AgentDef(
            name="build",
            description="spawnable build preset",
            tools=["read"],
            mode="all",
            prompt="Frozen build instructions",
            permission=[{"permission": "*", "pattern": "*", "action": "allow"}],
            subagent_capabilities=frozenset(SUBAGENT_ALL_CAPABILITIES),
        ),
        parent_tool_ids={"task", "read"},
        requested_tools=["read"],
        persona="Frozen persona overlay",
    )
    frozen = composition.frozen_agent()
    token = authority_mod._bound_frozen_agent.set(frozen)
    try:
        parts = await _build_system_prompt(frozen, composition.model)
    finally:
        authority_mod._bound_frozen_agent.reset(token)
    assert parts[0].startswith("Frozen build instructions")
    assert "Frozen persona overlay" in parts[0]


def test_authority_v1_is_compatible_but_v2_never_guesses_composition():
    legacy = compose_subagent_authority(
        tool_ids={"task", "read"},
        permission_rules=[{"permission": "*", "pattern": "*", "action": "allow"}],
        guard_rules=[],
    ).to_json()
    assert legacy["version"] == 1
    assert parse_subagent_authority(legacy).composition is None

    unexpected = {**legacy, "composition": {}}
    with pytest.raises(SubagentAuthorityError, match="unsupported"):
        parse_subagent_authority(unexpected)

    missing = {**legacy, "version": 2}
    with pytest.raises(SubagentAuthorityError, match="unsupported"):
        parse_subagent_authority(missing)

    composition = _build(requested_tools=["read"])
    upgraded = with_subagent_composition(
        parse_subagent_authority(legacy),
        composition,
    ).to_json()
    assert upgraded["version"] == 2
    assert parse_subagent_authority(upgraded).composition is not None
