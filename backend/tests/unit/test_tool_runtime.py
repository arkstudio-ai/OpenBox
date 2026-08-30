import pytest
from pydantic import BaseModel

from agent.tool_exposure import ExposureSignals
from agent.tool_payload import measure_tool_definitions
from agent.tool_runtime import (
    assemble_tool_runtime,
    effective_exposure_mode,
    enforce_serialized_payload_limits,
)
from core.config import ToolExposureConfig
from tool.tool import ToolInfo, ToolResult


class Args(BaseModel):
    value: str = ""


async def _execute(args, ctx):
    return ToolResult(title="ok", output="ok")


def _tool(name: str, **kwargs):
    description = kwargs.pop("description", f"Use {name}.")
    return ToolInfo(
        id=name,
        description=description,
        parameters=Args,
        execute=_execute,
        **kwargs,
    )


def test_legacy_runtime_keeps_provider_execution_and_eligible_equivalent():
    runtime = assemble_tool_runtime(
        {"read": _tool("read"), "image_gen": _tool("image_gen")},
        mode="legacy_eager",
        agent_name="build",
    )
    assert set(runtime.eligible_catalog.entries) == {"read", "image_gen"}
    assert set(runtime.provider_tools) == {"read", "image_gen"}
    assert set(runtime.execution_lookup) == {"read", "image_gen"}
    assert runtime.step_executable_ids == frozenset({"read", "image_gen"})


def test_portable_runtime_keeps_hidden_tool_eligible_but_not_executable():
    runtime = assemble_tool_runtime(
        {
            "read": _tool("read"),
            "capability_search": _tool("capability_search"),
            "image_gen": _tool("image_gen"),
        },
        mode="portable",
        agent_name="build",
        signals=ExposureSignals(user_task_text="fix the parser"),
    )
    assert "image_gen" in runtime.eligible_catalog.entries
    assert "image_gen" in runtime.execution_lookup
    assert "image_gen" not in runtime.provider_tools
    assert "image_gen" not in runtime.step_executable_ids
    assert runtime.canonical_id_for_provider_name("image_gen") == "image_gen"


def test_explicit_image_intent_materializes_the_pack_without_skill_loading():
    runtime = assemble_tool_runtime(
        {
            "read": _tool("read"),
            "capability_search": _tool("capability_search"),
            "image_gen": _tool("image_gen"),
        },
        mode="portable",
        agent_name="build",
        signals=ExposureSignals(user_task_text="生成一张蓝色方块图片"),
    )
    assert "image_gen" in runtime.provider_tools
    assert "image_gen" in runtime.step_executable_ids


def test_shadow_wire_is_eager_while_candidate_is_portable():
    runtime = assemble_tool_runtime(
        {
            "read": _tool("read"),
            "capability_search": _tool("capability_search"),
            "image_gen": _tool("image_gen"),
        },
        mode="shadow",
        agent_name="build",
    )
    assert set(runtime.provider_tools) == {"read", "image_gen"}
    assert runtime.candidate_plan is not None
    assert "capability_search" in runtime.candidate_plan.direct_ids
    assert "image_gen" in runtime.candidate_plan.discovery_ids
    assert "image_gen" in runtime.candidate_plan.deferred_ids


def test_request_local_synthetic_tool_is_direct_but_not_catalogued():
    synthetic = _tool("structured_output")
    runtime = assemble_tool_runtime(
        {"read": _tool("read")},
        mode="portable",
        agent_name="build",
        synthetic_tools={"structured_output": synthetic},
    )
    assert "structured_output" not in runtime.eligible_catalog.entries
    assert "structured_output" in runtime.provider_tools
    assert "structured_output" in runtime.execution_lookup
    assert "structured_output" in runtime.step_executable_ids
    assert synthetic.source == "synthetic"


def test_provider_name_maps_to_canonical_security_identity():
    mcp = _tool(
        "wire_name",
        source="mcp",
        plane="sandbox",
        canonical_id="mcp:v2:" + "a" * 52,
        provider_name="mcp_wire_name",
    )
    runtime = assemble_tool_runtime(
        {"mcp_wire_name": mcp},
        mode="legacy_eager",
        agent_name="build",
    )
    canonical = "mcp:v2:" + "a" * 52
    assert set(runtime.provider_tools) == {"mcp_wire_name"}
    assert set(runtime.execution_lookup) == {canonical}
    assert runtime.canonical_id_for_provider_name("mcp_wire_name") == canonical


def test_empty_explicit_agent_runtime_stays_completely_empty():
    runtime = assemble_tool_runtime({}, mode="portable", agent_name="custom")
    assert not runtime.eligible_catalog.entries
    assert not runtime.provider_tools
    assert not runtime.execution_lookup
    assert not runtime.step_executable_ids


def test_effective_exposure_mode_canaries_only_the_build_agent():
    for global_mode in ("legacy_eager", "shadow", "emergency_eager"):
        assert effective_exposure_mode(global_mode, "build") == global_mode
        assert effective_exposure_mode(global_mode, "custom") == global_mode

    for requested_mode in ("portable", "native_auto"):
        assert effective_exposure_mode(requested_mode, "build") == requested_mode
        for agent_name in ("plan", "explore", "general", "custom"):
            assert effective_exposure_mode(requested_mode, agent_name) == "shadow"
        assert effective_exposure_mode(
            requested_mode,
            "custom",
            portable_opt_in=True,
        ) == requested_mode


@pytest.mark.parametrize("mode", ["portable", "native_auto"])
def test_final_serialized_gate_fails_closed_when_required_tool_exceeds_hard_budget(mode):
    config = ToolExposureConfig(
        mode=mode,
        active_soft_chars=1_000,
        active_hard_chars=1_000,
    )
    runtime = assemble_tool_runtime(
        {
            "read": _tool("read", description="Required reader. " + "x" * 2_500),
            "capability_search": _tool("capability_search"),
        },
        mode=mode,
        agent_name="build",
        exposure_config=config,
    )
    assert runtime.budget_result is not None
    assert runtime.budget_result.hard_limit_exceeded is True
    metrics = measure_tool_definitions(dict(runtime.provider_tools), "responses")

    with pytest.raises(RuntimeError, match="portable hard budget"):
        enforce_serialized_payload_limits(
            exposure_mode=mode,
            catalogue_wire_chars=metrics.catalogue_wire_definition_chars,
            initial_visible_chars=metrics.initial_model_visible_definition_chars,
            native_wire_hard_chars=config.native_wire_hard_chars,
            active_hard_chars=config.active_hard_chars,
        )


@pytest.mark.parametrize("mode", ["legacy_eager", "shadow"])
def test_final_serialized_gate_keeps_migration_modes_warning_only(mode):
    enforce_serialized_payload_limits(
        exposure_mode=mode,
        catalogue_wire_chars=1_500,
        initial_visible_chars=1_500,
        native_wire_hard_chars=2_000,
        active_hard_chars=1_000,
    )


def test_final_serialized_gate_applies_provider_ceiling_to_every_mode():
    with pytest.raises(RuntimeError, match="provider ceiling"):
        enforce_serialized_payload_limits(
            exposure_mode="legacy_eager",
            catalogue_wire_chars=2_001,
            initial_visible_chars=1_500,
            native_wire_hard_chars=2_000,
            active_hard_chars=1_000,
        )
