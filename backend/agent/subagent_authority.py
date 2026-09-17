"""Monotonic, durable authority for Task-spawned child Agents.

An Agent definition is only the authority requested by the current step.  A
child must additionally remain inside every ancestor's boundary, including
after a worker restart or a continuable follow-up.  The descriptor snapshot is
therefore a monotonically narrowing intersection of tool ids plus independent permission planes;
planes are deliberately not flattened because permission rules are ordered
and last-match-wins inside one plane.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
from typing import Any, Iterable, Mapping, Sequence

from permission.permission import Rule


AUTHORITY_SNAPSHOT_VERSION = 2
LEGACY_AUTHORITY_SNAPSHOT_VERSION = 1
MAX_AUTHORITY_TOOLS = 4_096
MAX_AUTHORITY_PLANES = 16
MAX_AUTHORITY_RULES_PER_PLANE = 4_096
MAX_AUTHORITY_STRING = 1_024


class SubagentAuthorityError(RuntimeError):
    """A child authority snapshot is absent, corrupt, or unsupported."""


@dataclass(frozen=True, slots=True)
class SubagentAuthority:
    """Validated authority inherited by one child Session."""

    tool_ids: frozenset[str]
    permission_planes: tuple[tuple[Rule, ...], ...]
    guard_planes: tuple[tuple[Rule, ...], ...]
    composition: Any | None = None
    snapshot_version: int = LEGACY_AUTHORITY_SNAPSHOT_VERSION

    def to_json(self) -> dict[str, Any]:
        payload = {
            "version": self.snapshot_version,
            "tool_ids": sorted(self.tool_ids),
            "permission_planes": [
                [rule.model_dump(mode="json") for rule in plane]
                for plane in self.permission_planes
            ],
            "guard_planes": [
                [rule.model_dump(mode="json") for rule in plane]
                for plane in self.guard_planes
            ],
        }
        if self.snapshot_version == AUTHORITY_SNAPSHOT_VERSION:
            if self.composition is None:
                raise SubagentAuthorityError(
                    "v2 subagent authority requires a composition snapshot"
                )
            payload["composition"] = self.composition.to_json()
        return payload


def _validated_rule(raw: Any) -> Rule:
    if isinstance(raw, Rule):
        rule = raw
    else:
        if not isinstance(raw, dict):
            raise SubagentAuthorityError("authority rule must be an object")
        if set(raw) != {"permission", "pattern", "action"}:
            raise SubagentAuthorityError("authority rule has unsupported fields")
        try:
            rule = Rule.model_validate(raw)
        except Exception as exc:
            raise SubagentAuthorityError("authority rule is invalid") from exc
    if (
        not rule.permission
        or not rule.pattern
        or len(rule.permission) > MAX_AUTHORITY_STRING
        or len(rule.pattern) > MAX_AUTHORITY_STRING
    ):
        raise SubagentAuthorityError("authority rule exceeds its bounds")
    return Rule(
        permission=rule.permission,
        pattern=rule.pattern,
        action=rule.action,
    )


def _validated_plane(raw: Any, *, allow_empty: bool = False) -> tuple[Rule, ...]:
    if not isinstance(raw, (list, tuple)):
        raise SubagentAuthorityError("authority rule plane must be a list")
    if len(raw) > MAX_AUTHORITY_RULES_PER_PLANE:
        raise SubagentAuthorityError("authority rule plane is too large")
    if not raw and not allow_empty:
        raise SubagentAuthorityError("authority rule plane cannot be empty")
    return tuple(_validated_rule(rule) for rule in raw)


def _validated_planes(raw: Any, *, required: bool) -> tuple[tuple[Rule, ...], ...]:
    if not isinstance(raw, (list, tuple)):
        raise SubagentAuthorityError("authority planes must be a list")
    if len(raw) > MAX_AUTHORITY_PLANES:
        raise SubagentAuthorityError("authority has too many rule planes")
    if required and not raw:
        raise SubagentAuthorityError("authority permission planes are missing")
    return tuple(_validated_plane(plane) for plane in raw)


def parse_subagent_authority(raw: Any) -> SubagentAuthority:
    """Validate a descriptor snapshot without guessing legacy semantics."""
    if not isinstance(raw, dict):
        raise SubagentAuthorityError("subagent authority snapshot is missing")
    version = raw.get("version")
    expected = {
        "version", "tool_ids", "permission_planes", "guard_planes",
    }
    if version == AUTHORITY_SNAPSHOT_VERSION:
        expected = expected | {"composition"}
    if set(raw) != expected:
        raise SubagentAuthorityError("subagent authority snapshot is unsupported")
    if version not in {
        LEGACY_AUTHORITY_SNAPSHOT_VERSION, AUTHORITY_SNAPSHOT_VERSION,
    }:
        raise SubagentAuthorityError("subagent authority snapshot version is unsupported")

    raw_tools = raw.get("tool_ids")
    if not isinstance(raw_tools, list) or len(raw_tools) > MAX_AUTHORITY_TOOLS:
        raise SubagentAuthorityError("subagent authority tool boundary is invalid")
    tool_ids: set[str] = set()
    for tool_id in raw_tools:
        if (
            not isinstance(tool_id, str)
            or not tool_id
            or len(tool_id) > MAX_AUTHORITY_STRING
        ):
            raise SubagentAuthorityError("subagent authority contains an invalid tool id")
        tool_ids.add(tool_id)
    if len(tool_ids) != len(raw_tools):
        raise SubagentAuthorityError("subagent authority contains duplicate tool ids")

    composition = None
    if version == AUTHORITY_SNAPSHOT_VERSION:
        from agent.subagent_composition import (
            SubagentCompositionError,
            parse_subagent_composition,
        )

        try:
            composition = parse_subagent_composition(raw.get("composition"))
        except SubagentCompositionError as exc:
            raise SubagentAuthorityError(str(exc)) from exc
        if frozenset(tool_ids) != composition.tool_allowlist:
            raise SubagentAuthorityError(
                "subagent authority tools do not match its composition snapshot"
            )

    return SubagentAuthority(
        tool_ids=frozenset(tool_ids),
        permission_planes=_validated_planes(
            raw.get("permission_planes"), required=True,
        ),
        guard_planes=_validated_planes(raw.get("guard_planes"), required=False),
        composition=composition,
        snapshot_version=version,
    )


def _rules(rule_set: Sequence[Rule | dict]) -> tuple[Rule, ...]:
    return _validated_plane(list(rule_set))


def compose_subagent_authority(
    *,
    tool_ids: Iterable[str],
    permission_rules: Sequence[Rule | dict],
    guard_rules: Sequence[Rule | dict],
    inherited: SubagentAuthority | None = None,
) -> SubagentAuthority:
    """Intersect tools and append the current Agent as another must-pass plane."""
    current_tools = list(tool_ids)
    if len(current_tools) > MAX_AUTHORITY_TOOLS:
        raise SubagentAuthorityError("current Agent tool boundary is too large")
    if any(not isinstance(tool, str) or not tool for tool in current_tools):
        raise SubagentAuthorityError("current Agent tool boundary is invalid")
    tools = set(current_tools)
    if inherited is not None:
        tools.intersection_update(inherited.tool_ids)

    permission_planes = (
        inherited.permission_planes if inherited is not None else ()
    ) + (_rules(permission_rules),)
    current_guard = tuple(_validated_rule(rule) for rule in guard_rules)
    guard_planes = inherited.guard_planes if inherited is not None else ()
    if current_guard:
        guard_planes = guard_planes + (current_guard,)
    if (
        len(permission_planes) > MAX_AUTHORITY_PLANES
        or len(guard_planes) > MAX_AUTHORITY_PLANES
    ):
        raise SubagentAuthorityError("subagent nesting exceeds authority plane bounds")
    composition = inherited.composition if inherited is not None else None
    version = (
        inherited.snapshot_version
        if inherited is not None
        else LEGACY_AUTHORITY_SNAPSHOT_VERSION
    )
    if composition is not None:
        from agent.subagent_composition import narrow_follow_up_composition

        composition = narrow_follow_up_composition(
            composition,
            delegator_tool_ids=tools,
            requested_model=None,
            reasoning=None,
            persona=None,
            requested_tools=None,
            output_schema=None,
        )
        tools = set(composition.tool_allowlist)
        version = AUTHORITY_SNAPSHOT_VERSION
    return SubagentAuthority(
        tool_ids=frozenset(tools),
        permission_planes=permission_planes,
        guard_planes=guard_planes,
        composition=composition,
        snapshot_version=version,
    )


def with_subagent_composition(
    authority: SubagentAuthority,
    composition: Any,
) -> SubagentAuthority:
    """Upgrade an exact current-step boundary to the private v2 snapshot."""
    from agent.subagent_composition import parse_subagent_composition

    parsed = parse_subagent_composition(
        composition.to_json() if hasattr(composition, "to_json") else composition
    )
    if not parsed.tool_allowlist.issubset(authority.tool_ids):
        raise SubagentAuthorityError(
            "subagent composition exceeds the current Agent tool authority"
        )
    return SubagentAuthority(
        tool_ids=parsed.tool_allowlist,
        permission_planes=authority.permission_planes,
        guard_planes=authority.guard_planes,
        composition=parsed,
        snapshot_version=AUTHORITY_SNAPSHOT_VERSION,
    )


def _plane_key(plane: tuple[Rule, ...]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (rule.permission, rule.pattern, rule.action)
        for rule in plane
    )


def _deduplicated_planes(
    *collections: tuple[tuple[Rule, ...], ...],
) -> tuple[tuple[Rule, ...], ...]:
    result: list[tuple[Rule, ...]] = []
    seen: set[tuple[tuple[str, str, str], ...]] = set()
    for collection in collections:
        for plane in collection:
            key = _plane_key(plane)
            if key in seen:
                continue
            seen.add(key)
            result.append(plane)
    if len(result) > MAX_AUTHORITY_PLANES:
        raise SubagentAuthorityError("subagent authority has too many rule planes")
    return tuple(result)


def intersect_subagent_authorities(
    existing: SubagentAuthority,
    delegator: SubagentAuthority,
) -> SubagentAuthority:
    """Monotonically narrow a continuable child at each new follow-up."""
    tools = existing.tool_ids.intersection(delegator.tool_ids)
    composition = existing.composition
    version = existing.snapshot_version
    if composition is not None:
        from agent.subagent_composition import narrow_follow_up_composition

        composition = narrow_follow_up_composition(
            composition,
            delegator_tool_ids=tools,
            requested_model=None,
            reasoning=None,
            persona=None,
            requested_tools=None,
            output_schema=None,
        )
        tools = composition.tool_allowlist
        version = AUTHORITY_SNAPSHOT_VERSION
    return SubagentAuthority(
        tool_ids=tools,
        permission_planes=_deduplicated_planes(
            existing.permission_planes,
            delegator.permission_planes,
        ),
        guard_planes=_deduplicated_planes(
            existing.guard_planes,
            delegator.guard_planes,
        ),
        composition=composition,
        snapshot_version=version,
    )


def restrict_tools(
    tools: Mapping[str, Any],
    inherited: SubagentAuthority | None,
) -> dict[str, Any]:
    """Apply the durable parent tool boundary before exposure planning."""
    if inherited is None:
        return dict(tools)
    return {
        tool_id: tool
        for tool_id, tool in tools.items()
        if tool_id in inherited.tool_ids
    }


def authority_for_spawn(raw: Any) -> dict[str, Any]:
    """Validate the exact current-step snapshot before Task acceptance."""
    authority = parse_subagent_authority(raw)
    if "task" not in authority.tool_ids:
        raise SubagentAuthorityError(
            "Task is outside the current Agent authority snapshot"
        )
    return authority.to_json()


async def load_subagent_authority(session: Any) -> SubagentAuthority | None:
    """Load a child descriptor boundary; normal parented sessions fail closed."""
    # Clear an inherited ContextVar value before any lookup/error. A nested
    # child Task inherits its parent's asyncio context when created.
    _bound_frozen_agent.set(None)
    parent_id = str(getattr(session, "parent_id", "") or "")
    if not parent_id:
        return None
    # Cron transcripts also use parent_id for product grouping, not delegation.
    if str(getattr(session, "kind", "normal") or "normal") == "cron":
        return None

    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.subagent import SubagentDescriptor

    async with get_db_session() as db:
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.child_session_id == session.id,
                    SubagentDescriptor.user_id == session.user_id,
                    SubagentDescriptor.project_id == session.project_id,
                )
            )
        ).scalar_one_or_none()
    if descriptor is None:
        raise SubagentAuthorityError(
            "parented Agent Session has no durable subagent authority descriptor"
        )
    if descriptor.parent_session_id != parent_id:
        raise SubagentAuthorityError("subagent authority lineage does not match Session")
    authority = parse_subagent_authority(descriptor.authority_snapshot)
    if authority.composition is not None:
        from agent.subagent_composition import (
            SubagentCompositionError,
            validate_composition_availability,
        )
        from core.config import get_config

        config = get_config()
        try:
            validate_composition_availability(authority.composition, config)
        except SubagentCompositionError as exc:
            raise SubagentAuthorityError(str(exc)) from exc
    frozen = (
        authority.composition.frozen_agent()
        if authority.composition is not None
        else None
    )
    _bound_frozen_agent.set(frozen)
    return authority


_bound_frozen_agent: ContextVar[Any | None] = ContextVar(
    "subagent_frozen_agent",
    default=None,
)


def current_frozen_subagent_agent(name: str) -> Any | None:
    """Return the descriptor-bound preset for this child execution context."""
    agent = _bound_frozen_agent.get()
    if agent is None or getattr(agent, "name", None) != name:
        return None
    return agent
