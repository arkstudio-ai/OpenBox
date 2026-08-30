"""Private persistence for deferred-tool exposure and provider replay.

This module is the *only* supported read/write path for ``internal_parts``.
It deliberately has no event-bus dependency: opaque provider blocks and reveal
evidence must never appear in REST/SSE payloads.  PostgreSQL mutations lock the
session row; the single-process desktop SQLite deployment additionally uses an
application guard plus ``BEGIN IMMEDIATE`` as the database-equivalent write
fence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.internal_part import InternalPart
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session


log = create_logger("session.internal_parts")

TOOL_REVEAL_KIND = "tool_reveal"
PROVIDER_TRANSCRIPT_KIND = "provider_transcript"
STATE_VERSION = 1
DEFAULT_REVEAL_TTL_SECONDS = 30 * 60
DEFAULT_MAX_REVEALS_PER_AGENT = 8
DEFAULT_PROVIDER_FALLBACK_TTL_SECONDS = 30 * 60
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[^\x00-\x20\x7f]{1,256}$")
_PORTABLE_BINDING_BYTES = b"openbox:portable-tool-reveal:v1"
PORTABLE_CAPABILITY_KEY_DIGEST = hashlib.sha256(_PORTABLE_BINDING_BYTES).hexdigest()

FaultInjector = Callable[[str], None]


class ProviderCapabilityBinding(BaseModel):
    """Every provider dimension that controls opaque transcript replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=512)
    account_id: str = Field(min_length=1, max_length=256)
    api_version: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    dialect: str = Field(min_length=1, max_length=64)
    beta_headers: tuple[str, ...] = ()

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload["beta_headers"] = sorted(set(payload["beta_headers"]))
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ToolRevealEvent(BaseModel):
    """Validated evidence accepted by the single reveal commit boundary.

    Ordinary tool-result metadata cannot instantiate a reveal implicitly; a
    caller must construct this reserved typed outcome with all scope bindings.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=64)
    origin_part_id: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(min_length=1, max_length=64)
    canonical_tool_id: str = Field(min_length=1, max_length=256)
    schema_digest: str
    catalog_generation: str = Field(min_length=1, max_length=256)
    evidence_source: Literal["portable", "native"]
    stream_seq: int = Field(ge=0)
    capability_key_digest: str = PORTABLE_CAPABILITY_KEY_DIGEST
    response_chain_id: str | None = Field(default=None, max_length=128)

    @field_validator("schema_digest", "capability_key_digest")
    @classmethod
    def _full_digest(cls, value: str) -> str:
        value = value.lower()
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("must be a full lowercase SHA-256 digest")
        return value

    @field_validator("canonical_tool_id")
    @classmethod
    def _bounded_canonical_id(cls, value: str) -> str:
        if not _SAFE_ID_RE.fullmatch(value):
            raise ValueError("canonical tool id contains whitespace/control bytes or is too long")
        return value


class InternalPartRecord(BaseModel):
    """Private return type; it is intentionally absent from API schemas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    session_id: str
    message_id: str
    kind: str
    stream_seq: int
    origin_seq: int
    data: dict[str, Any]
    created_at: datetime


class RevealCommitResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    created: bool
    origin_seq: int
    state: dict[str, Any]


_guard_map_lock = asyncio.Lock()
_session_guards: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def session_exposure_lock(session_id: str):
    """Serialize exposure/branch mutations in the desktop process.

    PostgreSQL's row lock remains the cross-process authority.  SQLite has no
    row-level lock, so this guard prevents same-process interleaving while
    ``BEGIN IMMEDIATE`` fences other writers at the database level.
    """
    async with _guard_map_lock:
        guard = _session_guards.setdefault(session_id, asyncio.Lock())
    async with guard:
        yield
    async with _guard_map_lock:
        if not guard.locked() and not getattr(guard, "_waiters", None):
            _session_guards.pop(session_id, None)


async def begin_session_write(db: AsyncSession) -> None:
    """Acquire SQLite's write fence before the first read in a transaction."""
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))


