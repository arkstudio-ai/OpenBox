"""Pure, deterministic budgeting for provider-visible tool definitions.

The planner decides which tools are relevant; this module only applies size
budgets to that immutable decision.  It never edits ``ToolInfo`` objects,
descriptions, or JSON schemas.  A definition either remains whole or moves to
discovery.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping

if TYPE_CHECKING:
    from agent.tool_exposure import EligibleCatalog, ExposurePlan
    from core.config import ToolExposureConfig


Priority = Literal["resident", "product_pinned", "explicit_intent", "revealed", "other"]
CatalogueDecision = Literal[
    "ok",
    "warn",
    "fallback_portable",
    "fallback_meta",
    "fail_closed",
]

_PRIORITY_ORDER: Mapping[Priority, int] = MappingProxyType({
    "resident": 0,
    "product_pinned": 1,
    "explicit_intent": 2,
    "revealed": 3,
    "other": 4,
})
_NO_TRIM_STRATEGIES = frozenset({"legacy_eager", "shadow", "emergency_eager"})


def _freeze(values: Mapping) -> Mapping:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class ExposureBudgets:
    """Provider-neutral character budgets used by the pure selector.

    The model-visible soft target is 24K and the hard boundary is 32K.  The
    resident core has its own 20K/24K observability thresholds but is never
    truncated at runtime.
    """

    resident_soft_chars: int = 20_000
    resident_hard_chars: int = 24_000
    visible_soft_chars: int = 28_000
    visible_hard_chars: int = 32_000
    single_tool_soft_chars: int = 2_500
    single_tool_hard_chars: int = 5_000
    intent_pack_soft_chars: int = 10_000
    intent_pack_hard_chars: int = 12_000
    catalogue_wire_soft_chars: int = 96_000
    catalogue_wire_hard_chars: int = 128_000

    def __post_init__(self) -> None:
        pairs = (
            ("resident", self.resident_soft_chars, self.resident_hard_chars),
            ("visible", self.visible_soft_chars, self.visible_hard_chars),
            ("single_tool", self.single_tool_soft_chars, self.single_tool_hard_chars),
            ("intent_pack", self.intent_pack_soft_chars, self.intent_pack_hard_chars),
            (
                "catalogue_wire",
                self.catalogue_wire_soft_chars,
                self.catalogue_wire_hard_chars,
            ),
        )
        for name, soft, hard in pairs:
            if soft < 0 or hard < 0:
                raise ValueError(f"{name} budgets must be non-negative")
            if soft > hard:
                raise ValueError(f"{name} soft budget must not exceed hard budget")

    @classmethod
    def from_config(cls, config: "ToolExposureConfig") -> "ExposureBudgets":
        """Adapt the rollout config without coupling selection to Pydantic.

        Resident definitions have their own 20K/24K budget.  Once an intent
        pack or a persisted reveal is added, the combined request uses the
        independently configured 28K/32K active budget.
        """

        return cls(
            resident_soft_chars=config.resident_soft_chars,
            resident_hard_chars=config.resident_hard_chars,
            visible_soft_chars=config.active_soft_chars,
            visible_hard_chars=config.active_hard_chars,
            single_tool_soft_chars=config.single_tool_soft_chars,
            single_tool_hard_chars=config.single_tool_hard_chars,
            intent_pack_soft_chars=config.intent_pack_soft_chars,
            intent_pack_hard_chars=config.intent_pack_hard_chars,
            catalogue_wire_soft_chars=config.native_wire_soft_chars,
            catalogue_wire_hard_chars=config.native_wire_hard_chars,
        )


@dataclass(frozen=True)
class ToolBudgetDecision:
    tool_id: str
    item_chars: int
    priority: Priority
    action: Literal["kept", "deferred", "already_deferred"]
    reason: str


@dataclass(frozen=True)
class ExposureBudgetResult:
    """Immutable result of applying budgets to one exposure plan."""

    plan: "ExposurePlan"
    decisions: tuple[ToolBudgetDecision, ...]
    trimmed_ids: tuple[str, ...]
    deferred_reasons: Mapping[str, str]
    visible_chars: int
    resident_chars: int
    pack_chars: Mapping[str, int]
    catalogue_wire_chars: int
    catalogue_decision: CatalogueDecision
    soft_limit_exceeded: bool
    hard_limit_exceeded: bool
    warnings: tuple[str, ...]
    catalog_generation: str
    budgets: ExposureBudgets

    def __post_init__(self) -> None:
        object.__setattr__(self, "deferred_reasons", _freeze(self.deferred_reasons))
        object.__setattr__(self, "pack_chars", _freeze(self.pack_chars))


def _priority(reason: str) -> Priority:
    normalized = (reason or "").strip().lower()
    if normalized in {
        "resident",
        "core",
        "required",
        "model_editor",
        "synthetic",
        "structured_output",
    } or normalized.startswith(("resident:", "core:", "required:", "synthetic:")):
        return "resident"
    if normalized in {
        "product",
        "product_pinned",
        "product-state",
        "product_state",
        "pinned",
        "recovery",
    } or normalized.startswith(
        (
            "product:",
            "product_state:",
            "product-state:",
            "pinned:",
            "recovery:",
        )
    ):
        return "product_pinned"
    if normalized in {"intent", "explicit", "explicit_intent"} or normalized.startswith(
        ("intent:", "explicit:")
    ):
        return "explicit_intent"
    if normalized == "revealed" or normalized.startswith("revealed:"):
        return "revealed"
    return "other"


def _pack_name(reason: str, entry) -> str | None:
    normalized = (reason or "").strip().lower()
    prefixes = (
        "product_state:",
        "product-state:",
        "product:",
        "pinned:",
        "intent:",
        "explicit:",
    )
    for prefix in prefixes:
        if normalized.startswith(prefix):
            value = normalized[len(prefix):].strip()
            return value or None
    value = getattr(entry, "pack", None)
    return str(value) if value else None


def _validate_inputs(catalog: "EligibleCatalog", plan: "ExposurePlan") -> None:
    eligible = set(catalog.entries)
    direct = set(plan.direct_ids)
    deferred = set(plan.deferred_ids)
    discovery = set(plan.discovery_ids)
    unknown = (direct | deferred | discovery) - eligible
    if unknown:
        raise ValueError(f"exposure plan references unknown tool ids: {sorted(unknown)}")
    missing = eligible - (direct | deferred)
    if missing:
        raise ValueError(f"exposure plan omits eligible tool ids: {sorted(missing)}")
    if discovery - deferred:
        raise ValueError("discovery ids must be deferred in the input plan")


def _catalogue_wire_ids(strategy: str, catalog, budgeted_plan) -> tuple[str, ...]:
    normalized = (strategy or "").lower()
    if normalized in _NO_TRIM_STRATEGIES or normalized.startswith("native"):
        return tuple(sorted(catalog.entries))
    return tuple(sorted(budgeted_plan.direct_ids))


def _catalogue_decision(
    strategy: str,
    wire_chars: int,
    budgets: ExposureBudgets,
) -> CatalogueDecision:
    if wire_chars <= budgets.catalogue_wire_soft_chars:
        return "ok"
    if wire_chars <= budgets.catalogue_wire_hard_chars:
        return "warn"
    normalized = (strategy or "").lower()
    if normalized.startswith("native"):
        return "fallback_portable"
    if normalized == "portable":
        return "fallback_meta"
    return "fail_closed"


def _decision_reason(priority: Priority) -> str:
    return {
        "resident": "required_resident",
        "product_pinned": "required_product_pinned",
        "explicit_intent": "selected_explicit_intent",
        "revealed": "selected_revealed",
        "other": "selected_other",
    }[priority]


def apply_exposure_budget(
    catalog: "EligibleCatalog",
    plan: "ExposurePlan",
    config: "ToolExposureConfig | None" = None,
    *,
    budgets: ExposureBudgets | None = None,
) -> ExposureBudgetResult:
    """Return a whole-item budgeted plan without mutating either input.

    Resident and product-state-pinned tools are mandatory.  Explicit intent
    may consume burst capacity up to the 32K hard boundary; historical reveals
    and other candidates only fill the 24K soft target.  Anything removed is
    added to discovery with a machine-readable reason.
    """

    if config is not None and budgets is not None:
        raise ValueError("pass config or budgets, not both")
    active_budgets = budgets or (
        ExposureBudgets.from_config(config) if config is not None else ExposureBudgets()
    )
    _validate_inputs(catalog, plan)

    original_reasons = dict(plan.reasons)
    classified = {
        tool_id: _priority(original_reasons.get(tool_id, ""))
        for tool_id in plan.direct_ids
    }
    candidates = sorted(
        plan.direct_ids,
        key=lambda tool_id: (_PRIORITY_ORDER[classified[tool_id]], tool_id),
    )
    no_trim = (plan.strategy or "").lower() in _NO_TRIM_STRATEGIES

    kept: set[str] = set()
    trimmed: dict[str, str] = {}
    pack_totals: dict[str, int] = {}
    visible_chars = 0

    for tool_id in candidates:
        entry = catalog.entries[tool_id]
        size = entry.schema_chars
        priority = classified[tool_id]
        required = priority in {"resident", "product_pinned"}
        pack = _pack_name(original_reasons.get(tool_id, ""), entry)

        if no_trim or required:
            kept.add(tool_id)
            visible_chars += size
            if pack:
                pack_totals[pack] = pack_totals.get(pack, 0) + size
            continue

        if size > active_budgets.single_tool_hard_chars:
            trimmed[tool_id] = "single_tool_hard_limit"
            continue

        if pack and pack_totals.get(pack, 0) + size > active_budgets.intent_pack_hard_chars:
            trimmed[tool_id] = f"intent_pack_hard_limit:{pack}"
            continue

        total_limit = (
            active_budgets.visible_hard_chars
            if priority == "explicit_intent"
            else active_budgets.visible_soft_chars
        )
        if visible_chars + size > total_limit:
            limit_name = "hard" if priority == "explicit_intent" else "soft"
            trimmed[tool_id] = f"initial_visible_{limit_name}_limit"
            continue

        kept.add(tool_id)
        visible_chars += size
        if pack:
            pack_totals[pack] = pack_totals.get(pack, 0) + size

    trimmed_ids = tuple(sorted(trimmed))
    direct_ids = tuple(sorted(kept))
    deferred_ids = tuple(sorted(set(plan.deferred_ids) | set(trimmed_ids)))
    discovery_ids = tuple(sorted(set(plan.discovery_ids) | set(trimmed_ids)))
    budgeted_reasons = {
        tool_id: original_reasons[tool_id]
        for tool_id in direct_ids
        if tool_id in original_reasons
    }
    budgeted_plan = plan if no_trim else replace(
        plan,
        direct_ids=direct_ids,
        deferred_ids=deferred_ids,
        discovery_ids=discovery_ids,
        reasons=budgeted_reasons,
        schema_chars=visible_chars,
    )

    # Recalculate from the actual output for legacy/shadow, whose original
    # plan and ordering are intentionally preserved byte-for-byte.
    visible_chars = sum(
        catalog.entries[tool_id].schema_chars for tool_id in budgeted_plan.direct_ids
    )
    resident_chars = sum(
        catalog.entries[tool_id].schema_chars
        for tool_id in budgeted_plan.direct_ids
        if _priority(original_reasons.get(tool_id, "")) == "resident"
    )
    final_pack_totals: dict[str, int] = {}
    for tool_id in budgeted_plan.direct_ids:
        pack = _pack_name(original_reasons.get(tool_id, ""), catalog.entries[tool_id])
        if pack:
            final_pack_totals[pack] = (
                final_pack_totals.get(pack, 0) + catalog.entries[tool_id].schema_chars
            )

    decisions: list[ToolBudgetDecision] = []
    for tool_id in sorted(catalog.entries):
        size = catalog.entries[tool_id].schema_chars
        priority = _priority(original_reasons.get(tool_id, ""))
        if tool_id in trimmed:
            action = "deferred"
            reason = trimmed[tool_id]
        elif tool_id in kept or (no_trim and tool_id in plan.direct_ids):
            action = "kept"
            reason = "legacy_no_trim" if no_trim else _decision_reason(priority)
        else:
            action = "already_deferred"
            reason = "input_deferred"
        decisions.append(ToolBudgetDecision(tool_id, size, priority, action, reason))

    strategy = plan.strategy or "portable"
    wire_ids = _catalogue_wire_ids(strategy, catalog, budgeted_plan)
    catalogue_wire_chars = sum(catalog.entries[tool_id].schema_chars for tool_id in wire_ids)
    catalogue_decision = _catalogue_decision(
        strategy,
        catalogue_wire_chars,
        active_budgets,
    )

    warnings: list[str] = []
    for tool_id in sorted(plan.direct_ids):
        size = catalog.entries[tool_id].schema_chars
        priority = _priority(original_reasons.get(tool_id, ""))
        if size > active_budgets.single_tool_hard_chars:
            if priority in {"resident", "product_pinned"}:
                suffix = "required_kept"
            elif no_trim:
                suffix = "legacy_kept"
            else:
                suffix = "deferred"
            warnings.append(
                f"single_tool_hard_{suffix}:{tool_id}:{size}>"
                f"{active_budgets.single_tool_hard_chars}"
            )
        elif size > active_budgets.single_tool_soft_chars:
            warnings.append(
                f"single_tool_soft:{tool_id}:{size}>{active_budgets.single_tool_soft_chars}"
            )
    if resident_chars > active_budgets.resident_hard_chars:
        warnings.append(
            f"resident_hard:{resident_chars}>{active_budgets.resident_hard_chars}"
        )
    elif resident_chars > active_budgets.resident_soft_chars:
        warnings.append(
            f"resident_soft:{resident_chars}>{active_budgets.resident_soft_chars}"
        )
    for pack, chars in sorted(final_pack_totals.items()):
        if chars > active_budgets.intent_pack_hard_chars:
            warnings.append(
                f"intent_pack_hard_kept:{pack}:{chars}>"
                f"{active_budgets.intent_pack_hard_chars}"
            )
        elif chars > active_budgets.intent_pack_soft_chars:
            warnings.append(
                f"intent_pack_soft:{pack}:{chars}>{active_budgets.intent_pack_soft_chars}"
            )
    if visible_chars > active_budgets.visible_hard_chars:
        warnings.append(
            f"initial_visible_hard:{visible_chars}>{active_budgets.visible_hard_chars}"
        )
    elif visible_chars > active_budgets.visible_soft_chars:
        warnings.append(
            f"initial_visible_soft:{visible_chars}>{active_budgets.visible_soft_chars}"
        )
    if catalogue_decision != "ok":
        catalogue_cap = (
            active_budgets.catalogue_wire_soft_chars
            if catalogue_decision == "warn"
            else active_budgets.catalogue_wire_hard_chars
        )
        warnings.append(
            f"catalogue_wire_{catalogue_decision}:{catalogue_wire_chars}>"
            f"{catalogue_cap}"
        )

    return ExposureBudgetResult(
        plan=budgeted_plan,
        decisions=tuple(decisions),
        trimmed_ids=trimmed_ids,
        deferred_reasons=trimmed,
        visible_chars=visible_chars,
        resident_chars=resident_chars,
        pack_chars=final_pack_totals,
        catalogue_wire_chars=catalogue_wire_chars,
        catalogue_decision=catalogue_decision,
        soft_limit_exceeded=visible_chars > active_budgets.visible_soft_chars,
        hard_limit_exceeded=visible_chars > active_budgets.visible_hard_chars,
        warnings=tuple(sorted(set(warnings))),
        catalog_generation=catalog.generation,
        budgets=active_budgets,
    )


# A noun-first alias reads better at call sites that already have a plan.
budget_exposure_plan = apply_exposure_budget
