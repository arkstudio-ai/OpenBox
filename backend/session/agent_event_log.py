"""Canonical append-only Agent history and deterministic Surface projections.

``agent_events`` is the Agent kernel's source of truth.  The public projector
rebuilds the API/UI Surface without private replay data; the model projector
rebuilds the exact model-visible Surface plus API-hidden tool/provider replay
identity.  ``messages``/``parts`` and ``internal_parts`` are rebuildable read
models only.  Every mutation is appended in the same owner/fenced transaction
as its read-model write, and projection fails closed on a sequence gap.

Legacy Sessions are upgraded lazily: their current relational Surface is
captured once, followed by one private model seed.  After that boundary no
Agent context reader is allowed to fall back to mutable SQL rows.

Before every provider attempt the Loop freezes the complete provider-shaped
request, hashes it without persisting prompt bytes, then appends
``model.requested`` with an expected sequence+digest compare-and-swap.  Event
drift forces a fresh projection and payload build; a stale payload is never
sent after its checkpoint.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.identifier import ascending
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.internal_part import InternalPart
from db.models.message import Message
from db.models.part import Part, PRIVATE_TOOL_PART_FIELDS, public_part_data
from db.models.session import Session
from models.message import MessageWithParts
from session.internal_parts import begin_session_write, lock_owned_session


EVENT_SCHEMA_VERSION = 1
SURFACE_SCHEMA_VERSION = 1
MODEL_SURFACE_SCHEMA_VERSION = 1
RunFence = tuple[str, str, int]


class AgentEventProjectionError(ValueError):
    """The immutable stream cannot be projected without guessing."""


class AgentEventPrefixDriftError(AgentEventProjectionError):
    """A caller tried to dispatch against a prefix that is no longer current."""


@dataclass(frozen=True, slots=True)
class ProviderReplayRecord:
    """API-hidden provider transcript item reconstructed from Agent events."""

    id: str
    message_id: str
    kind: str
    capability_key_digest: str
    response_chain_id: str
    stream_seq: int
    origin_seq: int
    dedupe_key: str | None
    data: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class CanonicalModelSurface:
    """One immutable Agent-event prefix and its model-context projection."""

    session_id: str
    event_sequence: int
    event_digest: str
    replacement_generation: int
    messages: tuple[MessageWithParts, ...]
    provider_replay: tuple[ProviderReplayRecord, ...]

    def provider_replay_for(
        self,
        capability_key_digest: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return private replay grouped by Message for one exact binding."""
        digest = str(capability_key_digest or "").lower()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self.provider_replay:
            if record.capability_key_digest != digest:
                continue
            grouped.setdefault(record.message_id, []).append({
                "stream_seq": record.stream_seq,
                "data": deepcopy(record.data),
            })
        for records in grouped.values():
            records.sort(key=lambda item: int(item["stream_seq"]))
        return grouped


@dataclass(frozen=True, slots=True)
class CanonicalTailRepair:
    repaired_tools: int = 0
    closed_steps: int = 0
    closed_messages: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.repaired_tools or self.closed_steps or self.closed_messages)


class AgentEventParityReport(BaseModel):
    """Machine-readable evidence that canonical events match their read model."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    tracked: bool
    event_count: int
    last_sequence: int
    sequence_contiguous: bool
    projection_matches: bool
    balanced: bool
    require_closed: bool
    open_turn_ids: tuple[str, ...] = ()
    open_step_ids: tuple[str, ...] = ()
    open_tool_part_ids: tuple[str, ...] = ()
    unfinished_message_ids: tuple[str, ...] = ()
    error: str | None = None
    ok: bool


# Provider errors and provider-owned metadata are not public transcript data.
# This denylist is deliberately *not* applied to an arbitrary event payload:
# user text, tool inputs/outputs and ordinary structured content must survive
# byte-for-byte (apart from PostgreSQL's unsupported NUL byte).  Callers apply
# it only at the narrow provider-error/private-metadata write boundaries.
_PRIVATE_NORMALIZED_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.casefold())
    for key in {
        *PRIVATE_TOOL_PART_FIELDS,
        "provider_id",
        "provider_headers",
        "headers",
        "authorization",
        "api_key",
        "apikey",
        "access_key",
        "secret_access_key",
        "credential",
        "credentials",
        "password",
        "private_key",
        "client_secret",
        "access_token",
        "refresh_token",
        "token",
        "secret",
    }
}
_PRIVATE_NORMALIZED_SUFFIXES = (
    "authorization",
    "apikey",
    "accesskey",
    "secretaccesskey",
    "credential",
    "credentials",
    "password",
    "privatekey",
    "clientsecret",
    "accesstoken",
    "refreshtoken",
    "providerheaders",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:[a-z0-9]+[_-])*(?:authorization|api[_-]?key|"
    r"secret[_-]?access[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|session[_-]?token|password|client[_-]?secret|"
    r"private[_-]?key)\b\s*[:=]\s*(?:bearer\s+)?[\"']?)"
    r"([^\s,;}\"']+)"
)
_PROVIDER_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _is_private_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in _PRIVATE_NORMALIZED_KEYS or normalized.endswith(
        _PRIVATE_NORMALIZED_SUFFIXES
    )


def sanitize_provider_private(value: Any) -> Any:
    """Remove credentials from a provider-owned error/metadata subtree."""
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_provider_private(item)
            for key, item in value.items()
            if not _is_private_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_provider_private(item) for item in value]
    if isinstance(value, str):
        value = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", value)
        return _PROVIDER_KEY_RE.sub("sk-[REDACTED]", value)
    return value


def sanitize_message_error(value: Any) -> dict[str, Any] | None:
    """Sanitize one provider failure before SQL, events and SSE see it."""
    if value is None:
        return None
    sanitized = sanitize_provider_private(value)
    if isinstance(sanitized, Mapping):
        return dict(sanitized)
    return {"message": str(sanitized)}


def sanitize_public_part_data(value: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize only provider-owned fields of an otherwise exact Part body."""
    data = deepcopy(dict(value))
    if str(data.get("type") or "") == "tool":
        if data.get("metadata") is not None:
            data["metadata"] = sanitize_provider_private(data["metadata"])
        if data.get("error") is not None:
            data["error"] = sanitize_provider_private(data["error"])
        state = data.get("state")
        if isinstance(state, Mapping):
            state_copy = deepcopy(dict(state))
            if state_copy.get("metadata") is not None:
                state_copy["metadata"] = sanitize_provider_private(
                    state_copy["metadata"]
                )
            if state_copy.get("error") is not None:
                state_copy["error"] = sanitize_provider_private(
                    state_copy["error"]
                )
            data["state"] = state_copy
    return data


