from types import MappingProxyType

import pytest
from pydantic import BaseModel, Field

from agent.tool_exposure import (
    CatalogEntry,
    EligibleCatalog,
    ExposureSignals,
    build_eligible_catalog,
    legacy_eager_plan,
    preferred_editor_id,
    portable_plan,
    provider_tools_for_plan,
    route_explicit_intent_packs,
    route_intent_packs,
    route_product_state_packs,
    step_executable_ids,
)
from agent.tool_exposure_budget import ExposureBudgets, apply_exposure_budget
from tool.tool import ToolInfo, ToolResult


class Args(BaseModel):
    query: str = Field(description="Unique parameter marker")


async def _execute(args, ctx):
    return ToolResult(title="ok", output="ok")


def _tool(tool_id: str, **kwargs) -> ToolInfo:
    return ToolInfo(
        id=tool_id,
        description=kwargs.pop("description", f"Use {tool_id} for its reviewed task."),
        parameters=Args,
        execute=kwargs.pop("execute", _execute),
        **kwargs,
    )


def test_catalogue_is_stably_sorted_and_does_not_mutate_tools():
    second = _tool("zeta")
    first = _tool("alpha")
    source = {"zeta": second, "alpha": first}

    catalogue = build_eligible_catalog(source)

    assert tuple(catalogue.entries) == ("alpha", "zeta")
    assert tuple(catalogue.tools) == ("alpha", "zeta")
    assert source == {"zeta": second, "alpha": first}
    assert catalogue.entries["alpha"].parameter_names == ("query",)
    with pytest.raises(TypeError):
        catalogue.entries["other"] = catalogue.entries["alpha"]


def test_catalogue_generation_changes_with_schema_not_input_order():
    first = build_eligible_catalog({"b": _tool("b"), "a": _tool("a")})
    reordered = build_eligible_catalog({"a": _tool("a"), "b": _tool("b")})
    changed = build_eligible_catalog({
        "a": _tool("a", description="changed contract"),
        "b": _tool("b"),
    })

    assert first.generation == reordered.generation
    assert first.generation != changed.generation


def test_sandbox_entry_cannot_claim_pack_or_same_response_authority():
    common = dict(
        id="mcp:v2:" + "a" * 52,
        provider_name="mcp_example",
        discovery_hint="safe",
        parameter_names=(),
        source="mcp",
        plane="sandbox",
        schema_digest="d" * 64,
        schema_chars=10,
    )
    with pytest.raises(ValueError, match="intent pack"):
        CatalogEntry(**common, pack="video")
    with pytest.raises(ValueError, match="native reveal"):
        CatalogEntry(**common, pack=None, same_response_safe=True)


def test_mcp_identity_comes_from_registration_closure_and_hint_is_sanitized():
    canonical = "mcp:v2:" + "b" * 52

    async def mcp_execute(args, ctx):
        return ToolResult(title="ok", output="ok")

    mcp_execute._mcp_canonical_id = canonical
    tool = _tool(
        "mcp_legacy",
        execute=mcp_execute,
        description='<secret>& "remote"\x00' + "x" * 400,
        pack="video",
        same_response_safe=True,
    )
    catalogue = build_eligible_catalog({"mcp_legacy": tool})
    entry = catalogue.entries[canonical]

    assert entry.source == "mcp"
    assert entry.plane == "sandbox"
    assert entry.pack is None
    assert entry.same_response_safe is False
    assert len(entry.discovery_hint) <= 200
    assert "<secret>" not in entry.discovery_hint
    assert "&lt;secret&gt;" in entry.discovery_hint


def test_only_audited_builtin_ids_are_same_response_safe():
    catalogue = build_eligible_catalog({
        "read": _tool("read"),
        "image_gen": _tool("image_gen", same_response_safe=True),
    })
    assert catalogue.entries["read"].same_response_safe is True
    assert catalogue.entries["image_gen"].same_response_safe is False
    custom = build_eligible_catalog({
        "custom_read": _tool(
            "read",
            canonical_id="read",
            provider_name="custom_read",
            source="custom",
            plane="platform",
            same_response_safe=True,
        ),
    })
    assert custom.entries["read"].same_response_safe is False


