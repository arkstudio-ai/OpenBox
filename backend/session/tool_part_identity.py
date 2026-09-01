"""API-hidden ToolPart identity persistence and replay resolution.

Provider-visible names are request bindings, not authorization identities.
This module is the narrow bridge between a persisted public ToolPart and a
provider request. Historical calls keep their exact wire name while the wire
dialect is unchanged—even if a dynamic tool was later disabled or removed.
Switching dialects must resolve the canonical ID through the *current* map.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from db.base import get_db_session
from db.models.internal_part import InternalPart
from db.models.part import Part
from session.internal_parts import (
    begin_session_write,
    lock_owned_session,
    session_exposure_lock,
)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CANONICAL_RE = re.compile(r"^[^\x00-\x20\x7f]{1,128}$")
_SAFE_WIRE_RE = re.compile(r"^[^\x00-\x20\x7f]{1,128}$")
_SAFE_DIALECT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_IDENTITY_FIELDS = (
    "canonical_tool_id",
    "wire_tool_name",
    "provider_binding_digest",
    "provider_dialect",
)


class ToolPartReplayError(RuntimeError):
    """A persisted tool call cannot be replayed without identity ambiguity."""


class AmbiguousLegacyToolAlias(ToolPartReplayError):
    """An old display alias maps to zero/multiple canonical identities."""


class ToolPartReplayBinding(BaseModel):
    """Private replay decision; never used as an API response model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    part_id: str
    canonical_tool_id: str
    wire_tool_name: str
    original_wire_tool_name: str
    provider_binding_digest: str
    provider_dialect: str
    stream_seq: int
    same_binding: bool
    identity_source: Literal["persisted", "legacy_unique_alias"]


def _canonical(value: Any) -> str:
    candidate = str(value or "")
    if not _SAFE_CANONICAL_RE.fullmatch(candidate):
        raise ToolPartReplayError("invalid canonical tool identity")
    return candidate


def _wire(value: Any) -> str:
    candidate = str(value or "")
    if not _SAFE_WIRE_RE.fullmatch(candidate):
        raise ToolPartReplayError("invalid provider wire tool name")
    return candidate


def _digest(value: Any) -> str:
    candidate = str(value or "").lower()
    if not _DIGEST_RE.fullmatch(candidate):
        raise ToolPartReplayError("invalid provider binding digest")
    return candidate


def _dialect(value: Any) -> str:
    candidate = str(value or "")
    if not _SAFE_DIALECT_RE.fullmatch(candidate):
        raise ToolPartReplayError("invalid provider dialect")
    return candidate