def _without_nul(value: Any) -> Any:
    """Remove actual NUL bytes without changing a literal ``\\u0000`` sample."""
    if isinstance(value, Mapping):
        return {
            str(key).replace("\x00", ""): _without_nul(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_without_nul(item) for item in value]
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def json_safe_copy(value: Any) -> Any:
    """Return a detached JSON value while preserving ordinary Unicode text."""
    # Fail before appending if a supposedly canonical payload is not JSON.
    # PostgreSQL JSONB also rejects NUL bytes, so canonicalization strips them
    # before encoding. Replacing the encoded ``\\u0000`` spelling would also
    # corrupt a legitimate user example containing those six literal bytes.
    encoded = json.dumps(
        _without_nul(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return json.loads(encoded)


def _json_copy(value: Any) -> Any:
    return json_safe_copy(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def public_message_state(row: Message) -> dict[str, Any]:
    """The complete public relational Message state, excluding provider wire data."""
    return _json_copy({
        "id": row.id,
        "session_id": row.session_id,
        "role": row.role,
        "created_at": _iso(row.created_at),
        "client_message_id": row.client_message_id,
        "agent": row.agent,
        # Assistant model identity is stored in ``model_id``; user messages
        # store the requested/base selection in ``model``. Keeping this role
        # mapping identical to the reconnect API prevents a live model badge
        # from disappearing after refresh without exposing ``provider_id``.
        "model": row.model_id if row.role == "assistant" else row.model,
        "variant": row.variant,
        "parent_id": row.parent_id,
        "finish": row.finish,
        "summary": row.summary,
        "tokens": deepcopy(row.tokens),
        "error": sanitize_message_error(row.error),
        "reaction": row.reaction,
        "format": deepcopy(row.format),
        "structured": deepcopy(row.structured),
    })


def public_part_state(row: Part) -> dict[str, Any]:
    """The public Part row; replay identity columns never enter the payload."""
    return _json_copy({
        "id": row.id,
        "message_id": row.message_id,
        "session_id": row.session_id,
        "type": row.type,
        "created_at": _iso(row.created_at),
        "data": sanitize_public_part_data(
            public_part_data(deepcopy(row.data or {}))
        ),
    })


def private_part_state(row: Part) -> dict[str, Any] | None:
    """Return validated API-hidden replay identity for a ToolPart."""
    values = {
        "canonical_tool_id": row.canonical_tool_id,
        "wire_tool_name": row.wire_tool_name,
        "provider_binding_digest": row.provider_binding_digest,
        "provider_dialect": row.provider_dialect,
        "stream_seq": row.stream_seq,
    }
    present = [value is not None for value in values.values()]
    if not any(present):
        return None
    if not all(present):
        raise AgentEventProjectionError("partial ToolPart replay identity")
    digest = str(values["provider_binding_digest"] or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AgentEventProjectionError("invalid ToolPart provider binding digest")
    sequence = values["stream_seq"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise AgentEventProjectionError("invalid ToolPart stream sequence")
    return _json_copy({**values, "provider_binding_digest": digest})


def private_provider_state(row: InternalPart) -> dict[str, Any]:
    """Return replay data safe to retain in canonical private Agent history."""
    digest = str(row.capability_key_digest or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AgentEventProjectionError("invalid provider replay binding digest")
    if row.stream_seq < 0 or row.origin_seq < 1:
        raise AgentEventProjectionError("invalid provider replay ordering")
    return _json_copy({
        "id": row.id,
        "message_id": row.message_id,
        "kind": row.kind,
        "capability_key_digest": digest,
        "response_chain_id": row.response_chain_id,
        "stream_seq": row.stream_seq,
        "origin_seq": row.origin_seq,
        "dedupe_key": row.dedupe_key,
        "data": sanitize_provider_private(deepcopy(row.data or {})),
        "created_at": _iso(row.created_at),
    })


async def prepare_agent_event_write(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    run_fence: RunFence | None,
) -> Session:
    """Acquire the sequence/read-model transaction fence.

    PostgreSQL serializes by the owning Session row. Desktop SQLite acquires a
    write transaction before its first read. If this is an Agent-owned write,
    the existing Driver exact fence is held until this transaction commits.
    """
    await begin_session_write(db)
    if run_fence is not None:
        fence_session_id, run_id, generation = run_fence
        if fence_session_id != session_id:
            from agent.driver import LeaseLostError

            raise LeaseLostError("Agent event fence targets another session")
        from agent.driver import assert_run_fence_locked

        await assert_run_fence_locked(
            db,
            session_id=session_id,
            user_id=user_id,
            run_id=run_id,
            generation=generation,
        )
    # The fence above already locks this row, but loading it gives appenders a
    # concrete owner object and keeps the no-run path equally serialized.
    return await lock_owned_session(db, session_id, user_id)


def _event_key(
    *,
    kind: str,
    run_id: str | None,
    generation: int | None,
    turn_id: str | None,
    step_id: str | None,
    message_id: str | None,
    part_id: str | None,
    tool_call_id: str | None,
    payload: dict[str, Any],
    idempotency_key: str | None,
    event_id: str,
) -> str:
    associations = {
        "kind": kind,
        "run_id": run_id,
        "generation": generation,
        "turn_id": turn_id,
        "step_id": step_id,
        "message_id": message_id,
        "part_id": part_id,
        "tool_call_id": tool_call_id,
    }
    if idempotency_key is not None:
        # The caller-provided logical identity is stable across retries. The
        # stored-row comparison below then rejects a changed payload instead
        # of silently accepting a second meaning for the same operation.
        identity = {"idempotency_key": idempotency_key, **associations}
    else:
        # State may legitimately return to an earlier value (A -> B -> A).
        # A payload-derived key would mistake that third transition for a
        # retry of the first and silently leave projection behind the read
        # model.  Ordinary transitions therefore get an occurrence identity;
        # callers use ``idempotency_key`` only when they possess a genuine,
        # stable operation id.
        identity = {"event_id": event_id, **associations, "payload": payload}
    return hashlib.sha256(_canonical_bytes(identity)).hexdigest()


async def append_agent_event_locked(
    db: AsyncSession,
    session_row: Session,
    *,
    kind: str,
    payload: Mapping[str, Any],
    run_fence: RunFence | None = None,
    turn_id: str | None = None,
    step_id: str | None = None,
    message_id: str | None = None,
    part_id: str | None = None,
    tool_call_id: str | None = None,
    idempotency_key: str | None = None,
) -> AgentEvent:
    """Append one immutable event while the owning Session row is locked."""
    if not kind or len(kind) > 48:
        raise ValueError("Agent event kind must be 1..48 characters")
    run_id: str | None = None
    generation: int | None = None
    if run_fence is not None:
        fence_session_id, run_id, generation = run_fence
        if fence_session_id != session_row.id:
            raise ValueError("Agent event run fence targets another session")
        if not run_id or isinstance(generation, bool) or generation < 1:
            raise ValueError("Agent event run identity is invalid")

    clean_payload = _json_copy({**dict(payload), "version": EVENT_SCHEMA_VERSION})
    event_id = ascending("aevt")
    key = _event_key(
        kind=kind,
        run_id=run_id,
        generation=generation,
        turn_id=turn_id,
        step_id=step_id,
        message_id=message_id,
        part_id=part_id,
        tool_call_id=tool_call_id,
        payload=clean_payload,
        idempotency_key=idempotency_key,
        event_id=event_id,
    )
    if idempotency_key is not None:
        existing = (
            await db.execute(
                select(AgentEvent).where(
                    AgentEvent.session_id == session_row.id,
                    AgentEvent.event_key == key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            expected = (
                existing.user_id == session_row.user_id
                and existing.kind == kind
                and existing.run_id == run_id
                and existing.generation == generation
                and existing.turn_id == turn_id
                and existing.step_id == step_id
                and existing.message_id == message_id
                and existing.part_id == part_id
                and existing.tool_call_id == tool_call_id
                and existing.payload == clean_payload
            )
            if not expected:
                raise AgentEventProjectionError("Agent event idempotency conflict")
            return existing

    sequence = int((await db.execute(
        select(func.coalesce(func.max(AgentEvent.sequence), 0) + 1).where(
            AgentEvent.session_id == session_row.id
        )
    )).scalar_one())
    event = AgentEvent(
        id=event_id,
        session_id=session_row.id,
        user_id=session_row.user_id,
        sequence=sequence,
        event_key=key,
        kind=kind,
        run_id=run_id,
        generation=generation,
        turn_id=turn_id,
        step_id=step_id,
        message_id=message_id,
        part_id=part_id,
        tool_call_id=tool_call_id,
        payload=clean_payload,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    await db.flush()
    return event


async def _surface_snapshot_locked(
    db: AsyncSession,
    session_row: Session,
) -> dict[str, Any]:
    messages = list((await db.execute(
        select(Message).where(
            Message.session_id == session_row.id,
            Message.user_id == session_row.user_id,
        ).order_by(Message.created_at, Message.id)
    )).scalars().all())
    message_ids = [message.id for message in messages]
    parts: list[Part] = []
    if message_ids:
        parts = list((await db.execute(
            select(Part).where(
                Part.message_id.in_(message_ids),
                Part.session_id == session_row.id,
                Part.user_id == session_row.user_id,
            ).order_by(Part.created_at, Part.id)
        )).scalars().all())
    by_message: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        by_message.setdefault(part.message_id, []).append(public_part_state(part))
    return {
        "version": SURFACE_SCHEMA_VERSION,
        "session_id": session_row.id,
        "messages": [
            {**public_message_state(message), "parts": by_message.get(message.id, [])}
            for message in messages
        ],
    }


async def _sanitize_read_model_locked(
    db: AsyncSession,
    session_row: Session,
) -> None:
    """Normalize legacy provider-owned fields before seeding the read model."""
    messages = list((await db.execute(select(Message).where(
        Message.session_id == session_row.id,
        Message.user_id == session_row.user_id,
    ))).scalars().all())
    message_ids = [row.id for row in messages]
    for row in messages:
        safe_error = sanitize_message_error(row.error)
        if row.error != safe_error:
            row.error = safe_error
    if message_ids:
        parts = list((await db.execute(select(Part).where(
            Part.message_id.in_(message_ids),
            Part.session_id == session_row.id,
            Part.user_id == session_row.user_id,
        ))).scalars().all())
        for row in parts:
            safe_data = sanitize_public_part_data(
                public_part_data(deepcopy(row.data or {}))
            )
            if row.data != safe_data:
                row.data = safe_data
    await db.flush()


async def _model_snapshot_locked(
    db: AsyncSession,
    session_row: Session,
) -> dict[str, Any]:
    """Capture private replay read models for a one-time legacy seed."""
    part_rows = list((await db.execute(select(Part).where(
        Part.session_id == session_row.id,
        Part.user_id == session_row.user_id,
    ).order_by(Part.created_at, Part.id))).scalars().all())
    part_replay = {
        row.id: identity
        for row in part_rows
        if (identity := private_part_state(row)) is not None
    }
    provider_rows = list((await db.execute(select(InternalPart).where(
        InternalPart.session_id == session_row.id,
        InternalPart.user_id == session_row.user_id,
        InternalPart.kind != "tool_reveal",
    ).order_by(InternalPart.origin_seq, InternalPart.id))).scalars().all())
    return {
        "version": MODEL_SURFACE_SCHEMA_VERSION,
        "part_replay": part_replay,
        "provider_replay": [private_provider_state(row) for row in provider_rows],
    }


async def ensure_surface_seed_locked(
    db: AsyncSession,
    session_row: Session,
) -> AgentEvent | None:
    """Capture legacy relational rows once before the first canonical mutation."""
    existing = (await db.execute(
        select(AgentEvent.id).where(AgentEvent.session_id == session_row.id).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return None
    await _sanitize_read_model_locked(db, session_row)
    snapshot = await _surface_snapshot_locked(db, session_row)
    model = await _model_snapshot_locked(db, session_row)
    return await append_agent_event_locked(
        db,
        session_row,
        kind="surface.seed",
        payload={"surface": snapshot, "model": model},
        idempotency_key=f"surface-seed:{SURFACE_SCHEMA_VERSION}",
    )


async def ensure_model_seed_locked(
    db: AsyncSession,
    session_row: Session,
) -> AgentEvent | None:
    """Append one private model seed for logs created before canonical serving."""
    marker = (await db.execute(select(AgentEvent.id).where(
        AgentEvent.session_id == session_row.id,
        AgentEvent.kind.in_(("surface.seed", "surface.model_seed")),
    ).order_by(AgentEvent.sequence))).scalars().all()
    if marker:
        first = (await db.execute(select(AgentEvent).where(
            AgentEvent.id == marker[0]
        ))).scalar_one()
        first_model = (first.payload or {}).get("model")
        if (
            isinstance(first_model, Mapping)
            and first_model.get("version") == MODEL_SURFACE_SCHEMA_VERSION
        ) or len(marker) > 1:
            return None
    await _sanitize_read_model_locked(db, session_row)
    model = await _model_snapshot_locked(db, session_row)
    return await append_agent_event_locked(
        db,
        session_row,
        kind="surface.model_seed",
        payload={"model": model},
        idempotency_key=f"surface-model-seed:{MODEL_SURFACE_SCHEMA_VERSION}",
    )


async def append_message_events_locked(
    db: AsyncSession,
    session_row: Session,
    message: Message,
    *,
    operation: str,
    run_fence: RunFence | None,
    logical_turn_id: str | None = None,
) -> tuple[AgentEvent, ...]:
    if operation not in {"created", "updated"}:
        raise ValueError("unsupported Message event operation")
    safe_error = sanitize_message_error(message.error)
    if message.error != safe_error:
        message.error = safe_error
        await db.flush()
    state = public_message_state(message)
    turn_id = await _logical_turn_id_locked(
        db,
        session_row,
        message,
        run_fence=run_fence,
        explicit_turn_id=logical_turn_id,
    )
    events: list[AgentEvent] = []
    if operation == "created" and message.role == "user":
        existing_start = None
        if run_fence is not None:
            _, run_id, generation = run_fence
            existing_start = (await db.execute(select(AgentEvent.id).where(
                AgentEvent.session_id == session_row.id,
                AgentEvent.run_id == run_id,
                AgentEvent.generation == generation,
                AgentEvent.kind == "turn.started",
                AgentEvent.turn_id == turn_id,
            ).limit(1))).scalar_one_or_none()
        if existing_start is None:
            events.append(await append_agent_event_locked(
                db,
                session_row,
                kind="turn.started",
                payload={"message_id": message.id},
                run_fence=run_fence,
                turn_id=turn_id,
                message_id=message.id,
            ))
    events.append(await append_agent_event_locked(
        db,
        session_row,
        kind=f"message.{operation}",
        payload={"message": state},
        run_fence=run_fence,
        turn_id=turn_id,
        message_id=message.id,
    ))
    terminal = message.role == "assistant" and (
        bool(message.error)
        or (
            message.finish is not None
            and message.finish not in {"tool_calls", "tool-calls", "compact"}
        )
    )
    if operation == "updated" and terminal:
        events.append(await append_agent_event_locked(
            db,
            session_row,
            kind="turn.finished",
            payload={
                "message_id": message.id,
                "finish": message.finish,
                "error": deepcopy(message.error),
            },
            run_fence=run_fence,
            turn_id=turn_id,
            message_id=message.id,
        ))
    return tuple(events)


async def _logical_turn_id_locked(
    db: AsyncSession,
    session_row: Session,
    message: Message,
    *,
    run_fence: RunFence | None,
    explicit_turn_id: str | None = None,
) -> str:
    """Resolve one stable logical Turn across Inbox step boundaries."""
    if explicit_turn_id is not None:
        if not explicit_turn_id or len(explicit_turn_id) > 256:
            raise ValueError("logical turn id must be 1..256 characters")
        return explicit_turn_id
    if run_fence is not None:
        _, run_id, generation = run_fence
        started = (await db.execute(select(AgentEvent.turn_id).where(
            AgentEvent.session_id == session_row.id,
            AgentEvent.run_id == run_id,
            AgentEvent.generation == generation,
            AgentEvent.kind == "turn.started",
            AgentEvent.turn_id.is_not(None),
        ).order_by(AgentEvent.sequence).limit(1))).scalar_one_or_none()
        if started:
            return str(started)
    return message.id if message.role == "user" else (message.parent_id or message.id)


async def append_part_event_locked(
    db: AsyncSession,
    session_row: Session,
    part: Part,
    message: Message,
    *,
    operation: str,
    run_fence: RunFence | None,
) -> AgentEvent:
    if operation not in {"created", "updated"}:
        raise ValueError("unsupported Part event operation")
    if (
        part.session_id != session_row.id
        or part.user_id != session_row.user_id
        or part.message_id != message.id
    ):
        raise AgentEventProjectionError("Part event crosses its Session owner")
    safe_data = sanitize_public_part_data(
        public_part_data(deepcopy(part.data or {}))
    )
    if part.data != safe_data:
        part.data = safe_data
        await db.flush()
    state = public_part_state(part)
    data = state["data"]
    part_type = str(data.get("type") or part.type)
    turn_id = await _logical_turn_id_locked(
        db,
        session_row,
        message,
        run_fence=run_fence,
    )
    step_id: str | None = message.id if message.role == "assistant" else None
    tool_call_id: str | None = None
    if part_type == "step-start":
        kind = "step.started"
    elif part_type == "step-finish":
        kind = "step.finished"
    elif part_type == "tool":
        raw_call_id = str(data.get("call_id") or part.id)
        tool_call_id = raw_call_id if len(raw_call_id) <= 256 else hashlib.sha256(
            raw_call_id.encode("utf-8")
        ).hexdigest()
        status = getattr(data.get("status"), "value", data.get("status"))
        if status in {"completed", "error"}:
            kind = "tool.result"
        elif operation == "created":
            kind = "tool.called"
        else:
            kind = "tool.updated"
    else:
        kind = f"part.{operation}"
    return await append_agent_event_locked(
        db,
        session_row,
        kind=kind,
        payload={
            "part": state,
            "model": {"tool_identity": private_part_state(part)},
        },
        run_fence=run_fence,
        turn_id=turn_id,
        step_id=step_id,
        message_id=message.id,
        part_id=part.id,
        tool_call_id=tool_call_id,
    )


async def append_surface_remove_locked(
    db: AsyncSession,
    session_row: Session,
    *,
    message_ids: Sequence[str],
    run_fence: RunFence | None = None,
) -> AgentEvent:
    ordered = list(message_ids)
    if not ordered or len(set(ordered)) != len(ordered):
        raise ValueError("Surface removal ids must be non-empty and unique")
    return await append_agent_event_locked(
        db,
        session_row,
        kind="surface.messages_removed",
        payload={"message_ids": ordered},
        run_fence=run_fence,
        message_id=ordered[0],
    )


async def append_provider_replay_event_locked(
    db: AsyncSession,
    session_row: Session,
    row: InternalPart,
    *,
    run_fence: RunFence | None,
) -> AgentEvent:
    """Append one private provider replay item in its SQL insert transaction."""
    if (
        row.session_id != session_row.id
        or row.user_id != session_row.user_id
        or row.kind == "tool_reveal"
    ):
        raise AgentEventProjectionError("provider replay crosses its Session owner")
    replay = private_provider_state(row)
    return await append_agent_event_locked(
        db,
        session_row,
        kind="provider.transcript",
        payload={"provider_replay": replay},
        run_fence=run_fence,
        message_id=row.message_id,
        part_id=row.id,
        idempotency_key=f"provider-replay:{row.id}",
    )


def _event_value(event: AgentEvent | Mapping[str, Any], name: str) -> Any:
    return event.get(name) if isinstance(event, Mapping) else getattr(event, name)


def _model_exclusion_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate one immutable model-only Message exclusion payload."""
    raw_ids = payload.get("message_ids")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or len(raw_ids) > 8
        or any(
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > 160
            for message_id in raw_ids
        )
    ):
        raise AgentEventProjectionError(
            "model Surface exclusion has invalid Message ids"
        )
    message_ids = tuple(raw_ids)
    if len(set(message_ids)) != len(message_ids):
        raise AgentEventProjectionError(
            "model Surface exclusion has duplicate Message ids"
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason or len(reason) > 96:
        raise AgentEventProjectionError(
            "model Surface exclusion has an invalid reason"
        )
    source_item_id = payload.get("source_item_id")
    if source_item_id is not None and (
        not isinstance(source_item_id, str)
        or not source_item_id
        or len(source_item_id) > 160
    ):
        raise AgentEventProjectionError(
            "model Surface exclusion has an invalid source item"
        )
    return message_ids


def model_excluded_message_ids(
    events: Iterable[AgentEvent | Mapping[str, Any]],
) -> frozenset[str]:
    """Return the monotonic set hidden from model context, never public audit."""
    excluded: set[str] = set()
    for event in events:
        if str(_event_value(event, "kind")) != "surface.model_exclusion":
            continue
        payload = _event_value(event, "payload")
        if (
            not isinstance(payload, Mapping)
            or payload.get("version") != EVENT_SCHEMA_VERSION
        ):
            raise AgentEventProjectionError(
                "model Surface exclusion has an unsupported payload"
            )
        excluded.update(_model_exclusion_ids(payload))
    return frozenset(excluded)


def project_agent_events(
    events: Iterable[AgentEvent | Mapping[str, Any]],
) -> dict[str, Any]:
    """Purely rebuild the canonical public Message/Part Surface."""
    ordered = list(events)
    if not ordered:
        raise AgentEventProjectionError("Session has no canonical Agent events")
    expected_sequence = 1
    session_id = str(_event_value(ordered[0], "session_id"))
    messages: dict[str, dict[str, Any]] = {}
    parts: dict[str, dict[str, dict[str, Any]]] = {}
    seeded = False

    for event in ordered:
        sequence = int(_event_value(event, "sequence"))
        if sequence != expected_sequence:
            raise AgentEventProjectionError(
                f"Agent event sequence gap: expected {expected_sequence}, got {sequence}"
            )
        expected_sequence += 1
        if str(_event_value(event, "session_id")) != session_id:
            raise AgentEventProjectionError("Agent event stream crosses Session ids")
        kind = str(_event_value(event, "kind"))
        payload = _json_copy(_event_value(event, "payload"))
        if payload.get("version") != EVENT_SCHEMA_VERSION:
            raise AgentEventProjectionError("unsupported Agent event payload version")

        if kind == "surface.seed":
            if seeded or messages or parts:
                raise AgentEventProjectionError("Surface seed must be the first state event")
            surface = payload.get("surface")
            if not isinstance(surface, dict) or surface.get("version") != SURFACE_SCHEMA_VERSION:
                raise AgentEventProjectionError("invalid Surface seed")
            if str(surface.get("session_id")) != session_id:
                raise AgentEventProjectionError("Surface seed targets another Session")
            for message in surface.get("messages") or []:
                state = _json_copy(message)
                message_id = str(state.get("id") or "")
                if not message_id or str(state.get("session_id")) != session_id:
                    raise AgentEventProjectionError("invalid seeded Message")
                seeded_parts = state.pop("parts", [])
                messages[message_id] = state
                parts[message_id] = {}
                for part in seeded_parts:
                    part_state = _json_copy(part)
                    part_id = str(part_state.get("id") or "")
                    if (
                        not part_id
                        or str(part_state.get("message_id")) != message_id
                        or str(part_state.get("session_id")) != session_id
                    ):
                        raise AgentEventProjectionError("invalid seeded Part")
                    parts[message_id][part_id] = part_state
            seeded = True
            continue

        if kind in {"message.created", "message.updated"}:
            state = _json_copy(payload.get("message"))
            if not isinstance(state, dict):
                raise AgentEventProjectionError("Message event has no state")
            message_id = str(state.get("id") or "")
            if not message_id or str(state.get("session_id")) != session_id:
                raise AgentEventProjectionError("Message event targets another Session")
            messages[message_id] = state
            parts.setdefault(message_id, {})
            continue

        if kind in {
            "part.created",
            "part.updated",
            "step.started",
            "step.finished",
            "tool.called",
            "tool.updated",
            "tool.result",
        }:
            state = _json_copy(payload.get("part"))
            if not isinstance(state, dict):
                raise AgentEventProjectionError("Part event has no state")
            message_id = str(state.get("message_id") or "")
            part_id = str(state.get("id") or "")
            if (
                not part_id
                or message_id not in messages
                or str(state.get("session_id")) != session_id
            ):
                raise AgentEventProjectionError("Part event has no owning Message")
            parts.setdefault(message_id, {})[part_id] = state
            continue

        if kind == "surface.messages_removed":
            removed = payload.get("message_ids")
            if not isinstance(removed, list):
                raise AgentEventProjectionError("Surface removal has no Message ids")
            for message_id in removed:
                messages.pop(str(message_id), None)
                parts.pop(str(message_id), None)
            continue

        if kind == "surface.model_exclusion":
            # The public/API projection deliberately retains these Messages
            # as immutable delivery/audit evidence. Only the model projector
            # applies the monotonic exclusion.
            _model_exclusion_ids(payload)
            continue

        # Lifecycle and provenance records are immutable evidence only.  A
        # compaction replacement describes which projected Surface range its
        # summary shadows, while the compatible SQL Surface continues to keep
        # the visible transcript rows.  Fork lineage likewise never inserts or
        # removes a Message by itself.
        if kind in {
            "turn.started",
            "turn.finished",
            "surface.replacement",
            "surface.model_seed",
            "surface.model_import",
            "provider.transcript",
            "model.requested",
            "session.forked",
            "inbox.accepted",
            "inbox.claimed",
            "inbox.canceled",
            "inbox.settled",
        }:
            continue
        raise AgentEventProjectionError(f"unsupported Agent event kind: {kind}")

    def sort_key(state: Mapping[str, Any]) -> tuple[str, str]:
        return str(state.get("created_at") or ""), str(state.get("id") or "")

    projected_messages: list[dict[str, Any]] = []
    for message in sorted(messages.values(), key=sort_key):
        message_id = str(message["id"])
        projected_messages.append({
            **deepcopy(message),
            "parts": sorted(parts.get(message_id, {}).values(), key=sort_key),
        })
    return {
        "version": SURFACE_SCHEMA_VERSION,
        "session_id": session_id,
        "messages": projected_messages,
    }


def _immutable_event_state(event: AgentEvent | Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sequence": int(_event_value(event, "sequence")),
        "event_key": str(_event_value(event, "event_key")),
        "kind": str(_event_value(event, "kind")),
        "run_id": _event_value(event, "run_id"),
        "generation": _event_value(event, "generation"),
        "turn_id": _event_value(event, "turn_id"),
        "step_id": _event_value(event, "step_id"),
        "message_id": _event_value(event, "message_id"),
        "part_id": _event_value(event, "part_id"),
        "tool_call_id": _event_value(event, "tool_call_id"),
        "payload": _json_copy(_event_value(event, "payload")),
    }


def event_prefix_digest(
    events: Sequence[AgentEvent | Mapping[str, Any]],
) -> str:
    """Digest an exact immutable prefix after validating contiguous sequence."""
    if not events:
        raise AgentEventProjectionError("Session has no canonical Agent events")
    expected = 1
    for event in events:
        sequence = int(_event_value(event, "sequence"))
        if sequence != expected:
            raise AgentEventProjectionError(
                f"Agent event sequence gap: expected {expected}, got {sequence}"
            )
        expected += 1
    return hashlib.sha256(_canonical_bytes(
        [_immutable_event_state(event) for event in events]
    )).hexdigest()


def _validate_model_seed(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentEventProjectionError("model Surface seed is missing")
    seed = _json_copy(value)
    if seed.get("version") != MODEL_SURFACE_SCHEMA_VERSION:
        raise AgentEventProjectionError("unsupported model Surface seed version")
    if not isinstance(seed.get("part_replay"), Mapping):
        raise AgentEventProjectionError("invalid model ToolPart seed")
    if not isinstance(seed.get("provider_replay"), list):
        raise AgentEventProjectionError("invalid provider replay seed")
    return seed


def _validate_tool_identity(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentEventProjectionError("invalid ToolPart replay identity")
    required = set(PRIVATE_TOOL_PART_FIELDS)
    if set(value) != required:
        raise AgentEventProjectionError("partial ToolPart replay identity")
    digest = str(value.get("provider_binding_digest") or "").lower()
    sequence = value.get("stream_seq")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or not str(value.get("canonical_tool_id") or "")
        or not str(value.get("wire_tool_name") or "")
        or not str(value.get("provider_dialect") or "")
    ):
        raise AgentEventProjectionError("invalid ToolPart replay identity")
    return _json_copy({**dict(value), "provider_binding_digest": digest})


def _validate_provider_replay(value: Any, *, session_message_ids: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentEventProjectionError("invalid provider replay item")
    item = _json_copy(value)
    required = {
        "id",
        "message_id",
        "kind",
        "capability_key_digest",
        "response_chain_id",
        "stream_seq",
        "origin_seq",
        "data",
        "created_at",
    }
    allowed = required | {"dedupe_key"}
    if not required.issubset(item) or not set(item).issubset(allowed):
        raise AgentEventProjectionError("partial provider replay item")
    item.setdefault("dedupe_key", None)
    digest = str(item.get("capability_key_digest") or "").lower()
    dedupe_key = item.get("dedupe_key")
    if (
        str(item.get("message_id") or "") not in session_message_ids
        or not str(item.get("id") or "")
        or not str(item.get("kind") or "").startswith("provider_")
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(item.get("stream_seq"), int)
        or isinstance(item.get("stream_seq"), bool)
        or item["stream_seq"] < 0
        or not isinstance(item.get("origin_seq"), int)
        or isinstance(item.get("origin_seq"), bool)
        or item["origin_seq"] < 1
        or (
            dedupe_key is not None
            and not re.fullmatch(r"[0-9a-f]{64}", str(dedupe_key).lower())
        )
        or not isinstance(item.get("data"), Mapping)
    ):
        raise AgentEventProjectionError("invalid provider replay item")
    item["capability_key_digest"] = digest
    item["dedupe_key"] = (
        str(dedupe_key).lower() if dedupe_key is not None else None
    )
    return item


def _apply_replacement_projection(
    messages: list[dict[str, Any]],
    replacement: Mapping[str, Any],
) -> list[dict[str, Any]]:
    source = replacement.get("source")
    if not isinstance(source, Mapping):
        raise AgentEventProjectionError("compaction replacement has no source")
    # A fork retains the original immutable provenance descriptor verbatim,
    # including its source Session ids/digest, and supplies a separate list of
    # remapped child ids for projection. Native replacements use their cited
    # source ids directly.
    covered = replacement.get(
        "projected_covered_message_ids",
        source.get("covered_message_ids"),
    )
    boundary_id = str(replacement.get("boundary_user_message_id") or "")
    summary_id = str(replacement.get("summary_message_id") or "")
    tail_start_id = replacement.get("tail_start_id")
    if (
        not isinstance(covered, list)
        or not covered
        or not all(isinstance(item, str) and item for item in covered)
        or len(set(covered)) != len(covered)
        or not boundary_id
        or not summary_id
        or (tail_start_id is not None and not isinstance(tail_start_id, str))
    ):
        raise AgentEventProjectionError("invalid compaction replacement descriptor")
    original = list(messages)
    retained = [item for item in original if str(item.get("id")) not in set(covered)]
    boundary_index = next(
        (index for index, item in enumerate(retained)
         if str(item.get("id")) == boundary_id),
        -1,
    )
    if boundary_index < 0:
        raise AgentEventProjectionError("compaction replacement boundary is absent")
    result = retained[boundary_index:]
    summary_index = next(
        (index for index, item in enumerate(result)
         if str(item.get("id")) == summary_id),
        -1,
    )
    if summary_index < 0:
        raise AgentEventProjectionError("compaction replacement summary is absent")
    if not tail_start_id:
        return result
    original_boundary = next(
        (index for index, item in enumerate(original)
         if str(item.get("id")) == boundary_id),
        -1,
    )
    tail_index = next(
        (index for index, item in enumerate(original)
         if str(item.get("id")) == tail_start_id),
        -1,
    )
    if tail_index < 0 or original_boundary < 0 or tail_index >= original_boundary:
        raise AgentEventProjectionError("compaction replacement tail is invalid")
    tail = [
        item for item in original[tail_index:original_boundary]
        if str(item.get("id")) not in set(covered)
    ]
    return result[:summary_index + 1] + tail + result[summary_index + 1:]


def project_model_agent_events(
    events: Iterable[AgentEvent | Mapping[str, Any]],
) -> CanonicalModelSurface:
    """Purely rebuild model context and private replay from Agent events."""
    ordered = list(events)
    public = project_agent_events(ordered)
    excluded_message_ids = model_excluded_message_ids(ordered)
    message_ids = {
        str(message.get("id")) for message in public.get("messages") or []
    }
    known_message_ids = set(message_ids)
    for event in ordered:
        payload = _event_value(event, "payload")
        if not isinstance(payload, Mapping):
            continue
        message = payload.get("message")
        if isinstance(message, Mapping) and message.get("id"):
            known_message_ids.add(str(message["id"]))
        surface = payload.get("surface")
        if isinstance(surface, Mapping):
            known_message_ids.update(
                str(item.get("id"))
                for item in surface.get("messages") or []
                if isinstance(item, Mapping) and item.get("id")
            )
    unknown_exclusions = excluded_message_ids - known_message_ids
    if unknown_exclusions:
        raise AgentEventProjectionError(
            "model Surface exclusion references an unknown Message"
        )
    part_ids = {
        str(part.get("id"))
        for message in public.get("messages") or []
        for part in message.get("parts") or []
    }
    identities: dict[str, dict[str, Any]] = {}
    provider_items: dict[str, dict[str, Any]] = {}
    replacements: list[dict[str, Any]] = []
    has_model_seed = False

    for event in ordered:
        kind = str(_event_value(event, "kind"))
        payload = _json_copy(_event_value(event, "payload"))
        if kind in {"surface.seed", "surface.model_seed", "surface.model_import"}:
            raw_model = payload.get("model")
            if raw_model is None:
                continue
            seed = _validate_model_seed(raw_model)
            has_model_seed = True
            for part_id, raw_identity in seed["part_replay"].items():
                identity = _validate_tool_identity(raw_identity)
                if identity is not None:
                    identities[str(part_id)] = identity
            for raw_item in seed["provider_replay"]:
                item = _validate_provider_replay(
                    raw_item,
                    session_message_ids=known_message_ids,
                )
                provider_items[str(item["id"])] = item
            continue
        if kind in {
            "part.created",
            "part.updated",
            "step.started",
            "step.finished",
            "tool.called",
            "tool.updated",
            "tool.result",
        }:
            part = payload.get("part")
            part_id = str(part.get("id") or "") if isinstance(part, Mapping) else ""
            raw_model = payload.get("model")
            if isinstance(raw_model, Mapping) and "tool_identity" in raw_model:
                identity = _validate_tool_identity(raw_model.get("tool_identity"))
                if identity is None:
                    identities.pop(part_id, None)
                else:
                    identities[part_id] = identity
            continue
        if kind == "provider.transcript":
            item = _validate_provider_replay(
                payload.get("provider_replay"),
                session_message_ids=known_message_ids,
            )
            provider_items[str(item["id"])] = item
            continue
        if kind == "surface.messages_removed":
            removed = {str(item) for item in payload.get("message_ids") or []}
            for part_id in list(identities):
                # ``part_ids`` is final-state only; removal is enforced below by
                # retaining identities for final public parts exclusively.
                if part_id not in part_ids:
                    identities.pop(part_id, None)
            for item_id, item in list(provider_items.items()):
                if str(item.get("message_id")) in removed:
                    provider_items.pop(item_id, None)
            continue
        if kind == "surface.replacement":
            replacements.append(payload)

    if not has_model_seed:
        raise AgentEventProjectionError(
            "canonical model seed is missing; seed legacy Session before loading"
        )

    model_states = deepcopy(list(public.get("messages") or []))
    for replacement in replacements:
        visible_ids = {str(item.get("id")) for item in model_states}
        boundary_id = str(replacement.get("boundary_user_message_id") or "")
        summary_id = str(replacement.get("summary_message_id") or "")
        if boundary_id not in visible_ids and summary_id not in visible_ids:
            # A later regenerate/dismiss removed the whole compaction attempt;
            # immutable provenance stays in history but no longer shadows rows.
            continue
        if (boundary_id in visible_ids) != (summary_id in visible_ids):
            raise AgentEventProjectionError("partial compaction replacement Surface")
        model_states = _apply_replacement_projection(model_states, replacement)

    model_states = [
        state
        for state in model_states
        if str(state.get("id") or "") not in excluded_message_ids
    ]

    models: list[MessageWithParts] = []
    projected_message_ids = {str(item.get("id")) for item in model_states}
    projected_part_ids: set[str] = set()
    for state in model_states:
        value = deepcopy(dict(state))
        model_parts: list[dict[str, Any]] = []
        for part in value.get("parts") or []:
            if not isinstance(part, Mapping):
                raise AgentEventProjectionError("invalid projected Part")
            part_id = str(part.get("id") or "")
            data = deepcopy(dict(part.get("data") or {}))
            identity = identities.get(part_id)
            if identity is not None:
                data.update(identity)
            model_parts.append(data)
            projected_part_ids.add(part_id)
        value["parts"] = model_parts
        try:
            models.append(MessageWithParts.model_validate(value))
        except Exception as exc:
            raise AgentEventProjectionError(
                f"invalid model Surface Message {value.get('id')}"
            ) from exc

    replay = tuple(
        ProviderReplayRecord(**item)
        for item in sorted(
            provider_items.values(),
            key=lambda item: (
                str(item.get("created_at") or ""),
                int(item.get("stream_seq") or 0),
                int(item.get("origin_seq") or 0),
                str(item.get("id") or ""),
            ),
        )
        if str(item.get("message_id")) in projected_message_ids
    )
    return CanonicalModelSurface(
        session_id=str(public["session_id"]),
        event_sequence=int(_event_value(ordered[-1], "sequence")),
        event_digest=event_prefix_digest(ordered),
        replacement_generation=len(replacements),
        messages=tuple(models),
        provider_replay=replay,
    )


def project_private_event_state(
    events: Iterable[AgentEvent | Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], tuple[ProviderReplayRecord, ...]]:
    """Project un-compacted private sidecars for rebuild/fork operations."""
    ordered = list(events)
    public = project_agent_events(ordered)
    public_message_ids = {
        str(message.get("id")) for message in public.get("messages") or []
    }
    known_message_ids = set(public_message_ids)
    for event in ordered:
        payload = _event_value(event, "payload")
        if not isinstance(payload, Mapping):
            continue
        message = payload.get("message")
        if isinstance(message, Mapping) and message.get("id"):
            known_message_ids.add(str(message["id"]))
        surface = payload.get("surface")
        if isinstance(surface, Mapping):
            known_message_ids.update(
                str(item.get("id"))
                for item in surface.get("messages") or []
                if isinstance(item, Mapping) and item.get("id")
            )
    identities: dict[str, dict[str, Any]] = {}
    provider_items: dict[str, dict[str, Any]] = {}
    has_seed = False
    for event in ordered:
        kind = str(_event_value(event, "kind"))
        payload = _json_copy(_event_value(event, "payload"))
        if kind in {"surface.seed", "surface.model_seed", "surface.model_import"}:
            raw_seed = payload.get("model")
            if raw_seed is None:
                continue
            seed = _validate_model_seed(raw_seed)
            has_seed = True
            for part_id, raw_identity in seed["part_replay"].items():
                identity = _validate_tool_identity(raw_identity)
                if identity is not None:
                    identities[str(part_id)] = identity
            for raw_item in seed["provider_replay"]:
                item = _validate_provider_replay(
                    raw_item,
                    session_message_ids=known_message_ids,
                )
                provider_items[str(item["id"])] = item
        elif kind in {
            "part.created", "part.updated", "step.started", "step.finished",
            "tool.called", "tool.updated", "tool.result",
        }:
            part = payload.get("part")
            part_id = str(part.get("id") or "") if isinstance(part, Mapping) else ""
            raw_model = payload.get("model")
            if isinstance(raw_model, Mapping) and "tool_identity" in raw_model:
                identity = _validate_tool_identity(raw_model.get("tool_identity"))
                if identity is None:
                    identities.pop(part_id, None)
                else:
                    identities[part_id] = identity
        elif kind == "provider.transcript":
            item = _validate_provider_replay(
                payload.get("provider_replay"),
                session_message_ids=known_message_ids,
            )
            provider_items[str(item["id"])] = item
        elif kind == "surface.messages_removed":
            removed = {str(item) for item in payload.get("message_ids") or []}
            for item_id, item in list(provider_items.items()):
                if str(item.get("message_id")) in removed:
                    provider_items.pop(item_id, None)
    if not has_seed:
        raise AgentEventProjectionError("canonical model seed is missing")
    visible_part_ids = {
        str(part.get("id"))
        for message in public.get("messages") or []
        for part in message.get("parts") or []
        if isinstance(part, Mapping)
    }
    identities = {
        part_id: deepcopy(identity)
        for part_id, identity in identities.items()
        if part_id in visible_part_ids
    }
    providers = tuple(
        ProviderReplayRecord(**item)
        for item in sorted(
            provider_items.values(),
            key=lambda value: (int(value["origin_seq"]), str(value["id"])),
        )
        if str(item["message_id"]) in public_message_ids
    )
    return identities, providers


def _balance_diagnostics(
    surface: Mapping[str, Any],
    events: Sequence[AgentEvent],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    excluded_message_ids = model_excluded_message_ids(events)
    started_turns: set[tuple[str, int, str]] = set()
    finished_turns: set[tuple[str, int, str]] = set()
    canonical_turn_by_run: dict[tuple[str, int], str] = {}
    for event in events:
        if (
            event.kind == "turn.started"
            and event.run_id
            and event.generation is not None
            and event.turn_id
        ):
            canonical_turn_by_run.setdefault(
                (event.run_id, int(event.generation)),
                event.turn_id,
            )
    for event in events:
        if not event.run_id or event.generation is None or not event.turn_id:
            continue
        run_identity = (event.run_id, int(event.generation))
        identity = (
            *run_identity,
            canonical_turn_by_run.get(run_identity, event.turn_id),
        )
        if event.kind == "turn.started":
            started_turns.add(identity)
        elif event.kind == "turn.finished":
            finished_turns.add(identity)

    # A terminal marker closes only the User boundary it actually answered.
    # Inbox steer can append another User to the same logical generation after
    # an earlier Assistant already emitted turn.finished. Re-open that turn
    # until a terminal Assistant covers the newest User.
    message_turn: dict[str, tuple[str, int, str]] = {}
    ambiguous_messages: set[str] = set()
    for event in events:
        if (
            event.kind not in {
                "message.created",
                "message.updated",
                "turn.started",
            }
            or not event.message_id
            or not event.run_id
            or event.generation is None
            or not event.turn_id
        ):
            continue
        run_identity = (event.run_id, int(event.generation))
        identity = (
            *run_identity,
            canonical_turn_by_run.get(run_identity, event.turn_id),
        )
        previous = message_turn.get(event.message_id)
        if previous is not None and previous != identity:
            ambiguous_messages.add(event.message_id)
        else:
            message_turn[event.message_id] = identity
    for message_id in ambiguous_messages:
        message_turn.pop(message_id, None)

    grouped: dict[tuple[str, int, str], list[Mapping[str, Any]]] = {}
    for message in surface.get("messages") or []:
        identity = message_turn.get(str(message.get("id") or ""))
        if identity is not None:
            grouped.setdefault(identity, []).append(message)
    for identity, group in grouped.items():
        semantic_group = [
            item
            for item in group
            if str(item.get("id") or "") not in excluded_message_ids
        ]
        # A fully failed attachment boundary remains a closed public audit
        # turn (User + synthetic error Assistant), while a mixed boundary is
        # judged solely by the Messages that were eligible for model input.
        if not semantic_group:
            semantic_group = group
        users = [
            item for item in semantic_group if item.get("role") == "user"
        ]
        last = semantic_group[-1] if semantic_group else None
        if (
            not users
            or last is None
            or last.get("role") != "assistant"
            or str(last.get("parent_id") or "")
            != str(users[-1].get("id") or "")
            or not _terminal_message_state(last)
        ):
            finished_turns.discard(identity)
    open_turn_ids = tuple(sorted(
        f"{run_id}:{generation}:{turn_id}"
        for run_id, generation, turn_id in started_turns
        if (run_id, generation, turn_id) not in finished_turns
    ))

    open_steps: list[str] = []
    open_tools: list[str] = []
    unfinished_messages: list[str] = []
    for message in surface.get("messages") or []:
        message_id = str(message.get("id") or "")
        if (
            message.get("role") == "assistant"
            and message.get("finish") is None
            and not message.get("error")
        ):
            unfinished_messages.append(message_id)
        starts: dict[int, int] = {}
        finishes: dict[int, int] = {}
        for part in message.get("parts") or []:
            data = part.get("data") if isinstance(part, dict) else None
            if not isinstance(data, dict):
                continue
            part_type = data.get("type") or part.get("type")
            if part_type == "step-start":
                step = int(data.get("step") or 0)
                starts[step] = starts.get(step, 0) + 1
            elif part_type == "step-finish":
                step = int(data.get("step") or 0)
                finishes[step] = finishes.get(step, 0) + 1
            elif part_type == "tool" and data.get("status") in {"pending", "running"}:
                open_tools.append(str(part.get("id") or data.get("id") or ""))
        for step, count in starts.items():
            if count > finishes.get(step, 0):
                open_steps.extend(
                    f"{message_id}:{step}" for _ in range(count - finishes.get(step, 0))
                )
    return (
        open_turn_ids,
        tuple(sorted(open_steps)),
        tuple(sorted(part_id for part_id in open_tools if part_id)),
        tuple(sorted(message_id for message_id in unfinished_messages if message_id)),
    )


async def bootstrap_agent_event_log(
    session_id: str,
    *,
    user_id: str,
) -> AgentEvent | None:
    """Seed one legacy Session without changing its live Surface."""
    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        seeded = await ensure_surface_seed_locked(db, session_row)
        await ensure_model_seed_locked(db, session_row)
        return seeded


async def _load_events_locked(
    db: AsyncSession,
    session_row: Session,
) -> list[AgentEvent]:
    events = list((await db.execute(select(AgentEvent).where(
        AgentEvent.session_id == session_row.id,
        AgentEvent.user_id == session_row.user_id,
    ).order_by(AgentEvent.sequence))).scalars().all())
    # Projection performs the authoritative continuity/schema checks.
    project_agent_events(events)
    return events


def _terminal_message_state(message: Mapping[str, Any]) -> bool:
    if message.get("error"):
        return True
    finish = message.get("finish")
    return bool(finish and finish not in {"tool_calls", "tool-calls", "compact"})


def _part_body(part: Mapping[str, Any]) -> dict[str, Any]:
    raw = part.get("data")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _step_balance(parts: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    starts: list[dict[str, Any]] = []
    finishes = 0
    for part in parts:
        data = _part_body(part)
        part_type = data.get("type") or part.get("type")
        if part_type == "step-start":
            starts.append(data)
        elif part_type == "step-finish":
            finishes += 1
    return starts, finishes


async def repair_canonical_tail_locked(
    db: AsyncSession,
    session_row: Session,
    *,
    run_fence: RunFence | None,
    target_user_message_id: str | None = None,
    allow_unanchored_assistant: bool = False,
) -> CanonicalTailRepair:
    """Conservatively close an old open tail before model context is served.

    The transaction is authorized by the current/maintenance fence, while
    appended lifecycle events retain the interrupted run's logical identity.
    An exact currently-active generation is never repaired underneath itself.
    """
    events = await _load_events_locked(db, session_row)
    public = project_agent_events(events)
    messages = list(public.get("messages") or [])
    if not messages:
        return CanonicalTailRepair()
    excluded_message_ids = model_excluded_message_ids(events)

    def model_turn_members(
        users: list[dict[str, Any]],
        assistants: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        visible_users = [
            item
            for item in users
            if str(item.get("id") or "") not in excluded_message_ids
        ]
        visible_assistants = [
            item
            for item in assistants
            if str(item.get("id") or "") not in excluded_message_ids
        ]
        if visible_users or visible_assistants:
            return visible_users, visible_assistants
        # A boundary whose every Message was excluded from model context is
        # still retained as a closed public audit turn. Never synthesize new
        # model-visible content for it during recovery.
        return users, assistants

    current_identity = (
        (run_fence[1], int(run_fence[2])) if run_fence is not None else None
    )
    current_trigger_message_id: str | None = None
    if run_fence is not None:
        # Task/direct triggers are accepted before Driver reservation, so the
        # immutable User event can legitimately be unowned. Once this exact
        # Driver is running/finalizing, associate its bound trigger with the
        # in-flight identity for repair grouping only. Reserved recovery stays
        # conservative unless its Event already proves the original identity.
        from db.models.agent_driver import AgentDriverState

        driver = (await db.execute(select(AgentDriverState).where(
            AgentDriverState.session_id == session_row.id,
            AgentDriverState.user_id == session_row.user_id,
            AgentDriverState.run_id == run_fence[1],
            AgentDriverState.generation == run_fence[2],
        ))).scalar_one_or_none()
        if (
            driver is not None
            and driver.phase in {"running", "finalizing"}
            and driver.trigger_message_id
        ):
            current_trigger_message_id = str(driver.trigger_message_id)
    if run_fence is None:
        from db.models.agent_driver import AgentDriverState
        from agent.driver import _aware, _database_now, _is_live

        driver = (await db.execute(select(AgentDriverState).where(
            AgentDriverState.session_id == session_row.id,
            AgentDriverState.user_id == session_row.user_id,
        ).with_for_update())).scalar_one_or_none()
        if driver is not None:
            clock_result = await db.execute(select(_database_now(db)))
            database_now = _aware(clock_result.scalar_one())
            clock_result.close()
            assert database_now is not None
            if _is_live(driver, database_now):
                return CanonicalTailRepair()

    started_by_message: dict[str, tuple[str, int, str]] = {}
    canonical_turn_by_run: dict[tuple[str, int], str] = {}
    for event in events:
        if (
            event.kind == "turn.started"
            and event.run_id
            and event.generation is not None
            and event.turn_id
        ):
            canonical_turn_by_run.setdefault(
                (str(event.run_id), int(event.generation)),
                str(event.turn_id),
            )
    message_turn: dict[str, tuple[str, int, str]] = {}
    for event in events:
        if not event.run_id or event.generation is None:
            continue
        run_identity = (str(event.run_id), int(event.generation))
        logical = (
            *run_identity,
            canonical_turn_by_run.get(
                run_identity,
                str(event.turn_id or event.message_id or ""),
            ),
        )
        if event.kind == "turn.started" and event.message_id:
            started_by_message[str(event.message_id)] = logical
        if event.kind in {"message.created", "message.updated"} and event.message_id:
            existing = message_turn.get(str(event.message_id))
            if existing is not None and existing != logical:
                raise AgentEventProjectionError("Message crosses logical Agent turns")
            message_turn[str(event.message_id)] = logical

    if current_trigger_message_id is not None and current_identity is not None:
        existing = message_turn.get(current_trigger_message_id)
        if existing is None:
            logical = (
                str(current_identity[0]),
                int(current_identity[1]),
                current_trigger_message_id,
            )
            canonical_turn_by_run.setdefault(current_identity, logical[2])
            message_turn[current_trigger_message_id] = logical
            started_by_message.setdefault(current_trigger_message_id, logical)

    groups: list[
        tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
            tuple[str, int, str] | None,
        ]
    ] = []
    index = 0
    while index < len(messages):
        identity = message_turn.get(str(messages[index].get("id") or ""))
        if identity is not None:
            end = index + 1
            while (
                end < len(messages)
                and message_turn.get(str(messages[end].get("id") or "")) == identity
            ):
                end += 1
            users: list[dict[str, Any]] = []
            assistants: list[dict[str, Any]] = []
            for item in messages[index:end]:
                role = str(item.get("role") or "")
                if role == "user":
                    users.append(item)
                elif role == "assistant":
                    assistants.append(item)
                else:
                    raise AgentEventProjectionError("logical turn has invalid role")
            if not users and assistants:
                if not allow_unanchored_assistant:
                    raise AgentEventProjectionError(
                        "assistant tail has no User turn anchor"
                    )
                parent_ids = {
                    str(item.get("parent_id") or "") for item in assistants
                }
                # An imported Assistant-only legacy tail has no SQL User row.
                # Older maintenance passes may already have attached one
                # deterministic logical identity; retain only that exact
                # unanchored shape. A non-empty parent is accepted solely when
                # it equals the logical turn id, never as a cross-turn alias.
                if parent_ids not in ({""}, {str(identity[2])}):
                    raise AgentEventProjectionError(
                        "Assistant parent crosses its logical turn"
                    )
                users = [{
                    "id": str(identity[2]),
                    "role": "user",
                    "agent": assistants[-1].get("agent"),
                    "model": assistants[-1].get("model"),
                    "client_message_id": None,
                    "_unanchored": True,
                }]
            if not users:
                raise AgentEventProjectionError("logical turn has no User Message")
            raw_user_ids = {str(item.get("id") or "") for item in users}
            if not users[-1].get("_unanchored") and any(
                str(item.get("parent_id") or "") not in raw_user_ids
                for item in assistants
            ):
                raise AgentEventProjectionError(
                    "Assistant parent crosses its logical turn"
                )
            users, assistants = model_turn_members(users, assistants)
            if not users:
                raise AgentEventProjectionError(
                    "model-visible Assistant has no User turn anchor"
                )
            user_ids = {str(item.get("id") or "") for item in users}
            if not users[-1].get("_unanchored") and any(
                str(item.get("parent_id") or "") not in user_ids
                for item in assistants
            ):
                raise AgentEventProjectionError(
                    "model-visible Assistant parent crosses its logical turn"
                )
            groups.append((users, assistants, identity))
            index = end
            continue

        users: list[dict[str, Any]] = []
        while index < len(messages) and str(messages[index].get("role")) == "user":
            users.append(messages[index])
            index += 1
        assistants: list[dict[str, Any]] = []
        while index < len(messages) and str(messages[index].get("role")) != "user":
            assistants.append(messages[index])
            index += 1
        if users:
            identities = [
                started_by_message[str(item.get("id") or "")]
                for item in users
                if str(item.get("id") or "") in started_by_message
            ]
            users, assistants = model_turn_members(users, assistants)
            if not users:
                raise AgentEventProjectionError(
                    "model-visible Assistant has no User turn anchor"
                )
            groups.append((users, assistants, identities[-1] if identities else None))
        elif assistants:
            if not allow_unanchored_assistant:
                # Imported/corrupt transcripts without a User anchor fail
                # closed on normal model loads. Explicit recovery of a known
                # legacy driver may still conservatively close its tools.
                raise AgentEventProjectionError("assistant tail has no User turn anchor")
            anchor = str(assistants[-1].get("parent_id") or "")
            groups.append(([{
                "id": anchor or f"legacy-anchor:{assistants[-1]['id']}",
                "role": "user",
                "agent": assistants[-1].get("agent"),
                "model": assistants[-1].get("model"),
                "client_message_id": None,
                "_unanchored": True,
            }], assistants, None))

    now = datetime.now(timezone.utc)
    repaired_tools = 0
    closed_steps = 0
    closed_messages = 0

    async def synthesize_aborted_reply(
        last_user: Mapping[str, Any],
        logical_fence: RunFence | None,
    ) -> None:
        nonlocal closed_messages
        terminal = Message(
            id=ascending("message"),
            session_id=session_row.id,
            user_id=session_row.user_id,
            role="assistant",
            agent=last_user.get("agent"),
            model_id=last_user.get("model"),
            parent_id=str(last_user["id"]),
            created_at=now,
        )
        db.add(terminal)
        await db.flush()
        await append_message_events_locked(
            db,
            session_row,
            terminal,
            operation="created",
            run_fence=logical_fence,
        )
        terminal.finish = "aborted"
        await append_message_events_locked(
            db,
            session_row,
            terminal,
            operation="updated",
            run_fence=logical_fence,
        )
        closed_messages += 1

    for users, assistants, logical in groups:
        last_user = users[-1]
        user_ids = [str(item.get("id") or "") for item in users]
        if target_user_message_id is not None and (
            target_user_message_id not in user_ids
            and not any(
                str(item.get("parent_id") or "") == target_user_message_id
                for item in assistants
            )
        ):
            continue
        if logical is not None and (logical[0], logical[1]) == current_identity:
            continue

        last_assistant = assistants[-1] if assistants else None
        open_parts = False
        for assistant in assistants:
            for part in assistant.get("parts") or []:
                if not isinstance(part, Mapping):
                    continue
                data = _part_body(part)
                status = getattr(data.get("status"), "value", data.get("status"))
                if (data.get("type") or part.get("type")) == "tool" and status in {
                    "pending", "running",
                }:
                    open_parts = True
            starts, finishes = _step_balance([
                part for part in assistant.get("parts") or []
                if isinstance(part, Mapping)
            ])
            open_parts = open_parts or len(starts) > finishes
        latest_user_parented = bool(
            last_user.get("_unanchored")
            or (
                last_assistant is not None
                and str(last_assistant.get("parent_id") or "")
                == str(last_user.get("id") or "")
            )
        )
        needs_repair = bool(
            not latest_user_parented
            or last_assistant is None
            or not _terminal_message_state(last_assistant)
            or open_parts
        )
        if not needs_repair:
            continue

        # A standalone interruption note is intentionally a context record,
        # not a prompt requiring an Assistant reply. It will be grouped with a
        # later real User Message; when it is the tail, simply leave it alone.
        if (
            not assistants
            and len(users) == 1
            and str(last_user.get("client_message_id") or "").startswith("tabort:")
        ):
            continue

        turn_id = logical[2] if logical is not None else str(last_user["id"])
        if logical is None and last_user.get("_unanchored"):
            # There is no original User/run identity to recover for an
            # imported Assistant-only tail. Repair its read model and append
            # mutation evidence, but do not fabricate a balanced Agent turn
            # around a User Message that never existed.
            logical_fence = None
        elif logical is None:
            logical = (
                "legacy-tail-" + hashlib.sha256(
                    f"{session_row.id}:{turn_id}".encode("utf-8")
                ).hexdigest()[:40],
                1,
                turn_id,
            )
            await append_agent_event_locked(
                db,
                session_row,
                kind="turn.started",
                payload={"message_id": str(last_user["id"]), "recovered": True},
                run_fence=(session_row.id, logical[0], logical[1]),
                turn_id=turn_id,
                message_id=str(last_user["id"]),
                idempotency_key=f"tail-repair-start:{turn_id}",
            )
            logical_fence = (session_row.id, logical[0], logical[1])
        else:
            logical_fence = (session_row.id, logical[0], logical[1])

        rows = list((await db.execute(select(Message).where(
            Message.id.in_([str(item["id"]) for item in assistants]),
            Message.session_id == session_row.id,
            Message.user_id == session_row.user_id,
        ).order_by(Message.created_at, Message.id))).scalars().all()) if assistants else []
        if len(rows) != len(assistants):
            raise AgentEventProjectionError("open-tail SQL Message read model drifted")

        if not rows:
            await synthesize_aborted_reply(last_user, logical_fence)
            continue

        for row in rows:
            part_rows = list((await db.execute(select(Part).where(
                Part.message_id == row.id,
                Part.session_id == session_row.id,
                Part.user_id == session_row.user_id,
            ).order_by(Part.created_at, Part.id))).scalars().all())
            starts: list[dict[str, Any]] = []
            finish_count = 0
            touched = False
            for part_row in part_rows:
                data = deepcopy(part_row.data or {})
                part_type = data.get("type") or part_row.type
                if part_type == "step-start":
                    starts.append(data)
                elif part_type == "step-finish":
                    finish_count += 1
                elif part_type == "tool":
                    status = getattr(data.get("status"), "value", data.get("status"))
                    if status not in {"pending", "running"}:
                        continue
                    code = "tool_not_started" if status == "pending" else "tool_outcome_unknown"
                    metadata = dict(data.get("metadata") or {})
                    metadata.update({"recovery_code": code, "recovered_at": now.isoformat()})
                    data.update({
                        "status": "error",
                        "error": (
                            "Tool was not started before recovery. It was not executed."
                            if status == "pending"
                            else "Tool outcome is unknown after recovery. Do not retry automatically; inspect external state or ask the user."
                        ),
                        "metadata": metadata,
                    })
                    part_row.data = data
                    await append_part_event_locked(
                        db, session_row, part_row, row,
                        operation="updated", run_fence=logical_fence,
                    )
                    repaired_tools += 1
                    touched = True
            for start in starts[finish_count:]:
                from models.message import StepFinishPart

                finish = StepFinishPart(
                    id=ascending("part"),
                    step=int(start.get("step") or 0),
                    session_id=session_row.id,
                    message_id=row.id,
                    duration=0.0,
                    snapshot=start.get("snapshot"),
                )
                finish_row = Part(
                    id=finish.id,
                    message_id=row.id,
                    session_id=session_row.id,
                    user_id=session_row.user_id,
                    type="step-finish",
                    data=finish.model_dump(),
                    created_at=now,
                )
                db.add(finish_row)
                await db.flush()
                await append_part_event_locked(
                    db, session_row, finish_row, row,
                    operation="created", run_fence=logical_fence,
                )
                closed_steps += 1
                touched = True
            if row is rows[-1] and not _terminal_message_state(public["messages"][
                next(i for i, item in enumerate(public["messages"]) if item["id"] == row.id)
            ]):
                row.finish = "aborted"
                touched = True
                closed_messages += 1
            if touched:
                await append_message_events_locked(
                    db, session_row, row, operation="updated", run_fence=logical_fence,
                )
        if not latest_user_parented:
            await synthesize_aborted_reply(last_user, logical_fence)
    return CanonicalTailRepair(
        repaired_tools=repaired_tools,
        closed_steps=closed_steps,
        closed_messages=closed_messages,
    )


async def load_canonical_model_surface(
    session_id: str,
    *,
    user_id: str,
    run_fence: RunFence | None = None,
    repair_tail: bool = True,
) -> CanonicalModelSurface:
    """Load model context only from one stable, owner-checked Event prefix."""
    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        await ensure_surface_seed_locked(db, session_row)
        await ensure_model_seed_locked(db, session_row)
        if repair_tail:
            await repair_canonical_tail_locked(
                db,
                session_row,
                run_fence=run_fence,
            )
        events = await _load_events_locked(db, session_row)
        return project_model_agent_events(events)


def model_tool_schema_digest(tools: Mapping[str, Any]) -> str:
    """Hash provider-visible tool schemas without storing the schemas again."""
    payload: dict[str, Any] = {}
    for name, tool in sorted(tools.items()):
        if hasattr(tool, "model_dump"):
            state = tool.model_dump(mode="json")
        elif hasattr(tool, "to_json"):
            state = tool.to_json()
        elif isinstance(tool, Mapping):
            state = dict(tool)
        else:
            parameters = getattr(tool, "parameters", {})
            if hasattr(parameters, "model_json_schema"):
                parameters = parameters.model_json_schema()
            state = {
                "name": getattr(tool, "name", name),
                "description": getattr(tool, "description", ""),
                "parameters": getattr(tool, "raw_schema", None) or parameters,
            }
        payload[str(name)] = _json_copy(state)
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def model_tool_definition_digest(
    definitions: Sequence[Mapping[str, Any]],
    *,
    alternate_definitions: Sequence[Sequence[Mapping[str, Any]]] = (),
) -> str:
    """Hash the exact ordered tool definitions handed to the provider.

    ``model_tool_schema_digest`` is useful before provider serialization.  A
    request checkpoint needs the stricter boundary: provider wrappers, the
    compatibility ``_noop`` definition, and native Tool Search entries are all
    part of the request shape.  Only this digest is persisted; definitions and
    their descriptions never enter ``model.requested``.
    """
    payload = {
        "version": 1,
        "primary": [_json_copy(dict(definition)) for definition in definitions],
        "alternates": [
            [_json_copy(dict(definition)) for definition in alternate]
            for alternate in alternate_definitions
        ],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def model_prompt_shape_digest(
    *,
    system: Sequence[str],
    messages: Sequence[Mapping[str, Any]],
    model_id: str,
    provider_binding_digest: str,
    tool_schema_digest: str,
    tool_choice: str | None,
    variant: str | None,
    prompt_cache_key: str = "",
    alternate_prompt_shape_digests: Sequence[str] = (),
) -> str:
    """Fingerprint the exact semantic arguments of one provider dispatch.

    The raw prompt (including resolved image data) is used only inside this
    one-way computation and is never returned or written to the Event Log.
    Binding and serialized-tool digests bind provider-only request structure
    without copying credentials, headers, or tool descriptions into the
    checkpoint payload.
    """
    for name, value in {
        "provider_binding_digest": provider_binding_digest,
        "tool_schema_digest": tool_schema_digest,
    }.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(value or "").lower()):
            raise ValueError(f"{name} must be a full lowercase SHA-256 digest")
    alternates = []
    for value in alternate_prompt_shape_digests:
        digest = str(value or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(
                "alternate_prompt_shape_digests must contain full lowercase "
                "SHA-256 digests"
            )
        alternates.append(digest)
    payload = {
        "version": 1,
        "system": _json_copy(list(system)),
        "messages": _json_copy([dict(message) for message in messages]),
        "model_id": str(model_id),
        "provider_binding_digest": provider_binding_digest.lower(),
        "tool_schema_digest": tool_schema_digest.lower(),
        "tool_choice": tool_choice,
        "variant": variant,
        # This is already a salted, non-reversible tenant/session affinity
        # digest. Binding it here proves the request-level cache parameter
        # without persisting raw tenant identity.
        "prompt_cache_key": str(prompt_cache_key or ""),
        "alternate_prompt_shape_digests": alternates,
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


async def checkpoint_model_request(
    session_id: str,
    *,
    user_id: str,
    run_fence: RunFence,
    request_id: str,
    model_id: str,
    provider_binding_digest: str,
    tool_schema_digest: str,
    prompt_shape_digest: str,
    expected_event_sequence: int,
    expected_event_digest: str,
    turn_id: str | None = None,
    step_id: str | None = None,
    message_id: str | None = None,
) -> CanonicalModelSurface:
    """CAS and cite the exact Event prefix immediately before dispatch.

    Callers must first build their complete provider request from a canonical
    candidate Surface.  This transaction refuses to append a checkpoint if
    any Event arrived while that request was being built; the caller must
    re-project and rebuild instead of sending the stale payload.
    """
    digests = {
        "provider_binding_digest": provider_binding_digest,
        "tool_schema_digest": tool_schema_digest,
        "prompt_shape_digest": prompt_shape_digest,
        "expected_event_digest": expected_event_digest,
    }
    for name, value in digests.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(value or "").lower()):
            raise ValueError(f"{name} must be a full lowercase SHA-256 digest")
    if not isinstance(expected_event_sequence, int) or expected_event_sequence < 1:
        raise ValueError("expected_event_sequence must be a positive integer")
    if not request_id or len(request_id) > 256:
        raise ValueError("request_id must be 1..256 characters")

    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        await ensure_surface_seed_locked(db, session_row)
        await ensure_model_seed_locked(db, session_row)
        events = await _load_events_locked(db, session_row)
        snapshot = project_model_agent_events(events)
        if (
            snapshot.event_sequence != expected_event_sequence
            or snapshot.event_digest != expected_event_digest.lower()
        ):
            raise AgentEventPrefixDriftError(
                "Agent event prefix drift before model dispatch: "
                f"expected sequence {expected_event_sequence} digest "
                f"{expected_event_digest.lower()}, found sequence "
                f"{snapshot.event_sequence} digest {snapshot.event_digest}"
            )
        _, request_run_id, request_generation = run_fence
        checkpoint_turn_id = next((
            str(event.turn_id)
            for event in events
            if event.kind == "turn.started"
            and event.run_id == request_run_id
            and event.generation == request_generation
            and event.turn_id
        ), turn_id)
        payload: dict[str, Any] = {
            "request_id": request_id,
            "session_id": session_id,
            "event_sequence": snapshot.event_sequence,
            "event_digest": snapshot.event_digest,
            "replacement_generation": snapshot.replacement_generation,
            "model_id": model_id,
            "provider_binding_digest": provider_binding_digest.lower(),
            "tool_schema_digest": tool_schema_digest.lower(),
            "prompt_shape_digest": prompt_shape_digest.lower(),
        }
        await append_agent_event_locked(
            db,
            session_row,
            kind="model.requested",
            payload=payload,
            run_fence=run_fence,
            turn_id=checkpoint_turn_id,
            step_id=step_id,
            message_id=message_id,
            idempotency_key=f"model-request:{request_id}",
        )
        return snapshot


def _parse_iso(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AgentEventProjectionError("canonical timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentEventProjectionError("canonical timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def rebuild_sql_read_model_from_events(
    session_id: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    """Recreate all three mutable transcript tables from canonical events."""
    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        events = await _load_events_locked(db, session_row)
        public = project_agent_events(events)
        model = project_model_agent_events(events)

        identities: dict[str, dict[str, Any]] = {}
        providers: dict[str, dict[str, Any]] = {}
        public_message_ids = {
            str(item.get("id")) for item in public.get("messages") or []
        }
        known_messages = set(public_message_ids)
        for event in events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            message_state = payload.get("message")
            if isinstance(message_state, Mapping) and message_state.get("id"):
                known_messages.add(str(message_state["id"]))
            seed_surface = payload.get("surface")
            if isinstance(seed_surface, Mapping):
                known_messages.update(
                    str(item.get("id"))
                    for item in seed_surface.get("messages") or []
                    if isinstance(item, Mapping) and item.get("id")
                )
        for event in events:
            payload = _json_copy(event.payload)
            if event.kind in {"surface.seed", "surface.model_seed", "surface.model_import"}:
                raw_seed = payload.get("model")
                if isinstance(raw_seed, Mapping):
                    seed = _validate_model_seed(raw_seed)
                    for part_id, raw_identity in seed["part_replay"].items():
                        identity = _validate_tool_identity(raw_identity)
                        if identity is not None:
                            identities[str(part_id)] = identity
                    for raw_item in seed["provider_replay"]:
                        item = _validate_provider_replay(
                            raw_item,
                            session_message_ids=known_messages,
                        )
                        providers[str(item["id"])] = item
            elif event.kind in {
                "part.created", "part.updated", "step.started", "step.finished",
                "tool.called", "tool.updated", "tool.result",
            }:
                part = payload.get("part")
                part_id = str(part.get("id") or "") if isinstance(part, Mapping) else ""
                raw_model = payload.get("model")
                if isinstance(raw_model, Mapping) and "tool_identity" in raw_model:
                    identity = _validate_tool_identity(raw_model.get("tool_identity"))
                    if identity is None:
                        identities.pop(part_id, None)
                    else:
                        identities[part_id] = identity
            elif event.kind == "provider.transcript":
                item = _validate_provider_replay(
                    payload.get("provider_replay"),
                    session_message_ids=known_messages,
                )
                providers[str(item["id"])] = item
            elif event.kind == "surface.messages_removed":
                removed = {str(value) for value in payload.get("message_ids") or []}
                for item_id, item in list(providers.items()):
                    if str(item.get("message_id")) in removed:
                        providers.pop(item_id, None)

        await db.execute(delete(InternalPart).where(
            InternalPart.session_id == session_id,
            InternalPart.user_id == user_id,
        ))
        await db.execute(delete(Part).where(
            Part.session_id == session_id,
            Part.user_id == user_id,
        ))
        await db.execute(delete(Message).where(
            Message.session_id == session_id,
            Message.user_id == user_id,
        ))
        for message in public.get("messages") or []:
            role = str(message.get("role") or "")
            row = Message(
                id=str(message["id"]),
                session_id=session_id,
                user_id=user_id,
                role=role,
                client_message_id=message.get("client_message_id"),
                agent=message.get("agent"),
                model=message.get("model") if role == "user" else None,
                model_id=message.get("model") if role == "assistant" else None,
                variant=message.get("variant"),
                parent_id=message.get("parent_id"),
                tokens=deepcopy(message.get("tokens")),
                finish=message.get("finish"),
                summary=message.get("summary"),
                error=deepcopy(message.get("error")),
                reaction=message.get("reaction"),
                format=deepcopy(message.get("format")),
                structured=deepcopy(message.get("structured")),
                created_at=_parse_iso(message.get("created_at")),
            )
            db.add(row)
            for part in message.get("parts") or []:
                part_id = str(part["id"])
                db.add(Part(
                    id=part_id,
                    message_id=row.id,
                    session_id=session_id,
                    user_id=user_id,
                    type=str(part.get("type") or "text"),
                    data=deepcopy(dict(part.get("data") or {})),
                    **identities.get(part_id, {}),
                    created_at=_parse_iso(part.get("created_at")),
                ))
        await db.flush()
        for item in sorted(providers.values(), key=lambda value: value["origin_seq"]):
            if str(item["message_id"]) not in public_message_ids:
                continue
            db.add(InternalPart(
                id=str(item["id"]),
                session_id=session_id,
                message_id=str(item["message_id"]),
                user_id=user_id,
                kind=str(item["kind"]),
                capability_key_digest=str(item["capability_key_digest"]),
                response_chain_id=str(item["response_chain_id"]),
                stream_seq=int(item["stream_seq"]),
                origin_seq=int(item["origin_seq"]),
                dedupe_key=item.get("dedupe_key"),
                data=deepcopy(item["data"]),
                created_at=_parse_iso(item["created_at"]),
            ))
        await db.flush()
        rebuilt = await _surface_snapshot_locked(db, session_row)
        if rebuilt != public:
            raise AgentEventProjectionError("rebuilt SQL Surface differs from events")
        # Force private projection validation before committing the rebuilt rows.
        if model.session_id != session_id:
            raise AgentEventProjectionError("rebuilt model Surface crosses Session")
        return rebuilt


async def verify_agent_event_parity(
    session_id: str,
    *,
    user_id: str,
    require_closed: bool = True,
) -> AgentEventParityReport:
    """Compare a pure event projection with the current SQL read model.

    ``require_closed`` additionally rejects open turn/step/tool/message tails.
    This catches a retry or crash that left orphan assistant attempts even when
    the immutable log and mutable tables faithfully agree about those rows.
    """
    async with get_db_session() as db:
        session_row = (
            await db.execute(select(Session).where(
                Session.id == session_id,
                Session.user_id == user_id,
                Session.is_deleted == False,  # noqa: E712
            ))
        ).scalar_one_or_none()
        if session_row is None:
            raise LookupError("session not found")
        events = list((await db.execute(
            select(AgentEvent).where(
                AgentEvent.session_id == session_id,
                AgentEvent.user_id == user_id,
            ).order_by(AgentEvent.sequence)
        )).scalars().all())
        live = await _surface_snapshot_locked(db, session_row)

    tracked = bool(events)
    last_sequence = int(events[-1].sequence) if events else 0
    contiguous = bool(events) and [event.sequence for event in events] == list(
        range(1, len(events) + 1)
    )
    error: str | None = None
    projected: dict[str, Any] | None = None
    if events:
        try:
            projected = project_agent_events(events)
        except AgentEventProjectionError as exc:
            error = str(exc)
    projection_matches = projected == live if projected is not None else False
    diagnostics_surface = projected if projected is not None else live
    open_turns, open_steps, open_tools, unfinished = _balance_diagnostics(
        diagnostics_surface,
        events,
    )
    balanced = not (open_turns or open_steps or open_tools or unfinished)
    ok = tracked and contiguous and projection_matches and (
        balanced or not require_closed
    )
    return AgentEventParityReport(
        session_id=session_id,
        tracked=tracked,
        event_count=len(events),
        last_sequence=last_sequence,
        sequence_contiguous=contiguous,
        projection_matches=projection_matches,
        balanced=balanced,
        require_closed=require_closed,
        open_turn_ids=open_turns,
        open_step_ids=open_steps,
        open_tool_part_ids=open_tools,
        unfinished_message_ids=unfinished,
        error=error,
        ok=ok,
    )
