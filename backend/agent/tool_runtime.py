"""Assemble the four distinct tool sets consumed by one agent step."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from agent.tool_exposure import (
    EligibleCatalog,
    ExposurePlan,
    ExposureSignals,
    build_eligible_catalog,
    legacy_eager_plan,
    portable_plan,
    provider_tools_for_plan,
)
from tool.tool import ToolInfo

try:  # Imported only for annotations/adapter; keeps the pure module lightweight.
    from agent.tool_exposure_budget import ExposureBudgetResult
except ImportError:  # pragma: no cover
    ExposureBudgetResult = object  # type: ignore[misc,assignment]


@dataclass(frozen=True)
class ToolRuntime:
    """One immutable binding between catalogue, provider and executor."""

    eligible_catalog: EligibleCatalog
    provider_plan: ExposurePlan
    provider_tools: Mapping[str, ToolInfo]
    execution_lookup: Mapping[str, ToolInfo]
    step_executable_ids: frozenset[str]
    provider_to_canonical: Mapping[str, str]
    candidate_plan: ExposurePlan | None = None
    budget_result: "ExposureBudgetResult | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_tools", MappingProxyType(dict(self.provider_tools)))
        object.__setattr__(self, "execution_lookup", MappingProxyType(dict(self.execution_lookup)))
        object.__setattr__(
            self,
            "provider_to_canonical",
            MappingProxyType(dict(self.provider_to_canonical)),
        )
        if not self.step_executable_ids <= set(self.execution_lookup):
            raise ValueError("step executable ids must exist in execution lookup")
        unknown_provider_names = set(self.provider_tools) - set(self.provider_to_canonical)
        if unknown_provider_names:
            raise ValueError("every provider definition needs a canonical binding")

    def canonical_id_for_provider_name(self, name: str) -> str | None:
        return self.provider_to_canonical.get(name)


def effective_exposure_mode(
    requested_mode: str,
    agent_name: str,
    *,
    portable_opt_in: bool = False,
) -> str:
    """Resolve one rollout mode before any step-specific exposure work.

    Migration and emergency modes deliberately remain global.  Once portable
    exposure is requested, the top-level build agent and config-defined agents
    that explicitly opted into discovery participate. Every other agent keeps
    the eager wire while computing the portable candidate in shadow.
    """
    if requested_mode in {"legacy_eager", "shadow", "emergency_eager"}:
        return requested_mode
    if requested_mode in {"portable", "native_auto"}:
        return (
            requested_mode
            if agent_name == "build" or portable_opt_in
            else "shadow"
        )
    raise ValueError(f"unsupported tool exposure mode: {requested_mode}")


def enforce_serialized_payload_limits(
    *,
    exposure_mode: str,
    catalogue_wire_chars: int,
    initial_visible_chars: int,
    native_wire_hard_chars: int,
    active_hard_chars: int,
) -> None:
    """Fail closed from the final provider payload, after serialization.

    Planner budget state is deliberately not an input. Resident and recovery
    tools may be preserved intact by planning even when they exceed a target,
    but that preservation must never bypass the portable/native wire gate.
    Legacy and shadow remain observation-only below the global provider
    catalogue ceiling during migration.
    """
    if catalogue_wire_chars > native_wire_hard_chars:
        raise RuntimeError(
            "Tool definition catalogue exceeds the configured provider ceiling "
            f"({catalogue_wire_chars} > {native_wire_hard_chars} chars)"
        )
    if (
        exposure_mode in {"portable", "native_auto"}
        and initial_visible_chars > active_hard_chars
    ):
        raise RuntimeError(
            "Initial tool definitions exceed the portable hard budget "
            f"({initial_visible_chars} > {active_hard_chars} chars)"
        )


def assemble_tool_runtime(
    eligible_tools: Mapping[str, ToolInfo],
    *,
    mode: str,
    agent_name: str,
    signals: ExposureSignals = ExposureSignals(),
    revealed_ids: Iterable[str] = (),
    editor_id: str | None = None,
    synthetic_tools: Mapping[str, ToolInfo] | None = None,
    exposure_config=None,
) -> ToolRuntime:
    """Build a step runtime without database, provider, Skill or sandbox I/O."""
    catalogue = build_eligible_catalog(eligible_tools)
    synthetic = dict(synthetic_tools or {})

    if mode in {"legacy_eager", "emergency_eager"}:
        provider_plan = legacy_eager_plan(catalogue, strategy=mode)
        candidate_plan = None
    elif mode == "shadow":
        eager_plan = legacy_eager_plan(catalogue, strategy="shadow_wire_eager")
        # Shadow needs the logical discovery slot in its candidate catalogue
        # so the measurements describe the portable plan we could actually
        # roll out. The production wire must nevertheless remain byte-for-
        # byte compatible with legacy eager, where capability_search was not
        # registered for the provider at all.
        provider_direct_ids = tuple(
            tool_id
            for tool_id in eager_plan.direct_ids
            if tool_id != "capability_search"
        )
        provider_plan = ExposurePlan(
            direct_ids=provider_direct_ids,
            deferred_ids=(),
            discovery_ids=(),
            reasons={
                tool_id: eager_plan.reasons[tool_id]
                for tool_id in provider_direct_ids
            },
            strategy=eager_plan.strategy,
            schema_chars=sum(
                catalogue.entries[tool_id].schema_chars
                for tool_id in provider_direct_ids
            ),
        )
        candidate_plan = portable_plan(
            catalogue,
            agent_name=agent_name,
            signals=signals,
            revealed_ids=revealed_ids,
            editor_id=editor_id,
        )
    elif mode in {"portable", "native_auto"}:
        # Native adapters are capability-gated later. Until a binding proves
        # request+stream+replay support, native_auto deliberately uses the
        # provider-neutral portable plan.
        provider_plan = portable_plan(
            catalogue,
            agent_name=agent_name,
            signals=signals,
            revealed_ids=revealed_ids,
            editor_id=editor_id,
        )
        candidate_plan = None
    else:
        raise ValueError(f"unsupported tool exposure mode: {mode}")

    from agent.tool_exposure_budget import apply_exposure_budget

    budget_result = apply_exposure_budget(
        catalogue,
        candidate_plan if mode == "shadow" and candidate_plan is not None else provider_plan,
        exposure_config,
    )
    if mode == "shadow":
        # Candidate metrics/decisions are available, but shadow must preserve
        # the production eager wire exactly.
        candidate_plan = budget_result.plan
    else:
        provider_plan = budget_result.plan

    provider_tools = provider_tools_for_plan(catalogue, provider_plan)
    execution_lookup = dict(catalogue.tools)
    step_ids = set(provider_plan.direct_ids)
    provider_to_canonical = {
        entry.provider_name: tool_id for tool_id, entry in catalogue.entries.items()
    }

    # Structured output and other request-local synthetic tools are direct in
    # all modes but never enter the persistent/discoverable catalogue.
    for provider_name, tool in sorted(synthetic.items()):
        canonical_id = tool.canonical_id or tool.id or provider_name
        if canonical_id in execution_lookup or provider_name in provider_tools:
            raise ValueError("synthetic tool identity collides with eligible catalogue")
        tool.source = "synthetic"
        tool.plane = "platform"
        tool.canonical_id = canonical_id
        tool.provider_name = provider_name
        tool.pack = None
        tool.same_response_safe = False
        execution_lookup[canonical_id] = tool
        provider_tools[provider_name] = tool
        provider_to_canonical[provider_name] = canonical_id
        step_ids.add(canonical_id)

    return ToolRuntime(
        eligible_catalog=catalogue,
        provider_plan=provider_plan,
        provider_tools=provider_tools,
        execution_lookup=execution_lookup,
        step_executable_ids=frozenset(step_ids),
        provider_to_canonical=provider_to_canonical,
        candidate_plan=candidate_plan,
        budget_result=budget_result,
    )