def test_legacy_plan_preserves_the_complete_eligible_set():
    catalogue = build_eligible_catalog({"b": _tool("b"), "a": _tool("a")})
    plan = legacy_eager_plan(catalogue)

    assert plan.direct_ids == ("a", "b")
    assert plan.deferred_ids == ()
    assert set(provider_tools_for_plan(catalogue, plan)) == {"a", "b"}
    assert step_executable_ids(plan) == frozenset({"a", "b"})


def test_portable_plan_separates_resident_direct_and_hidden_execution():
    catalogue = build_eligible_catalog({
        name: _tool(name)
        for name in ("bash", "read", "skill", "capability_search", "image_gen", "cron")
    })
    plan = portable_plan(
        catalogue,
        agent_name="build",
        signals=ExposureSignals(user_task_text="Please generate an image"),
    )

    assert set(plan.direct_ids) == {"bash", "read", "skill", "capability_search", "image_gen"}
    assert plan.deferred_ids == ("cron",)
    assert plan.discovery_ids == ("cron",)
    assert "cron" not in step_executable_ids(plan)
    assert set(provider_tools_for_plan(catalogue, plan)) == set(plan.direct_ids)


def test_custom_agent_keeps_conditional_discovery_slots_resident():
    catalogue = build_eligible_catalog({
        name: _tool(name)
        for name in ("capability_search", "skill_search", "qa_deferred_echo")
    })
    plan = portable_plan(catalogue, agent_name="qa-custom")

    assert plan.direct_ids == ("capability_search", "skill_search")
    assert plan.deferred_ids == ("qa_deferred_echo",)
    assert plan.discovery_ids == ("qa_deferred_echo",)
    assert plan.reasons == {
        "capability_search": "resident",
        "skill_search": "resident",
    }


def test_custom_search_has_no_discovery_frontier_without_deferred_tools():
    catalogue = build_eligible_catalog({
        "capability_search": _tool("capability_search"),
    })
    plan = portable_plan(catalogue, agent_name="qa-custom")

    assert plan.direct_ids == ("capability_search",)
    assert plan.deferred_ids == ()
    assert plan.discovery_ids == ()


def test_automation_intent_is_direct_but_ordinary_code_is_not():
    assert "automation" in route_intent_packs(
        ExposureSignals(user_task_text="每天上午 9 点提醒我提交日报")
    )
    assert "automation" not in route_intent_packs(
        ExposureSignals(user_task_text="修复这个 Python 函数")
    )


def test_loading_video_skill_alone_does_not_route_video_pack():
    packs = route_intent_packs(ExposureSignals(
        user_task_text="只加载并总结 video-production skill，不执行视频",
    ))
    assert "video" not in packs


def test_negative_skill_clause_does_not_erase_real_video_task_on_same_line():
    packs = route_intent_packs(ExposureSignals(
        user_task_text=(
            "不要加载任何 skill。请直接创建一个视频项目并发起脚本审批。"
        ),
    ))
    assert "video" in packs


def test_english_negative_skill_sentence_keeps_following_real_task():
    packs = route_intent_packs(ExposureSignals(
        user_task_text="Do not load the video skill. Create a video project.",
    ))
    assert "video" in packs


def test_active_video_state_routes_even_without_video_words():
    signals = ExposureSignals(
        user_task_text="继续第三段",
        has_active_video_production=True,
    )
    assert "video" in route_product_state_packs(signals)
    assert "video" not in route_explicit_intent_packs(signals)
    assert "video" in route_intent_packs(signals)