def _stream_seq(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ToolPartReplayError("invalid ToolPart stream sequence")
    return value


def tool_part_identity_values(part: Any) -> dict[str, Any]:
    """Validate hidden fields before ``save_part`` writes relational columns.

    An absent identity is tolerated for legacy/current compatibility.  Once
    any identity dimension is present, all dimensions plus ``stream_seq`` are
    mandatory so no partially-bound row can later be replayed.
    """
    if str(getattr(part, "type", "")) != "tool":
        return {}
    raw = {field: getattr(part, field, None) for field in _IDENTITY_FIELDS}
    sequence = getattr(part, "stream_seq", None)
    present = [value is not None for value in raw.values()]
    if not any(present):
        return {"stream_seq": _stream_seq(sequence)} if sequence is not None else {}
    if not all(present) or sequence is None:
        raise ToolPartReplayError("partial ToolPart provider identity is forbidden")
    return {
        "canonical_tool_id": _canonical(raw["canonical_tool_id"]),
        "wire_tool_name": _wire(raw["wire_tool_name"]),
        "provider_binding_digest": _digest(raw["provider_binding_digest"]),
        "provider_dialect": _dialect(raw["provider_dialect"]),
        "stream_seq": _stream_seq(sequence),
    }


def _alias_candidates(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    candidates = {str(item) for item in value if str(item)}
    return tuple(sorted(candidates))


def _validated_wire_map(current_wire_by_canonical: Mapping[str, str]) -> dict[str, str]:
    result = {
        _canonical(canonical): _wire(wire)
        for canonical, wire in current_wire_by_canonical.items()
    }
    if len(result.values()) != len(set(result.values())):
        raise ToolPartReplayError("current provider wire map contains a collision")
    return result


def resolve_projected_tool_part_for_replay(
    *,
    part: Any,
    current_binding_digest: str,
    current_provider_dialect: str,
    current_wire_by_canonical: Mapping[str, str],
    legacy_aliases: Mapping[str, str | Iterable[str]] | None = None,
    legacy_stream_seq: int | None = None,
) -> ToolPartReplayBinding:
    """Resolve identity from a canonical Event projection without SQL fallback."""
    binding_digest = _digest(current_binding_digest)
    dialect = _dialect(current_provider_dialect)
    wire_map = _validated_wire_map(current_wire_by_canonical)
    aliases = legacy_aliases or {}
    if isinstance(part, Mapping):
        value = dict(part)
        get = value.get
    else:
        get = lambda name, default=None: getattr(part, name, default)
    part_id = str(get("id") or "")
    if not part_id:
        raise ToolPartReplayError("historical ToolPart has no persisted identity key")
    raw = {field: get(field) for field in _IDENTITY_FIELDS}
    present = [item is not None for item in raw.values()]
    sequence_value = get("stream_seq")
    if any(present) and not all(present):
        raise ToolPartReplayError("partial projected ToolPart identity")
    if all(present):
        canonical = _canonical(raw["canonical_tool_id"])
        original_wire = _wire(raw["wire_tool_name"])
        original_digest = _digest(raw["provider_binding_digest"])
        original_dialect = _dialect(raw["provider_dialect"])
        sequence = _stream_seq(sequence_value)
        same_binding = (
            original_digest == binding_digest and original_dialect == dialect
        )
        # A past assistant tool call and its result remain valid provider
        # history after an MCP server/plugin is disabled. The old tool does
        # not become executable again: this value is used only to preserve the
        # historical call/result pair. Requiring it in the current catalogue
        # would permanently brick every Session that had ever used a dynamic
        # capability. A dialect switch still requires an explicit current
        # mapping because its wire representation may differ.
        replay_wire = (
            original_wire
            if original_dialect == dialect
            else wire_map.get(canonical)
        )
        if replay_wire is None:
            raise ToolPartReplayError(
                "canonical tool is unavailable in the current provider binding"
            )
        return ToolPartReplayBinding(
            part_id=part_id,
            canonical_tool_id=canonical,
            wire_tool_name=replay_wire,
            original_wire_tool_name=original_wire,
            provider_binding_digest=original_digest,
            provider_dialect=original_dialect,
            stream_seq=sequence,
            same_binding=same_binding,
            identity_source="persisted",
        )
    if sequence_value is not None:
        raise ToolPartReplayError("partial projected ToolPart identity")
    display_alias = str(get("tool") or "")
    candidates = _alias_candidates(aliases.get(display_alias))
    if len(candidates) != 1:
        raise AmbiguousLegacyToolAlias(
            "legacy tool alias is unknown or ambiguous; regenerate before replay"
        )
    if legacy_stream_seq is None:
        raise AmbiguousLegacyToolAlias(
            "legacy ToolPart ordering is ambiguous; regenerate before replay"
        )
    canonical = _canonical(candidates[0])
    replay_wire = wire_map.get(canonical)
    if replay_wire is None:
        raise ToolPartReplayError(
            "legacy canonical tool is unavailable in the current provider binding"
        )
    return ToolPartReplayBinding(
        part_id=part_id,
        canonical_tool_id=canonical,
        wire_tool_name=replay_wire,
        original_wire_tool_name=replay_wire,
        provider_binding_digest=binding_digest,
        provider_dialect=dialect,
        stream_seq=_stream_seq(legacy_stream_seq),
        same_binding=True,
        identity_source="legacy_unique_alias",
    )


async def _legacy_sequence(db, row: Part) -> int:
    """Infer old public ordering only when no newer ordering evidence exists."""
    internal_count = (
        await db.execute(
            select(func.count()).select_from(InternalPart).where(
                InternalPart.message_id == row.message_id
            )
        )
    ).scalar_one()
    if internal_count:
        raise AmbiguousLegacyToolAlias(
            "legacy ToolPart ordering is ambiguous; regenerate before replay"
        )
    ordered_rows = list((await db.execute(
        select(Part.id, Part.stream_seq).where(
            Part.message_id == row.message_id,
            Part.type == "tool",
        ).order_by(Part.created_at, Part.id)
    )).all())
    # Runtime stream_seq is the tool-call index within an assistant message.
    # A loop may lazily backfill several old calls one at a time; already
    # backfilled siblings are safe only when they agree with this deterministic
    # old-row order. Any partial-rollout value that disagrees remains closed.
    for index, (_part_id, sequence) in enumerate(ordered_rows):
        if sequence is not None and sequence != index:
            raise AmbiguousLegacyToolAlias(
                "legacy ToolPart ordering is ambiguous; regenerate before replay"
            )
    try:
        return [part_id for part_id, _sequence in ordered_rows].index(row.id)
    except ValueError as exc:  # pragma: no cover - row is locked in this transaction
        raise ToolPartReplayError("legacy ToolPart disappeared during replay") from exc


async def resolve_tool_part_for_replay(
    *,
    part_id: str,
    session_id: str,
    user_id: str,
    current_binding_digest: str,
    current_provider_dialect: str,
    current_wire_by_canonical: Mapping[str, str],
    legacy_aliases: Mapping[str, str | Iterable[str]] | None = None,
) -> ToolPartReplayBinding:
    """Resolve one historical call without authorizing by display/wire name.

    - An unchanged wire dialect reuses the immutable original wire name even
      after that dynamic tool disappears from the current catalogue.
    - A dialect switch uses canonical ID and the current request's
      collision-free mapping.
    - Legacy rows are backfilled only when their display alias is uniquely
      mapped. Unknown or colliding aliases fail closed and remain untouched.
    """
    binding_digest = _digest(current_binding_digest)
    dialect = _dialect(current_provider_dialect)
    wire_map = _validated_wire_map(current_wire_by_canonical)
    aliases = legacy_aliases or {}

    resolved: ToolPartReplayBinding | None = None
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            await lock_owned_session(db, session_id, user_id)
            row = (
                await db.execute(
                    select(Part).where(
                        Part.id == part_id,
                        Part.session_id == session_id,
                        Part.user_id == user_id,
                        Part.type == "tool",
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError("tool part not found")

            persisted = [getattr(row, field) is not None for field in _IDENTITY_FIELDS]
            if any(persisted) and not all(persisted):
                raise ToolPartReplayError("partial persisted ToolPart identity")
            if all(persisted):
                canonical = _canonical(row.canonical_tool_id)
                original_wire = _wire(row.wire_tool_name)
                original_digest = _digest(row.provider_binding_digest)
                original_dialect = _dialect(row.provider_dialect)
                sequence = _stream_seq(row.stream_seq)
                same_binding = (
                    original_digest == binding_digest
                    and original_dialect == dialect
                )
                if original_dialect == dialect:
                    replay_wire = original_wire
                else:
                    replay_wire = wire_map.get(canonical)
                    if replay_wire is None:
                        raise ToolPartReplayError(
                            "canonical tool is unavailable in the current provider binding"
                        )
                resolved = ToolPartReplayBinding(
                    part_id=row.id,
                    canonical_tool_id=canonical,
                    wire_tool_name=replay_wire,
                    original_wire_tool_name=original_wire,
                    provider_binding_digest=original_digest,
                    provider_dialect=original_dialect,
                    stream_seq=sequence,
                    same_binding=same_binding,
                    identity_source="persisted",
                )
            else:
                if row.stream_seq is not None:
                    # A sequence without identity was written by a partial
                    # rollout and is not a trustworthy legacy row.
                    raise ToolPartReplayError("partial persisted ToolPart identity")
                display_alias = ""
                if isinstance(row.data, dict):
                    display_alias = str(row.data.get("tool") or "")
                candidates = _alias_candidates(aliases.get(display_alias))
                if len(candidates) != 1:
                    raise AmbiguousLegacyToolAlias(
                        "legacy tool alias is unknown or ambiguous; regenerate before replay"
                    )
                canonical = _canonical(candidates[0])
                replay_wire = wire_map.get(canonical)
                if replay_wire is None:
                    raise ToolPartReplayError(
                        "legacy canonical tool is unavailable in the current provider binding"
                    )
                sequence = await _legacy_sequence(db, row)
                # Lazy backfill is intentionally relational/API-hidden; the
                # legacy display JSON stays byte-compatible for the frontend.
                row.canonical_tool_id = canonical
                row.wire_tool_name = replay_wire
                row.provider_binding_digest = binding_digest
                row.provider_dialect = dialect
                row.stream_seq = sequence
                resolved = ToolPartReplayBinding(
                    part_id=row.id,
                    canonical_tool_id=canonical,
                    wire_tool_name=replay_wire,
                    original_wire_tool_name=replay_wire,
                    provider_binding_digest=binding_digest,
                    provider_dialect=dialect,
                    stream_seq=sequence,
                    same_binding=True,
                    identity_source="legacy_unique_alias",
                )
    assert resolved is not None
    return resolved