async def lock_owned_session(
    db: AsyncSession,
    session_id: str,
    user_id: str,
    *,
    include_deleted: bool = False,
) -> Session:
    conditions = [Session.id == session_id, Session.user_id == user_id]
    if not include_deleted:
        conditions.append(Session.is_deleted == False)  # noqa: E712
    row = (
        await db.execute(select(Session).where(*conditions).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        # Do not distinguish a missing session from another user's session.
        raise LookupError("session not found")
    return row


def _empty_state(*, next_origin_seq: int = 1, provider_fallback: Mapping[str, Any] | None = None) -> dict:
    return {
        "v": STATE_VERSION,
        "next_origin_seq": max(1, next_origin_seq),
        "agents": {},
        "provider_fallback": deepcopy(dict(provider_fallback or {})),
    }


def _normalize_state(raw: Any) -> dict:
    if not isinstance(raw, dict) or raw.get("v") != STATE_VERSION:
        return _empty_state()
    next_seq = raw.get("next_origin_seq", 1)
    if not isinstance(next_seq, int) or isinstance(next_seq, bool) or next_seq < 1:
        next_seq = 1
    agents = raw.get("agents")
    fallback = raw.get("provider_fallback")
    return {
        "v": STATE_VERSION,
        "next_origin_seq": next_seq,
        "agents": deepcopy(agents) if isinstance(agents, dict) else {},
        "provider_fallback": deepcopy(fallback) if isinstance(fallback, dict) else {},
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def get_provider_fallback_status(
    *,
    session_id: str,
    user_id: str,
    capability_key_digest: str,
    now: datetime | None = None,
) -> tuple[Literal["supported", "unsupported"], datetime, str] | None:
    """Return one unexpired native capability result from private session state."""

    digest = str(capability_key_digest).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("capability_key_digest must be a full SHA-256 digest")
    current = now or datetime.now(timezone.utc)
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(Session).where(
                    Session.id == session_id,
                    Session.user_id == user_id,
                    Session.is_deleted == False,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        state = _normalize_state(row.tool_exposure_state)
        raw = state.get("provider_fallback", {}).get(digest)
        if not isinstance(raw, dict) or raw.get("status") not in {
            "supported",
            "unsupported",
        }:
            return None
        expires_at = _parse_time(raw.get("expires_at"))
        if expires_at is None or expires_at <= current:
            return None
        return raw["status"], expires_at, str(raw.get("reason") or "")[:256]


async def set_provider_fallback_status(
    *,
    session_id: str,
    user_id: str,
    capability_key_digest: str,
    status: Literal["supported", "unsupported"],
    ttl_seconds: int = DEFAULT_PROVIDER_FALLBACK_TTL_SECONDS,
    reason: str = "",
    now: datetime | None = None,
    max_entries: int = 64,
) -> dict[str, Any]:
    """Atomically persist one binding-scoped native capability result."""

    digest = str(capability_key_digest).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("capability_key_digest must be a full SHA-256 digest")
    if status not in {"supported", "unsupported"}:
        raise ValueError("invalid provider fallback status")
    if not 1 <= ttl_seconds <= 86_400:
        raise ValueError("ttl_seconds must be between 1 and 86400")
    if not 1 <= max_entries <= 256:
        raise ValueError("max_entries must be between 1 and 256")
    current = now or datetime.now(timezone.utc)
    expires_at = current + timedelta(seconds=ttl_seconds)
    committed: dict[str, Any] | None = None
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            row = await lock_owned_session(db, session_id, user_id)
            state = _normalize_state(row.tool_exposure_state)
            fallback = state["provider_fallback"]
            for key, raw in list(fallback.items()):
                expiry = _parse_time(raw.get("expires_at")) if isinstance(raw, dict) else None
                if expiry is None or expiry <= current:
                    fallback.pop(key, None)
            fallback[digest] = {
                "status": status,
                "expires_at": _iso(expires_at),
                "reason": str(reason)[:256],
            }
            if len(fallback) > max_entries:
                ranked = sorted(
                    fallback.items(),
                    key=lambda item: (
                        _parse_time(item[1].get("expires_at"))
                        or datetime.min.replace(tzinfo=timezone.utc),
                        item[0],
                    ),
                    reverse=True,
                )
                state["provider_fallback"] = dict(ranked[:max_entries])
            row.tool_exposure_state = state
            committed = deepcopy(state)
    assert committed is not None
    return committed


def _call_fault(injector: FaultInjector | None, stage: str) -> None:
    if injector is not None:
        injector(stage)


def _dedupe_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _require_owned_message(
    db: AsyncSession,
    *,
    session_id: str,
    message_id: str,
    user_id: str,
) -> None:
    found = (
        await db.execute(
            select(Message.id).where(
                Message.id == message_id,
                Message.session_id == session_id,
                Message.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise LookupError("message not found")


async def _require_origin_part(
    db: AsyncSession,
    *,
    session_id: str,
    message_id: str,
    user_id: str,
    part_id: str,
    evidence_source: Literal["portable", "native"],
) -> None:
    public = (
        await db.execute(
            select(Part).where(
                Part.id == part_id,
                Part.session_id == session_id,
                Part.message_id == message_id,
                Part.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if public is not None:
        if (
            evidence_source == "portable"
            and public.type == "tool"
            and isinstance(public.data, dict)
            and public.data.get("tool") == "capability_search"
        ):
            return
        raise ValueError("invalid reveal origin part")
    private = (
        await db.execute(
            select(InternalPart).where(
                InternalPart.id == part_id,
                InternalPart.session_id == session_id,
                InternalPart.message_id == message_id,
                InternalPart.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if private is None:
        raise LookupError("origin part not found")
    if not (
        evidence_source == "native"
        and private.kind.startswith("provider_")
        and isinstance(private.data, dict)
        and private.data.get("type") == "tool_revealed"
    ):
        raise ValueError("invalid reveal origin part")


async def _allocate_origin_seq(db: AsyncSession, session_row: Session, state: dict) -> int:
    max_stored = (
        await db.execute(
            select(func.max(InternalPart.origin_seq)).where(
                InternalPart.session_id == session_row.id
            )
        )
    ).scalar_one_or_none()
    candidate = max(state["next_origin_seq"], (max_stored or 0) + 1)
    state["next_origin_seq"] = candidate + 1
    return candidate


def _record(row: InternalPart) -> InternalPartRecord:
    return InternalPartRecord(
        id=row.id,
        session_id=row.session_id,
        message_id=row.message_id,
        kind=row.kind,
        stream_seq=row.stream_seq,
        origin_seq=row.origin_seq,
        data=deepcopy(row.data),
        created_at=row.created_at,
    )


async def save_internal_part(
    *,
    session_id: str,
    user_id: str,
    message_id: str,
    kind: str,
    data: Mapping[str, Any],
    binding: ProviderCapabilityBinding,
    capability_key_digest: str | None = None,
    response_chain_id: str,
    stream_seq: int,
    idempotency_key: str | None = None,
    _fault_injector: FaultInjector | None = None,
) -> InternalPartRecord:
    """Persist an opaque provider block without publishing any public event."""
    if kind == TOOL_REVEAL_KIND:
        raise ValueError("tool reveal rows must use commit_tool_reveal()")
    if not kind.startswith("provider_") or len(kind) > 40:
        raise ValueError("internal provider kind must start with 'provider_'")
    if not response_chain_id or len(response_chain_id) > 128:
        raise ValueError("response_chain_id must be 1..128 characters")
    if stream_seq < 0:
        raise ValueError("stream_seq must be non-negative")
    # Round-trip through JSON now so unsupported objects fail before acquiring
    # a row lock, and strip PostgreSQL-forbidden NUL bytes.
    clean_data = json.loads(json.dumps(dict(data), ensure_ascii=False).replace("\\u0000", ""))
    binding_digest = binding.digest()
    storage_digest = (
        str(capability_key_digest).lower()
        if capability_key_digest is not None
        else binding_digest
    )
    if not _SHA256_RE.fullmatch(storage_digest):
        raise ValueError("capability_key_digest must be a full SHA-256 digest")
    dedupe_key = None
    if idempotency_key is not None:
        if not idempotency_key or len(idempotency_key) > 512:
            raise ValueError("idempotency_key must be 1..512 characters")
        dedupe_key = _dedupe_digest(
            {
                "kind": kind,
                "binding": storage_digest,
                "chain": response_chain_id,
                "key": idempotency_key,
            }
        )

    committed: InternalPartRecord | None = None
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            session_row = await lock_owned_session(db, session_id, user_id)
            await _require_owned_message(
                db,
                session_id=session_id,
                message_id=message_id,
                user_id=user_id,
            )
            if dedupe_key:
                existing = (
                    await db.execute(
                        select(InternalPart).where(
                            InternalPart.session_id == session_id,
                            InternalPart.dedupe_key == dedupe_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if (
                        existing.message_id != message_id
                        or existing.kind != kind
                        or existing.stream_seq != stream_seq
                        or existing.data != clean_data
                    ):
                        raise ValueError("internal part idempotency conflict")
                    return _record(existing)

            state = _normalize_state(session_row.tool_exposure_state)
            origin_seq = await _allocate_origin_seq(db, session_row, state)
            _call_fault(_fault_injector, "before_insert")
            row = InternalPart(
                id=ascending("ipart"),
                session_id=session_id,
                message_id=message_id,
                user_id=user_id,
                kind=kind,
                capability_key_digest=storage_digest,
                response_chain_id=response_chain_id,
                stream_seq=stream_seq,
                origin_seq=origin_seq,
                dedupe_key=dedupe_key,
                data=clean_data,
                created_at=datetime.now(timezone.utc),
            )
            db.add(row)
            await db.flush()
            _call_fault(_fault_injector, "after_insert")
            session_row.tool_exposure_state = state
            _call_fault(_fault_injector, "before_commit")
            committed = _record(row)
    assert committed is not None
    return committed


def _apply_reveal(
    state: dict,
    *,
    event_data: Mapping[str, Any],
    origin_seq: int,
    now: datetime,
    max_reveals: int,
) -> None:
    agent_id = event_data["agent_id"]
    generation = event_data["catalog_generation"]
    agents = state["agents"]
    agent_state = agents.get(agent_id)
    if not isinstance(agent_state, dict) or agent_state.get("catalog_generation") != generation:
        agent_state = {"catalog_generation": generation, "revealed": {}}
        agents[agent_id] = agent_state
    revealed = agent_state.get("revealed")
    if not isinstance(revealed, dict):
        revealed = {}
        agent_state["revealed"] = revealed

    # TTL is checked both during commit and projection rebuild, so stale rows
    # never reappear after a branch mutation.
    for tool_id, record in list(revealed.items()):
        expires_at = _parse_time(record.get("expires_at")) if isinstance(record, dict) else None
        if expires_at is None or expires_at <= now:
            revealed.pop(tool_id, None)

    tool_id = event_data["canonical_tool_id"]
    revealed[tool_id] = {
        "schema_digest": event_data["schema_digest"],
        "last_used_at": event_data["occurred_at"],
        "expires_at": event_data["expires_at"],
        "origin_message_id": event_data["origin_message_id"],
        "origin_part_id": event_data["origin_part_id"],
        "origin_seq": origin_seq,
    }
    if len(revealed) > max_reveals:
        ranked = sorted(
            revealed.items(),
            key=lambda item: (
                _parse_time(item[1].get("last_used_at")) or datetime.min.replace(tzinfo=timezone.utc),
                item[1].get("origin_seq", 0),
                item[0],
            ),
            reverse=True,
        )
        agent_state["revealed"] = dict(ranked[:max_reveals])


def _reveal_descriptor(event: ToolRevealEvent) -> dict[str, Any]:
    response_chain_id = event.response_chain_id or f"reveal:{event.message_id}"
    evidence_key = _dedupe_digest(
        {
            "kind": TOOL_REVEAL_KIND,
            "agent": event.agent_id,
            "tool": event.canonical_tool_id,
            "schema": event.schema_digest,
            "generation": event.catalog_generation,
            "source": event.evidence_source,
            "message": event.message_id,
            "part": event.origin_part_id,
            "binding": event.capability_key_digest,
            "chain": response_chain_id,
        }
    )
    expected_fields = {
        "agent_id": event.agent_id,
        "canonical_tool_id": event.canonical_tool_id,
        "schema_digest": event.schema_digest,
        "catalog_generation": event.catalog_generation,
        "evidence_source": event.evidence_source,
        "origin_message_id": event.message_id,
        "origin_part_id": event.origin_part_id,
    }
    return {
        "event": event,
        "response_chain_id": response_chain_id,
        "evidence_key": evidence_key,
        "expected_fields": expected_fields,
        # The evidence key intentionally omits stream_seq so a retry that
        # changes ordering is detected as a conflict rather than a new event.
        "semantic": {
            **expected_fields,
            "message_id": event.message_id,
            "user_id": event.user_id,
            "stream_seq": event.stream_seq,
            "capability_key_digest": event.capability_key_digest,
            "response_chain_id": response_chain_id,
        },
    }


def _validate_existing_reveal(existing: InternalPart, descriptor: Mapping[str, Any]) -> None:
    event: ToolRevealEvent = descriptor["event"]
    expected_fields: Mapping[str, Any] = descriptor["expected_fields"]
    if (
        existing.kind != TOOL_REVEAL_KIND
        or existing.user_id != event.user_id
        or existing.message_id != event.message_id
        or existing.capability_key_digest != event.capability_key_digest
        or existing.response_chain_id != descriptor["response_chain_id"]
        or existing.stream_seq != event.stream_seq
        or not isinstance(existing.data, dict)
        or any(existing.data.get(key) != value for key, value in expected_fields.items())
    ):
        raise ValueError("tool reveal idempotency conflict")


async def commit_tool_reveals(
    events: Sequence[ToolRevealEvent],
    *,
    ttl_seconds: int = DEFAULT_REVEAL_TTL_SECONDS,
    max_reveals: int = DEFAULT_MAX_REVEALS_PER_AGENT,
    _fault_injector: FaultInjector | None = None,
) -> tuple[RevealCommitResult, ...]:
    """Atomically validate and commit one capability-result reveal batch.

    Every event is ownership/origin/idempotency checked before the first row
    is inserted. New ledger rows and the set-union projection then share one
    session lock and one database transaction, so a failure on item N rolls
    back items 0..N-1 as well as the JSON projection.
    """
    if not 1 <= ttl_seconds <= 86_400:
        raise ValueError("ttl_seconds must be between 1 and 86400")
    if not 1 <= max_reveals <= 64:
        raise ValueError("max_reveals must be between 1 and 64")
    batch = tuple(events)
    if not batch:
        return ()
    if any(not isinstance(event, ToolRevealEvent) for event in batch):
        raise TypeError("events must contain ToolRevealEvent values")
    session_id = batch[0].session_id
    user_id = batch[0].user_id
    if any(event.session_id != session_id or event.user_id != user_id for event in batch):
        raise ValueError("tool reveal batch must belong to one session owner")

    descriptors = tuple(_reveal_descriptor(event) for event in batch)
    unique_descriptors: dict[str, dict[str, Any]] = {}
    for descriptor in descriptors:
        evidence_key = descriptor["evidence_key"]
        previous = unique_descriptors.get(evidence_key)
        if previous is not None:
            if previous["semantic"] != descriptor["semantic"]:
                raise ValueError("tool reveal idempotency conflict")
            continue
        unique_descriptors[evidence_key] = descriptor

    committed: tuple[RevealCommitResult, ...] | None = None
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            session_row = await lock_owned_session(db, session_id, user_id)

            # Validate the entire ownership/origin surface before allocating a
            # sequence or adding an ORM row. Duplicate origins are checked once.
            checked_messages: set[str] = set()
            checked_origins: set[tuple[str, str, str]] = set()
            for descriptor in unique_descriptors.values():
                event = descriptor["event"]
                if event.message_id not in checked_messages:
                    await _require_owned_message(
                        db,
                        session_id=session_id,
                        message_id=event.message_id,
                        user_id=user_id,
                    )
                    checked_messages.add(event.message_id)
                origin_key = (
                    event.message_id,
                    event.origin_part_id,
                    event.evidence_source,
                )
                if origin_key not in checked_origins:
                    await _require_origin_part(
                        db,
                        session_id=session_id,
                        message_id=event.message_id,
                        user_id=user_id,
                        part_id=event.origin_part_id,
                        evidence_source=event.evidence_source,
                    )
                    checked_origins.add(origin_key)

            evidence_keys = tuple(unique_descriptors)
            existing_rows = list((await db.execute(
                select(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.dedupe_key.in_(evidence_keys),
                )
            )).scalars().all())
            existing_by_key = {str(row.dedupe_key): row for row in existing_rows}
            for evidence_key, descriptor in unique_descriptors.items():
                existing = existing_by_key.get(evidence_key)
                if existing is not None:
                    _validate_existing_reveal(existing, descriptor)

            state = _normalize_state(session_row.tool_exposure_state)
            now = datetime.now(timezone.utc)
            new_by_key: dict[str, tuple[InternalPart, dict[str, Any]]] = {}
            for item_index, (evidence_key, descriptor) in enumerate(
                unique_descriptors.items()
            ):
                if evidence_key in existing_by_key:
                    continue
                event = descriptor["event"]
                origin_seq = await _allocate_origin_seq(db, session_row, state)
                event_data = {
                    **descriptor["expected_fields"],
                    "occurred_at": _iso(now),
                    "expires_at": _iso(now + timedelta(seconds=ttl_seconds)),
                }
                _call_fault(_fault_injector, "before_insert")
                _call_fault(_fault_injector, f"before_insert:{item_index}")
                row = InternalPart(
                    id=ascending("ipart"),
                    session_id=session_id,
                    message_id=event.message_id,
                    user_id=user_id,
                    kind=TOOL_REVEAL_KIND,
                    capability_key_digest=event.capability_key_digest,
                    response_chain_id=descriptor["response_chain_id"],
                    stream_seq=event.stream_seq,
                    origin_seq=origin_seq,
                    dedupe_key=evidence_key,
                    data=event_data,
                    created_at=now,
                )
                db.add(row)
                await db.flush()
                _call_fault(_fault_injector, "after_insert")
                _call_fault(_fault_injector, f"after_insert:{item_index}")
                new_by_key[evidence_key] = (row, event_data)

            # Projection happens only after every row has passed validation and
            # insertion. It is still in the same transaction as those rows.
            for item_index, (evidence_key, descriptor) in enumerate(
                unique_descriptors.items()
            ):
                created_row = new_by_key.get(evidence_key)
                if created_row is None:
                    continue
                row, event_data = created_row
                _call_fault(_fault_injector, "before_projection")
                _call_fault(_fault_injector, f"before_projection:{item_index}")
                _apply_reveal(
                    state,
                    event_data=event_data,
                    origin_seq=row.origin_seq,
                    now=now,
                    max_reveals=max_reveals,
                )

            if new_by_key:
                session_row.tool_exposure_state = state
                _call_fault(_fault_injector, "before_commit")

            final_state = deepcopy(state)
            seen_keys: set[str] = set()
            results: list[RevealCommitResult] = []
            for descriptor in descriptors:
                evidence_key = descriptor["evidence_key"]
                new_row = new_by_key.get(evidence_key)
                existing = existing_by_key.get(evidence_key)
                row = new_row[0] if new_row is not None else existing
                assert row is not None
                results.append(RevealCommitResult(
                    created=new_row is not None and evidence_key not in seen_keys,
                    origin_seq=row.origin_seq,
                    state=deepcopy(final_state),
                ))
                seen_keys.add(evidence_key)
            committed = tuple(results)
    assert committed is not None
    return committed


async def commit_tool_reveal(
    event: ToolRevealEvent,
    *,
    ttl_seconds: int = DEFAULT_REVEAL_TTL_SECONDS,
    max_reveals: int = DEFAULT_MAX_REVEALS_PER_AGENT,
    _fault_injector: FaultInjector | None = None,
) -> RevealCommitResult:
    """Backward-compatible single-event wrapper around the atomic batch API."""

    committed = await commit_tool_reveals(
        (event,),
        ttl_seconds=ttl_seconds,
        max_reveals=max_reveals,
        _fault_injector=_fault_injector,
    )
    return committed[0]


async def _rebuild_projection_locked(
    db: AsyncSession,
    session_row: Session,
    *,
    now: datetime | None = None,
    max_reveals: int = DEFAULT_MAX_REVEALS_PER_AGENT,
) -> dict:
    """Rebuild JSON from surviving typed events; caller holds session lock."""
    now = now or datetime.now(timezone.utc)
    previous = _normalize_state(session_row.tool_exposure_state)
    rows = (
        await db.execute(
            select(InternalPart)
            .where(InternalPart.session_id == session_row.id)
            .order_by(InternalPart.origin_seq)
        )
    ).scalars().all()
    next_seq = max(
        previous["next_origin_seq"],
        max((row.origin_seq for row in rows), default=0) + 1,
    )
    rebuilt = _empty_state(
        next_origin_seq=next_seq,
        provider_fallback=previous.get("provider_fallback"),
    )
    for row in rows:
        if row.kind != TOOL_REVEAL_KIND or not isinstance(row.data, dict):
            continue
        data = row.data
        required = {
            "agent_id",
            "canonical_tool_id",
            "schema_digest",
            "catalog_generation",
            "evidence_source",
            "origin_message_id",
            "origin_part_id",
            "occurred_at",
            "expires_at",
        }
        if not required <= data.keys():
            continue
        if data.get("evidence_source") not in ("portable", "native"):
            continue
        expires_at = _parse_time(data.get("expires_at"))
        if expires_at is None or expires_at <= now:
            continue
        try:
            if not _SHA256_RE.fullmatch(str(data["schema_digest"]).lower()):
                continue
            if not _SAFE_ID_RE.fullmatch(str(data["canonical_tool_id"])):
                continue
            _apply_reveal(
                rebuilt,
                event_data=data,
                origin_seq=row.origin_seq,
                now=now,
                max_reveals=max_reveals,
            )
        except (KeyError, TypeError, ValueError):
            continue
    session_row.tool_exposure_state = rebuilt
    return rebuilt


async def rebuild_tool_exposure_state(
    session_id: str,
    user_id: str,
    *,
    now: datetime | None = None,
    max_reveals: int = DEFAULT_MAX_REVEALS_PER_AGENT,
) -> dict:
    """Repair a session projection from its API-hidden event ledger."""
    rebuilt: dict | None = None
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            session_row = await lock_owned_session(db, session_id, user_id)
            before = _normalize_state(session_row.tool_exposure_state)
            rebuilt = await _rebuild_projection_locked(
                db,
                session_row,
                now=now,
                max_reveals=max_reveals,
            )
            if rebuilt != before:
                log.warning(
                    "Repaired tool exposure projection session=%s agents_before=%d agents_after=%d",
                    session_id,
                    len(before.get("agents", {})),
                    len(rebuilt.get("agents", {})),
                )
    assert rebuilt is not None
    return deepcopy(rebuilt)


async def get_valid_revealed_ids(
    *,
    session_id: str,
    user_id: str,
    agent_id: str,
    catalog_generation: str,
    schema_digests: Mapping[str, str],
    catalogue_availability: Literal[
        "available", "stale", "unavailable"
    ] = "available",
    now: datetime | None = None,
) -> frozenset[str]:
    """Validate and prune semantic reveals against the current eligible set.

    ``schema_digests`` is intentionally the already permission-filtered
    catalogue.  Removing invalid ledger rows prevents an old reveal from
    resurrecting if a schema or permission later happens to revert. A cold
    ``unavailable`` sandbox projection is different from an authoritative
    empty catalogue: it returns an empty execution frontier for this step but
    leaves durable reveal evidence untouched so a later reconnect can restore
    it. A ``stale`` last-known-good projection remains safe to validate by its
    original generation and schema digests.
    """
    if catalogue_availability not in {"available", "stale", "unavailable"}:
        raise ValueError("invalid catalogue availability")
    current = now or datetime.now(timezone.utc)
    normalized_digests = {
        tool_id: digest.lower()
        for tool_id, digest in schema_digests.items()
        if isinstance(tool_id, str)
        and isinstance(digest, str)
        and _SHA256_RE.fullmatch(digest.lower())
    }
    rebuilt: dict[str, Any] | None = None
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            session_row = await lock_owned_session(db, session_id, user_id)
            if catalogue_availability == "unavailable":
                # Ownership is still checked, but no ledger/projection write is
                # allowed while the remote catalogue is unknown.
                return frozenset()
            if catalogue_availability == "stale":
                # A last-known-good snapshot may safely restore only the
                # intersection already present in the durable projection. It
                # must not delete a newer generation written by another
                # worker, nor repair/rebuild state from older ledger rows.
                state = _normalize_state(session_row.tool_exposure_state)
                agent_state = state.get("agents", {}).get(agent_id)
                if (
                    not isinstance(agent_state, dict)
                    or agent_state.get("catalog_generation")
                    != catalog_generation
                ):
                    return frozenset()
                revealed = agent_state.get("revealed")
                if not isinstance(revealed, dict):
                    return frozenset()
                valid = set()
                for tool_id, record in revealed.items():
                    expected_digest = normalized_digests.get(tool_id)
                    expires = (
                        _parse_time(record.get("expires_at"))
                        if isinstance(record, dict)
                        else None
                    )
                    if (
                        expected_digest is not None
                        and expires is not None
                        and expires > current
                        and record.get("schema_digest") == expected_digest
                    ):
                        valid.add(tool_id)
                return frozenset(valid)
            # An available response can still have been fetched before a
            # concurrent worker committed a newer generation. Generation
            # hashes are intentionally unordered, so a mismatch has no safe
            # destructive interpretation: fail closed and leave both the
            # ledger and its current projection untouched. A later explicit
            # capability reveal owns any generation transition.
            state = _normalize_state(session_row.tool_exposure_state)
            agent_state = state.get("agents", {}).get(agent_id)
            if (
                not isinstance(agent_state, dict)
                or agent_state.get("catalog_generation") != catalog_generation
            ):
                return frozenset()
            reveal_rows = (
                await db.execute(
                    select(InternalPart).where(
                        InternalPart.session_id == session_id,
                        InternalPart.kind == TOOL_REVEAL_KIND,
                    )
                )
            ).scalars().all()
            invalid_ids: list[str] = []
            for row in reveal_rows:
                data = row.data if isinstance(row.data, dict) else {}
                if data.get("agent_id") != agent_id:
                    continue
                tool_id = data.get("canonical_tool_id")
                expires = _parse_time(data.get("expires_at"))
                expected_digest = normalized_digests.get(tool_id)
                if (
                    data.get("catalog_generation") != catalog_generation
                    or expires is None
                    or expires <= current
                    or expected_digest is None
                    or data.get("schema_digest") != expected_digest
                ):
                    invalid_ids.append(row.id)
            if invalid_ids:
                await db.execute(
                    delete(InternalPart).where(InternalPart.id.in_(sorted(invalid_ids)))
                )
            rebuilt = await _rebuild_projection_locked(db, session_row, now=current)
    assert rebuilt is not None
    agent_state = rebuilt.get("agents", {}).get(agent_id)
    if not isinstance(agent_state, dict) or agent_state.get("catalog_generation") != catalog_generation:
        return frozenset()
    revealed = agent_state.get("revealed")
    return frozenset(revealed) if isinstance(revealed, dict) else frozenset()


async def get_provider_replay_parts(
    *,
    session_id: str,
    user_id: str,
    binding: ProviderCapabilityBinding,
    capability_key_digest: str | None = None,
    response_chain_id: str,
) -> list[InternalPartRecord]:
    """Read opaque blocks only for an exact owner + full capability binding."""
    if not response_chain_id or len(response_chain_id) > 128:
        return []
    digest = str(capability_key_digest or binding.digest()).lower()
    if not _SHA256_RE.fullmatch(digest):
        return []
    async with get_db_session() as db:
        owned = (
            await db.execute(
                select(Session.id).where(
                    Session.id == session_id,
                    Session.user_id == user_id,
                    Session.is_deleted == False,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if owned is None:
            return []
        rows = (
            await db.execute(
                select(InternalPart)
                .join(Message, Message.id == InternalPart.message_id)
                .where(
                    InternalPart.session_id == session_id,
                    InternalPart.user_id == user_id,
                    InternalPart.kind != TOOL_REVEAL_KIND,
                    InternalPart.capability_key_digest == digest,
                    InternalPart.response_chain_id == response_chain_id,
                    Message.session_id == session_id,
                    Message.user_id == user_id,
                )
                .order_by(Message.created_at, InternalPart.stream_seq, InternalPart.origin_seq)
            )
        ).scalars().all()
        return [_record(row) for row in rows]


async def get_provider_replay_parts_for_binding(
    *,
    session_id: str,
    user_id: str,
    binding: ProviderCapabilityBinding,
    capability_key_digest: str | None = None,
) -> list[InternalPartRecord]:
    """Read every opaque provider block for one exact binding, grouped by caller.

    Unlike ``get_provider_replay_parts`` this private LLM-only query spans
    response chains so a resumed session can reconstruct each completed Tool
    Search exchange.  Ownership and the full provider capability digest remain
    mandatory; no REST/session loader imports this helper.
    """

    digest = str(capability_key_digest or binding.digest()).lower()
    if not _SHA256_RE.fullmatch(digest):
        return []
    async with get_db_session() as db:
        owned = (
            await db.execute(
                select(Session.id).where(
                    Session.id == session_id,
                    Session.user_id == user_id,
                    Session.is_deleted == False,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if owned is None:
            return []
        rows = (
            await db.execute(
                select(InternalPart)
                .join(Message, Message.id == InternalPart.message_id)
                .where(
                    InternalPart.session_id == session_id,
                    InternalPart.user_id == user_id,
                    InternalPart.kind == PROVIDER_TRANSCRIPT_KIND,
                    InternalPart.capability_key_digest == digest,
                    Message.session_id == session_id,
                    Message.user_id == user_id,
                )
                .order_by(
                    Message.created_at,
                    InternalPart.stream_seq,
                    InternalPart.origin_seq,
                )
            )
        ).scalars().all()
        return [_record(row) for row in rows]


async def delete_internal_parts_for_messages_locked(
    db: AsyncSession,
    session_row: Session,
    message_ids: list[str],
) -> dict:
    """Delete branch-owned private rows and rebuild; caller holds session row."""
    if message_ids:
        await db.execute(
            delete(InternalPart).where(
                InternalPart.session_id == session_row.id,
                InternalPart.message_id.in_(sorted(message_ids)),
            )
        )
    return await _rebuild_projection_locked(db, session_row)


async def clear_internal_session_locked(db: AsyncSession, session_row: Session) -> None:
    """Clear private data during a user-requested session deletion."""
    await db.execute(delete(InternalPart).where(InternalPart.session_id == session_row.id))
    previous = _normalize_state(session_row.tool_exposure_state)
    session_row.tool_exposure_state = _empty_state(
        next_origin_seq=previous["next_origin_seq"]
    )