def test_product_state_and_text_intent_get_distinct_plan_reasons():
    catalogue = build_eligible_catalog({
        name: _tool(name)
        for name in (
            "capability_search",
            "todo_write",
            "browser_mode",
            "share_file",
            "video_project",
            "web_search",
        )
    })
    plan = portable_plan(
        catalogue,
        agent_name="build",
        signals=ExposureSignals(
            user_task_text="research the latest docs",
            has_open_todos=True,
            has_active_video_job=True,
            browser_workflow_active=True,
            deliverable_asset_ids=("asset_1",),
        ),
    )

    assert plan.reasons["todo_write"] == "product:planning"
    assert plan.reasons["browser_mode"] == "product:browser"
    assert plan.reasons["share_file"] == "product:delivery"
    assert plan.reasons["video_project"] == "product:video"
    assert plan.reasons["web_search"] == "intent:research"


def test_explicit_video_text_remains_intent_without_recovery_state():
    catalogue = build_eligible_catalog({
        "capability_search": _tool("capability_search"),
        "video_project": _tool("video_project"),
    })
    plan = portable_plan(
        catalogue,
        agent_name="build",
        signals=ExposureSignals(user_task_text="生成一段视频"),
    )

    assert plan.reasons["video_project"] == "intent:video"


def test_product_recovery_pack_survives_adversarial_multi_intent_budgeting():
    catalogue = build_eligible_catalog({
        "capability_search": _tool("capability_search"),
        "video_project": _tool(
            "video_project", description="Resume project. " + "p" * 300
        ),
        "video_generate": _tool(
            "video_generate", description="Resume generation. " + "g" * 300
        ),
        "web_search": _tool(
            "web_search", description="Search current sources. " + "s" * 300
        ),
        "web_fetch": _tool(
            "web_fetch", description="Fetch current source. " + "f" * 300
        ),
    })
    plan = portable_plan(
        catalogue,
        agent_name="build",
        signals=ExposureSignals(
            user_task_text="research and verify the latest docs",
            has_active_video_production=True,
        ),
        # Recovery state must outrank historical discovery evidence.
        revealed_ids={"video_project"},
    )
    assert plan.reasons["video_project"] == "product:video"
    assert plan.reasons["video_generate"] == "product:video"
    assert plan.reasons["web_search"] == "intent:research"
    assert plan.reasons["web_fetch"] == "intent:research"

    result = apply_exposure_budget(
        catalogue,
        plan,
        budgets=ExposureBudgets(
            resident_soft_chars=50,
            resident_hard_chars=75,
            visible_soft_chars=100,
            visible_hard_chars=150,
            single_tool_soft_chars=50,
            single_tool_hard_chars=100,
            intent_pack_soft_chars=100,
            intent_pack_hard_chars=150,
            catalogue_wire_soft_chars=10_000,
            catalogue_wire_hard_chars=20_000,
        ),
    )

    assert {"video_project", "video_generate"}.issubset(result.plan.direct_ids)
    assert {"web_search", "web_fetch"}.issubset(result.trimmed_ids)
    assert result.hard_limit_exceeded is True
    assert all(
        result.deferred_reasons[tool_id] == "single_tool_hard_limit"
        for tool_id in ("web_search", "web_fetch")
    )


def test_model_family_selects_exactly_one_available_editor():
    eligible = {"edit", "apply_patch", "read"}
    assert preferred_editor_id("openai/gpt-5.4", eligible) == "apply_patch"
    assert preferred_editor_id("anthropic/claude-sonnet", eligible) == "edit"
    assert preferred_editor_id("unknown", {"apply_patch"}) == "apply_patch"
    assert preferred_editor_id("unknown", {"read"}) is None


def test_eligible_catalog_rejects_duplicate_canonical_identity():
    first = _tool("wire_a", canonical_id="same")
    second = _tool("wire_b", canonical_id="same")
    with pytest.raises(ValueError, match="duplicate canonical"):
        build_eligible_catalog({"wire_a": first, "wire_b": second})


def test_manual_catalog_requires_identical_tool_and_entry_keys():
    with pytest.raises(ValueError, match="identical stable ids"):
        EligibleCatalog(
            tools=MappingProxyType({"a": _tool("a")}),
            entries=MappingProxyType({}),
            generation="generation",
        )
