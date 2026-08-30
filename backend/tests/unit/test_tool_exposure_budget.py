"""Whole-schema, priority-stable tool exposure budgeting."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent.tool_exposure import CatalogEntry, EligibleCatalog, ExposurePlan
from agent.tool_exposure_budget import (
    ExposureBudgets,
    apply_exposure_budget,
)
from core.config import ToolExposureConfig


def _catalog(sizes: dict[str, int], *, order: tuple[str, ...] | None = None) -> EligibleCatalog:
    ordered = order or tuple(sizes)
    tools = {}
    entries = {}
    for tool_id in ordered:
        # The sentinel schema proves the budgeter never trims the source object.
        tools[tool_id] = SimpleNamespace(
            id=tool_id,
            description=f"description:{tool_id}",
            raw_schema={
                "type": "object",
                "properties": {f"sentinel_{tool_id}": {"type": "string"}},
            },
        )
        entries[tool_id] = CatalogEntry(
            id=tool_id,
            provider_name=tool_id,
            discovery_hint=f"hint:{tool_id}",
            parameter_names=(f"sentinel_{tool_id}",),
            source="builtin",
            plane="platform",
            pack=None,
            schema_digest=f"digest:{tool_id}",
            schema_chars=sizes[tool_id],
        )
    return EligibleCatalog(tools=tools, entries=entries, generation="generation-1")


def _plan(
    catalog: EligibleCatalog,
    direct: tuple[str, ...],
    reasons: dict[str, str],
    *,
    strategy: str = "portable",
) -> ExposurePlan:
    deferred = tuple(tool_id for tool_id in catalog.entries if tool_id not in set(direct))
    return ExposurePlan(
        direct_ids=direct,
        deferred_ids=deferred,
        discovery_ids=deferred,
        reasons=reasons,
        strategy=strategy,
        schema_chars=sum(catalog.entries[tool_id].schema_chars for tool_id in direct),
    )


def _budgets(**overrides) -> ExposureBudgets:
    values = {
        "resident_soft_chars": 18,
        "resident_hard_chars": 24,
        "visible_soft_chars": 24,
        "visible_hard_chars": 32,
        "single_tool_soft_chars": 10,
        "single_tool_hard_chars": 20,
        "intent_pack_soft_chars": 10,
        "intent_pack_hard_chars": 16,
        "catalogue_wire_soft_chars": 80,
        "catalogue_wire_hard_chars": 100,
    }
    values.update(overrides)
    return ExposureBudgets(**values)


def test_config_adapter_keeps_resident_and_active_budgets_distinct():
    budgets = ExposureBudgets.from_config(ToolExposureConfig())

    assert budgets.resident_soft_chars == 20_000
    assert budgets.resident_hard_chars == 24_000
    assert budgets.visible_soft_chars == 28_000
    assert budgets.visible_hard_chars == 32_000
    assert budgets.visible_hard_chars == 32_000
    assert budgets.single_tool_soft_chars == 2_500
    assert budgets.single_tool_hard_chars == 5_000
    assert budgets.intent_pack_hard_chars == 12_000
    assert budgets.catalogue_wire_hard_chars == 128_000


def test_priority_is_resident_then_product_then_intent_then_reveal_then_other():
    catalog = _catalog({"core": 10, "recovery": 8, "intent": 10, "revealed": 4, "other": 2})
    # Worst-first input catches implementations that slice direct_ids by position.
    plan = _plan(
        catalog,
        ("other", "revealed", "intent", "recovery", "core"),
        {
            "other": "candidate",
            "revealed": "revealed",
            "intent": "explicit_intent",
            "recovery": "product_pinned",
            "core": "core",
        },
    )

    result = apply_exposure_budget(catalog, plan, budgets=_budgets())

    assert result.plan.direct_ids == ("core", "intent", "recovery")
    assert result.trimmed_ids == ("other", "revealed")
    assert result.deferred_reasons == {
        "other": "initial_visible_soft_limit",
        "revealed": "initial_visible_soft_limit",
    }
    assert result.visible_chars == 28
    assert result.soft_limit_exceeded is True
    assert result.hard_limit_exceeded is False
    assert {"other", "revealed"}.issubset(result.plan.discovery_ids)


def test_core_and_paid_recovery_survive_even_when_they_exceed_global_hard_limit():
    catalog = _catalog({"intent": 2, "recovery": 18, "core": 18})
    plan = _plan(
        catalog,
        ("intent", "recovery", "core"),
        {
            "intent": "intent:research",
            "recovery": "recovery:paid-video",
            "core": "core:build",
        },
    )

    result = apply_exposure_budget(catalog, plan, budgets=_budgets(visible_hard_chars=30))

    assert result.plan.direct_ids == ("core", "recovery")
    assert result.deferred_reasons["intent"] == "initial_visible_hard_limit"
    assert result.visible_chars == 36
    assert result.hard_limit_exceeded is True
    assert any(warning.startswith("initial_visible_hard:") for warning in result.warnings)


def test_explicit_pack_is_capped_as_whole_items_not_sliced_bytes():
    catalog = _catalog({"alpha": 7, "beta": 7})
    plan = _plan(
        catalog,
        ("beta", "alpha"),
        {"alpha": "intent:video", "beta": "intent:video"},
    )

    result = apply_exposure_budget(
        catalog,
        plan,
        budgets=_budgets(intent_pack_soft_chars=8, intent_pack_hard_chars=12),
    )

    assert result.plan.direct_ids == ("alpha",)
    assert result.pack_chars == {"video": 7}
    assert result.deferred_reasons == {"beta": "intent_pack_hard_limit:video"}
    assert "beta" in result.plan.discovery_ids


def test_product_pinned_pack_is_not_sacrificed_to_the_pack_target():
    catalog = _catalog({"status": 8, "cancel": 8, "new_job": 2})
    plan = _plan(
        catalog,
        ("new_job", "status", "cancel"),
        {
            "status": "product:video",
            "cancel": "product:video",
            "new_job": "intent:video",
        },
    )

    result = apply_exposure_budget(
        catalog,
        plan,
        budgets=_budgets(intent_pack_soft_chars=10, intent_pack_hard_chars=12),
    )

    assert result.plan.direct_ids == ("cancel", "status")
    assert result.pack_chars == {"video": 16}
    assert result.deferred_reasons == {"new_job": "intent_pack_hard_limit:video"}
    assert any(warning.startswith("intent_pack_hard_kept:video:16>") for warning in result.warnings)


def test_oversized_nonrequired_item_moves_whole_to_discovery_without_source_mutation():
    catalog = _catalog({"oversized": 21, "core": 3})
    plan = _plan(
        catalog,
        ("oversized", "core"),
        {"oversized": "intent:research", "core": "resident"},
    )
    source_tool = catalog.tools["oversized"]
    source_schema = deepcopy(source_tool.raw_schema)
    source_entry = catalog.entries["oversized"]

    result = apply_exposure_budget(catalog, plan, budgets=_budgets(single_tool_hard_chars=20))

    assert result.plan.direct_ids == ("core",)
    assert result.deferred_reasons == {"oversized": "single_tool_hard_limit"}
    assert "oversized" in result.plan.discovery_ids
    assert catalog.tools["oversized"] is source_tool
    assert catalog.tools["oversized"].raw_schema == source_schema
    assert catalog.entries["oversized"] is source_entry
    assert catalog.entries["oversized"].schema_chars == 21


def test_oversized_required_item_is_kept_intact_and_reported():
    catalog = _catalog({"core": 21})
    plan = _plan(catalog, ("core",), {"core": "resident"})

    result = apply_exposure_budget(catalog, plan, budgets=_budgets(single_tool_hard_chars=20))

    assert result.plan.direct_ids == ("core",)
    assert result.trimmed_ids == ()
    assert any(
        warning.startswith("single_tool_hard_required_kept:core:21>")
        for warning in result.warnings
    )


@pytest.mark.parametrize("strategy", ["legacy_eager", "shadow"])
def test_migration_modes_warn_but_never_trim(strategy):
    catalog = _catalog({"low": 20, "core": 20})
    plan = _plan(
        catalog,
        ("low", "core"),
        {"low": "other", "core": "resident"},
        strategy=strategy,
    )

    result = apply_exposure_budget(
        catalog,
        plan,
        budgets=_budgets(visible_soft_chars=10, visible_hard_chars=12),
    )

    assert result.plan is plan
    assert result.plan.direct_ids == ("low", "core")
    assert result.trimmed_ids == ()
    assert result.hard_limit_exceeded is True
    assert any(decision.reason == "legacy_no_trim" for decision in result.decisions)


def test_native_catalogue_over_128k_equivalent_requests_portable_fallback():
    catalog = _catalog({"direct": 60, "deferred": 60})
    plan = _plan(
        catalog,
        ("direct",),
        {"direct": "resident"},
        strategy="native_auto",
    )

    result = apply_exposure_budget(
        catalog,
        plan,
        budgets=_budgets(
            visible_soft_chars=100,
            visible_hard_chars=100,
            single_tool_hard_chars=100,
        ),
    )

    assert result.catalogue_wire_chars == 120
    assert result.catalogue_decision == "fallback_portable"


def test_eager_catalogue_over_provider_ceiling_is_fail_closed_signal():
    catalog = _catalog({"one": 60, "two": 60})
    plan = _plan(
        catalog,
        ("one", "two"),
        {"one": "resident", "two": "resident"},
        strategy="legacy_eager",
    )

    result = apply_exposure_budget(catalog, plan, budgets=_budgets())

    assert result.plan is plan
    assert result.catalogue_wire_chars == 120
    assert result.catalogue_decision == "fail_closed"


def test_selection_is_stable_across_mapping_and_plan_order():
    sizes = {"core": 10, "intent": 10, "revealed": 10}
    reasons = {"core": "resident", "intent": "intent:research", "revealed": "revealed"}
    forward = _catalog(sizes, order=("core", "intent", "revealed"))
    reverse = _catalog(sizes, order=("revealed", "intent", "core"))
    plan_a = _plan(forward, ("revealed", "intent", "core"), reasons)
    plan_b = _plan(reverse, ("core", "intent", "revealed"), reasons)

    result_a = apply_exposure_budget(forward, plan_a, budgets=_budgets(visible_soft_chars=20))
    result_b = apply_exposure_budget(reverse, plan_b, budgets=_budgets(visible_soft_chars=20))

    assert result_a.plan.direct_ids == result_b.plan.direct_ids == ("core", "intent")
    assert result_a.trimmed_ids == result_b.trimmed_ids == ("revealed",)
    assert result_a.decisions == result_b.decisions


def test_result_mappings_and_records_are_immutable():
    catalog = _catalog({"core": 3, "low": 30})
    plan = _plan(catalog, ("core", "low"), {"core": "resident", "low": "other"})
    result = apply_exposure_budget(catalog, plan, budgets=_budgets())

    with pytest.raises(TypeError):
        result.deferred_reasons["new"] = "reason"
    with pytest.raises(TypeError):
        result.pack_chars["new"] = 1
    with pytest.raises(FrozenInstanceError):
        result.visible_chars = 0


def test_budgeter_rejects_a_plan_that_silently_omits_catalogue_entries():
    catalog = _catalog({"core": 3, "missing": 3})
    plan = ExposurePlan(
        direct_ids=("core",),
        deferred_ids=(),
        discovery_ids=(),
        reasons={"core": "resident"},
        strategy="portable",
        schema_chars=3,
    )

    with pytest.raises(ValueError, match="omits eligible tool ids"):
        apply_exposure_budget(catalog, plan, budgets=_budgets())
