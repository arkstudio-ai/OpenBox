"""Durable inbox/activation/outbox protocol for continuable Task children.

Acceptance is the only boundary at which new child work comes into existence.
It commits the exact child trigger, lineage descriptor, parent ToolPart pointer,
activation inbox row, and waiting outbox in one fenced transaction. Dispatch is
separately claimed with a short database lease so foreground execution and the
periodic recovery worker cannot both wake the same activation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Iterable
import time
import uuid
import weakref

from sqlalchemy import String, cast, func, or_, select, update

from bus import bus
from bus.events import PART_UPDATED
from core.identifier import ascending
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.message import Message
from db.models.part import Part, public_part_data
from db.models.project import Project
from db.models.session import Session
from db.models.subagent import (
    SubagentActivation,
    SubagentDescriptor,
    SubagentOutbox,
)


SUBAGENT_CLAIM_SECONDS = 30.0
MAX_SUBAGENT_DEPTH = 3
MAX_PROMPT_CHARS = 65_536
MAX_LIST_RESULTS = 50

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_UNKNOWN = "outcome_unknown"
OUTCOME_ERROR = "error"
TERMINAL_OUTCOMES = frozenset({
    OUTCOME_SUCCEEDED,
    OUTCOME_INTERRUPTED,
    OUTCOME_UNKNOWN,
    OUTCOME_ERROR,
})
_activation_claim_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


class SubagentFenceError(RuntimeError):
    """Tenant, lineage, project, or generation identity did not match."""


class SubagentBusyError(RuntimeError):
    """A continuable child already owns an unfinished activation."""


@dataclass(frozen=True, slots=True)
class ActivationRef:
    id: str
    descriptor_id: str
    user_id: str
    project_id: str
    parent_session_id: str
    parent_part_id: str
    child_session_id: str
    child_trigger_message_id: str
    descriptor_generation: int
    created: bool = False


@dataclass(frozen=True, slots=True)
class ActivationClaim:
    activation_id: str
    descriptor_id: str
    descriptor_generation: int
    user_id: str
    child_session_id: str
    child_trigger_message_id: str
    token: str
    owner: str
    local_deadline: float


@dataclass(frozen=True, slots=True)
class ApplyResult:
    rejoined: int
    updates: tuple[dict, ...]
    message_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoveredActivationBinding:
    state: str  # absent | bound | terminal
    activation_id: str | None


def _database_now(db):
    if db.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return func.current_timestamp()


def _message_error_is_empty():
    """Match both SQL NULL and JSON null across SQLite/PostgreSQL.

    PostgreSQL JSONB's default ``none_as_null=False`` binds Python ``None`` as
    JSON null, while historical/imported rows may contain SQL NULL. Casting
    JSON null to text yields the unquoted token ``null`` on both databases.
    """
    return or_(
        Message.error.is_(None),
        cast(Message.error, String) == "null",
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _ref(row: SubagentActivation, *, created: bool = False) -> ActivationRef:
    return ActivationRef(
        id=row.id,
        descriptor_id=row.descriptor_id,
        user_id=row.user_id,
        project_id=row.project_id,
        parent_session_id=row.parent_session_id,
        parent_part_id=row.parent_part_id,
        child_session_id=row.child_session_id,
        child_trigger_message_id=row.child_trigger_message_id,
        descriptor_generation=row.descriptor_generation,
        created=created,
    )


def _is_task_part(part: Part) -> bool:
    data = dict(part.data or {})
    if part.canonical_tool_id is not None:
        return part.canonical_tool_id == "task"
    return str(data.get("tool") or "").lower() == "task"


def _publish_part(user_id: str, data: dict) -> None:
    public = public_part_data(data)
    event_part = {
        key: value
        for key, value in public.items()
        if key not in {"session_id", "message_id", "state"}
    }
    bus.publish(PART_UPDATED, {
        "userId": user_id,
        "sessionId": public.get("session_id", ""),
        "messageId": public.get("message_id", ""),
        "part": event_part,
    })


async def _locked_parent_tool(
    db,
    *,
    user_id: str,
    parent_session_id: str,
    parent_message_id: str,
    parent_part_id: str,
    parent_run_id: str,
    parent_generation: int,
) -> tuple[Session, Message, Part]:
    """Lock and fence the exact parent tool invocation."""
    from session.internal_parts import begin_session_write

    await begin_session_write(db)
    parent = (
        await db.execute(
            select(Session).where(
                Session.id == parent_session_id,
                Session.user_id == user_id,
                Session.is_deleted.is_(False),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if parent is None:
        raise SubagentFenceError("parent Session is no longer live")
    live_parent = (
        await db.execute(
            select(AgentDriverState).where(
                AgentDriverState.session_id == parent_session_id,
                AgentDriverState.user_id == user_id,
                AgentDriverState.run_id == parent_run_id,
                AgentDriverState.generation == parent_generation,
                AgentDriverState.phase != "idle",
                AgentDriverState.lease_expires_at.is_not(None),
                AgentDriverState.lease_expires_at > _database_now(db),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if live_parent is None:
        raise SubagentFenceError("parent Task generation is no longer live")
    project = (
        await db.execute(
            select(Project.id).where(
                Project.id == parent.project_id,
                Project.user_id == user_id,
                Project.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise SubagentFenceError("parent project is missing or no longer available")
    message = (
        await db.execute(
            select(Message).where(
                Message.id == parent_message_id,
                Message.session_id == parent_session_id,
                Message.user_id == user_id,
                Message.role == "assistant",
            )
        )
    ).scalar_one_or_none()
    part = (
        await db.execute(
            select(Part).where(
                Part.id == parent_part_id,
                Part.message_id == parent_message_id,
                Part.session_id == parent_session_id,
                Part.user_id == user_id,
                Part.type == "tool",
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if message is None or part is None or not _is_task_part(part):
        raise SubagentFenceError("Task parent transcript identity mismatch")
    status = getattr(
        dict(part.data or {}).get("status"), "value", dict(part.data or {}).get("status")
    )
    if status not in {"pending", "running"}:
        raise SubagentFenceError("parent Task part is already terminal")
    return parent, message, part


async def _parent_lineage(db, parent: Session) -> tuple[str, str | None, int]:
    parent_descriptor = (
        await db.execute(
            select(SubagentDescriptor).where(
                SubagentDescriptor.child_session_id == parent.id,
                SubagentDescriptor.user_id == parent.user_id,
                SubagentDescriptor.project_id == parent.project_id,
            )
        )
    ).scalar_one_or_none()
    if parent_descriptor is None:
        return parent.id, None, 1
    depth = parent_descriptor.depth + 1
    if depth > MAX_SUBAGENT_DEPTH:
        raise SubagentFenceError(
            f"subagent depth exceeds configured maximum {MAX_SUBAGENT_DEPTH}"
        )
    return parent_descriptor.root_session_id, parent_descriptor.id, depth


def _activation_metadata(
    *, descriptor_id: str, activation_id: str, child_session_id: str,
    subagent_type: str, generation: int,
) -> dict[str, Any]:
    return {
        "subagent_id": descriptor_id,
        "subagent_activation_id": activation_id,
        "child_session_id": child_session_id,
        "subagent_type": subagent_type,
        "subagent_generation": generation,
        # Existing Processor allowlist persists these compatibility keys. They
        # preserve the exact activation pointer on the live-result path without
        # changing Processor during this kernel slice.
        "task_handoff_id": activation_id,
    }


async def accept_spawn(
    *,
    user_id: str,
    parent_session_id: str,
    parent_message_id: str,
    parent_part_id: str,
    parent_run_id: str,
    parent_generation: int,
    task_title: str,
    prompt: str,
    subagent_type: str,
    child_model: str,
    lifecycle: str,
    authority_snapshot: dict,
    fork_seed: Any | None = None,
) -> ActivationRef:
    """Atomically accept a new child, first trigger, descriptor, and outbox."""
    if lifecycle not in {"one_shot", "continuable"}:
        raise ValueError("invalid subagent lifecycle")
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"subagent prompt must be 1..{MAX_PROMPT_CHARS} characters")
    if not parent_run_id or parent_generation <= 0:
        raise SubagentFenceError("Task acceptance requires a parent run fence")
    from agent.subagent_authority import parse_subagent_authority

    authority = parse_subagent_authority(authority_snapshot)
    canonical_authority = authority.to_json()
    composition = authority.composition
    if composition is not None:
        from agent.subagent_composition import validate_composition_availability
        from core.config import get_config

        validate_composition_availability(composition, get_config())
        if composition.agent_preset.name != subagent_type:
            raise SubagentFenceError("Task preset does not match its composition snapshot")
        if composition.model != child_model:
            raise SubagentFenceError("Task model does not match its composition snapshot")
        if (fork_seed is not None) != (composition.seed_mode == "fork"):
            raise SubagentFenceError("Task fork seed does not match its composition snapshot")
    elif fork_seed is not None:
        raise SubagentFenceError("legacy Task authority cannot accept a fork seed")

    published_part: dict | None = None
    published_session = None
    published_message = None
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        parent, parent_message, part = await _locked_parent_tool(
            db,
            user_id=user_id,
            parent_session_id=parent_session_id,
            parent_message_id=parent_message_id,
            parent_part_id=parent_part_id,
            parent_run_id=parent_run_id,
            parent_generation=parent_generation,
        )
        existing = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.parent_part_id == parent_part_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.user_id != user_id
                or existing.parent_session_id != parent_session_id
                or existing.parent_message_id != parent_message_id
                or existing.parent_run_id != parent_run_id
                or existing.parent_generation != parent_generation
                or existing.kind != "spawn"
            ):
                raise SubagentFenceError("Task acceptance idempotency collision")
            existing_descriptor = (
                await db.execute(
                    select(SubagentDescriptor).where(
                        SubagentDescriptor.id == existing.descriptor_id
                    )
                )
            ).scalar_one_or_none()
            if (
                existing_descriptor is None
                or parse_subagent_authority(
                    existing_descriptor.authority_snapshot
                ).to_json() != canonical_authority
            ):
                raise SubagentFenceError("Task authority idempotency collision")
            return _ref(existing)

        root_session_id, parent_descriptor_id, depth = await _parent_lineage(db, parent)
        from session.session import _insert_user_message_locked, _new_session_record

        child_row, published_session = _new_session_record(
            model=child_model,
            agent=subagent_type,
            title=f"{task_title[:255]} (@{subagent_type} subagent)",
            parent_id=parent_session_id,
            user_id=user_id,
            project_id=parent.project_id,
            kind="normal",
            now=now,
        )
        db.add(child_row)
        await db.flush()
        trigger_now = now
        if fork_seed is not None:
            if getattr(fork_seed, "session_id", None) != parent_session_id:
                raise SubagentFenceError("Task fork seed targets another parent")
            from session.fork import clone_stable_event_prefix_locked

            cloned = await clone_stable_event_prefix_locked(
                db,
                source_row=parent,
                destination_row=child_row,
                frozen=fork_seed,
                now=now,
            )
            trigger_now = cloned["next_created_at"]
        published_message = await _insert_user_message_locked(
            db,
            session_id=child_row.id,
            text=prompt,
            agent=subagent_type,
            model=(composition.model if composition is not None else None),
            synthetic=False,
            variant=(composition.reasoning if composition is not None else None),
            client_message_id=None,
            output_format=(
                composition.output_schema if composition is not None else None
            ),
            user_id=user_id,
            run_fence=None,
            session_row=child_row,
            now=trigger_now,
        )
        descriptor_id = ascending("subagent")
        activation_id = ascending("activation")
        descriptor = SubagentDescriptor(
            id=descriptor_id,
            user_id=user_id,
            project_id=parent.project_id,
            parent_session_id=parent_session_id,
            child_session_id=child_row.id,
            root_session_id=root_session_id,
            parent_descriptor_id=parent_descriptor_id,
            depth=depth,
            subagent_type=subagent_type,
            lifecycle=lifecycle,
            authority_snapshot=canonical_authority,
            state="active",
            generation=1,
            active_activation_id=activation_id,
            interrupt_requested_generation=None,
            interrupt_applied_generation=None,
            created_at=now,
            updated_at=now,
            settled_at=None,
        )
        activation = SubagentActivation(
            id=activation_id,
            descriptor_id=descriptor_id,
            user_id=user_id,
            project_id=parent.project_id,
            parent_session_id=parent_session_id,
            parent_message_id=parent_message_id,
            parent_part_id=parent_part_id,
            parent_run_id=parent_run_id,
            parent_generation=parent_generation,
            descriptor_generation=1,
            kind="spawn",
            child_session_id=child_row.id,
            child_trigger_message_id=published_message.id,
            child_run_id=None,
            child_generation=None,
            state="accepted",
            claim_token=None,
            claim_owner=None,
            claim_expires_at=None,
            task_title=task_title[:255],
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        outbox = SubagentOutbox(
            activation_id=activation_id,
            descriptor_id=descriptor_id,
            user_id=user_id,
            project_id=parent.project_id,
            parent_session_id=parent_session_id,
            parent_message_id=parent_message_id,
            parent_part_id=parent_part_id,
            state="waiting",
            outcome="waiting",
            result_payload=None,
            created_at=now,
            updated_at=now,
            ready_at=None,
            delivered_at=None,
        )
        db.add_all((descriptor, activation, outbox))

        part_data = dict(part.data or {})
        metadata = dict(part_data.get("metadata") or {})
        metadata.update(_activation_metadata(
            descriptor_id=descriptor_id,
            activation_id=activation_id,
            child_session_id=child_row.id,
            subagent_type=subagent_type,
            generation=1,
        ))
        part_data["metadata"] = metadata
        part.data = public_part_data(part_data)
        published_part = dict(part.data)
        from session.agent_event_log import append_part_event_locked, ensure_surface_seed_locked

        await ensure_surface_seed_locked(db, parent)
        await append_part_event_locked(
            db,
            parent,
            part,
            parent_message,
            operation="updated",
            run_fence=(parent_session_id, parent_run_id, parent_generation),
        )
        await db.flush()
        ref = _ref(activation, created=True)

    if published_session is not None:
        from session.session import _publish_session_created, _publish_user_message

        _publish_session_created(published_session)
        _publish_user_message(published_message, user_id=user_id, run_fence=None)
    if published_part is not None:
        _publish_part(user_id, published_part)
    return ref


async def accept_follow_up(
    *,
    descriptor_id: str,
    user_id: str,
    parent_session_id: str,
    parent_message_id: str,
    parent_part_id: str,
    parent_run_id: str,
    parent_generation: int,
    task_title: str,
    prompt: str,
    authority_snapshot: dict,
    requested_model: str | None = None,
    reasoning: str | None = None,
    persona: str | None = None,
    requested_tools: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> ActivationRef:
    """Atomically append one exact follow-up trigger and its delivery rows."""
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"subagent prompt must be 1..{MAX_PROMPT_CHARS} characters")
    from agent.subagent_authority import parse_subagent_authority

    delegator_authority = parse_subagent_authority(authority_snapshot)
    published_part: dict | None = None
    published_message = None
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        parent, parent_message, part = await _locked_parent_tool(
            db,
            user_id=user_id,
            parent_session_id=parent_session_id,
            parent_message_id=parent_message_id,
            parent_part_id=parent_part_id,
            parent_run_id=parent_run_id,
            parent_generation=parent_generation,
        )
        existing = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.parent_part_id == parent_part_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.descriptor_id != descriptor_id
                or existing.user_id != user_id
                or existing.parent_session_id != parent_session_id
                or existing.parent_run_id != parent_run_id
                or existing.parent_generation != parent_generation
                or existing.kind != "follow_up"
            ):
                raise SubagentFenceError("follow-up idempotency collision")
            return _ref(existing)

        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == descriptor_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if descriptor is None:
            raise LookupError("subagent not found")
        if (
            descriptor.user_id != user_id
            or descriptor.project_id != parent.project_id
            or descriptor.parent_session_id != parent_session_id
        ):
            raise SubagentFenceError("subagent direct lineage or project mismatch")
        if descriptor.lifecycle != "continuable" or descriptor.state != "active":
            raise SubagentFenceError("subagent is not continuable and active")
        # A follow-up must reuse the original monotonic boundary.  Do not
        # reconstruct it from a possibly changed parent/config, and do not
        # accept legacy/corrupt rows with unknown semantics.
        from agent.subagent_authority import (
            AUTHORITY_SNAPSHOT_VERSION,
            SubagentAuthority,
            intersect_subagent_authorities,
        )

        existing_authority = parse_subagent_authority(
            descriptor.authority_snapshot
        )
        narrowed_authority = intersect_subagent_authorities(
            existing_authority,
            delegator_authority,
        )
        if existing_authority.composition is None:
            if any(value is not None for value in (
                requested_model,
                reasoning,
                persona,
                requested_tools,
                output_schema,
            )):
                raise SubagentFenceError(
                    "legacy subagent cannot acquire composition capabilities on follow-up"
                )
            composition = None
        else:
            from agent.subagent_composition import (
                narrow_follow_up_composition,
                validate_composition_availability,
            )
            from core.config import get_config

            validate_composition_availability(
                existing_authority.composition,
                get_config(),
            )
            composition = narrow_follow_up_composition(
                existing_authority.composition,
                delegator_tool_ids=narrowed_authority.tool_ids,
                requested_model=requested_model,
                reasoning=reasoning,
                persona=persona,
                requested_tools=requested_tools,
                output_schema=output_schema,
            )
            narrowed_authority = SubagentAuthority(
                tool_ids=composition.tool_allowlist,
                permission_planes=narrowed_authority.permission_planes,
                guard_planes=narrowed_authority.guard_planes,
                composition=composition,
                snapshot_version=AUTHORITY_SNAPSHOT_VERSION,
            )
        if descriptor.active_activation_id is not None:
            raise SubagentBusyError("subagent already has an active activation")
        child = (
            await db.execute(
                select(Session).where(
                    Session.id == descriptor.child_session_id,
                    Session.user_id == user_id,
                    Session.project_id == parent.project_id,
                    Session.parent_id == parent_session_id,
                    Session.is_deleted.is_(False),
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if child is None:
            raise SubagentBusyError("subagent child is not idle")
        if child.status == "error":
            # A conservative outcome_unknown/error is already visible in the
            # exact outbox. Permit the next activation to acknowledge that
            # terminal turn and restore the reusable child, but only when no
            # newer User trigger or live Driver can be hidden by the reset.
            previous = (
                await db.execute(
                    select(SubagentActivation, SubagentOutbox)
                    .join(
                        SubagentOutbox,
                        SubagentOutbox.activation_id == SubagentActivation.id,
                    )
                    .where(
                        SubagentActivation.descriptor_id == descriptor.id,
                        SubagentActivation.descriptor_generation
                        == descriptor.generation,
                        SubagentActivation.child_session_id == child.id,
                        SubagentActivation.state == "completed",
                        SubagentOutbox.state.in_(("ready", "delivered")),
                        SubagentOutbox.outcome.in_(tuple(TERMINAL_OUTCOMES)),
                    )
                )
            ).one_or_none()
            driver = (
                await db.execute(
                    select(AgentDriverState).where(
                        AgentDriverState.session_id == child.id,
                        AgentDriverState.user_id == user_id,
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            latest_user_id = (
                await db.execute(
                    select(Message.id).where(
                        Message.session_id == child.id,
                        Message.user_id == user_id,
                        Message.role == "user",
                    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            recoverable = bool(
                previous is not None
                and driver is not None
                and driver.phase == "idle"
                and driver.run_id is None
                and driver.owner_id is None
                and driver.lease_expires_at is None
                and latest_user_id == previous[0].child_trigger_message_id
            )
            if not recoverable:
                raise SubagentBusyError("subagent child error is not settled")
            child.status = "idle"
            child.updated_at = now
        elif child.status != "idle":
            raise SubagentBusyError("subagent child is not idle")

        activation_id = ascending("activation")
        generation = descriptor.generation + 1
        from session.session import _insert_user_message_locked

        published_message = await _insert_user_message_locked(
            db,
            session_id=child.id,
            text=prompt,
            agent=descriptor.subagent_type,
            model=(composition.model if composition is not None else None),
            synthetic=False,
            variant=(composition.reasoning if composition is not None else None),
            client_message_id=None,
            output_format=(
                composition.output_schema if composition is not None else None
            ),
            user_id=user_id,
            run_fence=None,
            session_row=child,
            now=now,
        )
        activation = SubagentActivation(
            id=activation_id,
            descriptor_id=descriptor.id,
            user_id=user_id,
            project_id=descriptor.project_id,
            parent_session_id=parent_session_id,
            parent_message_id=parent_message_id,
            parent_part_id=parent_part_id,
            parent_run_id=parent_run_id,
            parent_generation=parent_generation,
            descriptor_generation=generation,
            kind="follow_up",
            child_session_id=child.id,
            child_trigger_message_id=published_message.id,
            child_run_id=None,
            child_generation=None,
            state="accepted",
            claim_token=None,
            claim_owner=None,
            claim_expires_at=None,
            task_title=(task_title or f"Follow up {descriptor.id}")[:255],
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        db.add(activation)
        db.add(SubagentOutbox(
            activation_id=activation_id,
            descriptor_id=descriptor.id,
            user_id=user_id,
            project_id=descriptor.project_id,
            parent_session_id=parent_session_id,
            parent_message_id=parent_message_id,
            parent_part_id=parent_part_id,
            state="waiting",
            outcome="waiting",
            result_payload=None,
            created_at=now,
            updated_at=now,
            ready_at=None,
            delivered_at=None,
        ))
        descriptor.generation = generation
        descriptor.authority_snapshot = narrowed_authority.to_json()
        descriptor.active_activation_id = activation_id
        descriptor.updated_at = now
        descriptor.interrupt_requested_generation = None
        descriptor.interrupt_applied_generation = None

        part_data = dict(part.data or {})
        metadata = dict(part_data.get("metadata") or {})
        metadata.update(_activation_metadata(
            descriptor_id=descriptor.id,
            activation_id=activation_id,
            child_session_id=child.id,
            subagent_type=descriptor.subagent_type,
            generation=generation,
        ))
        part_data["metadata"] = metadata
        part.data = public_part_data(part_data)
        published_part = dict(part.data)
        from session.agent_event_log import append_part_event_locked, ensure_surface_seed_locked

        await ensure_surface_seed_locked(db, parent)
        await append_part_event_locked(
            db,
            parent,
            part,
            parent_message,
            operation="updated",
            run_fence=(parent_session_id, parent_run_id, parent_generation),
        )
        await db.flush()
        ref = _ref(activation, created=True)

    from session.session import _publish_user_message

    _publish_user_message(published_message, user_id=user_id, run_fence=None)
    if published_part is not None:
        _publish_part(user_id, published_part)
    return ref


async def claim_activation(
    activation_id: str,
    *,
    user_id: str | None = None,
    owner: str | None = None,
) -> ActivationClaim | None:
    lock = _activation_claim_locks.get(activation_id)
    if lock is None:
        lock = asyncio.Lock()
        _activation_claim_locks[activation_id] = lock
    async with lock:
        return await _claim_activation_locked(
            activation_id,
            user_id=user_id,
            owner=owner,
        )


async def _claim_activation_locked(
    activation_id: str,
    *,
    user_id: str | None,
    owner: str | None,
) -> ActivationClaim | None:
    """Exact-CAS claim one accepted/expired-claimed activation.

    The descriptor interrupt fence is checked while both rows are locked. A
    committed interrupt can therefore never be followed by a scanner reserve.
    """
    from agent.driver import WORKER_ID

    actual_owner = owner or WORKER_ID
    token = uuid.uuid4().hex
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.id == activation_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if activation is None or (user_id and activation.user_id != user_id):
            return None
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == activation.descriptor_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if descriptor is None:
            return None
        clock = await db.execute(select(_database_now(db)))
        now = _aware(clock.scalar_one())
        clock.close()
        assert now is not None
        claim_expiry = _aware(activation.claim_expires_at)
        claimable = activation.state == "accepted" or (
            activation.state == "claimed"
            and claim_expiry is not None
            and claim_expiry <= now
        )
        if not claimable:
            return None
        if (
            descriptor.user_id != activation.user_id
            or descriptor.project_id != activation.project_id
            or descriptor.child_session_id != activation.child_session_id
            or descriptor.parent_session_id != activation.parent_session_id
            or descriptor.active_activation_id != activation.id
            or descriptor.generation != activation.descriptor_generation
            or descriptor.state != "active"
        ):
            raise SubagentFenceError("activation descriptor identity mismatch")
        if descriptor.interrupt_requested_generation == activation.descriptor_generation:
            await _complete_locked(
                db,
                activation=activation,
                descriptor=descriptor,
                outcome=OUTCOME_INTERRUPTED,
                raw_result=_error_result(
                    activation,
                    descriptor,
                    outcome=OUTCOME_INTERRUPTED,
                    message="Subagent activation was interrupted before it started.",
                    recovery_code="subagent_interrupted_before_start",
                ),
                now=now,
            )
            descriptor.interrupt_applied_generation = activation.descriptor_generation
            return None
        result = await db.execute(
            update(SubagentActivation)
            .where(
                SubagentActivation.id == activation.id,
                or_(
                    SubagentActivation.state == "accepted",
                    (
                        (SubagentActivation.state == "claimed")
                        & SubagentActivation.claim_expires_at.is_not(None)
                        & (SubagentActivation.claim_expires_at <= _database_now(db))
                    ),
                ),
            )
            .values(
                state="claimed",
                claim_token=token,
                claim_owner=actual_owner,
                claim_expires_at=now + timedelta(seconds=SUBAGENT_CLAIM_SECONDS),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        matched = bool(result.rowcount)
        result.close()
        if not matched:
            return None
        return ActivationClaim(
            activation_id=activation.id,
            descriptor_id=activation.descriptor_id,
            descriptor_generation=activation.descriptor_generation,
            user_id=activation.user_id,
            child_session_id=activation.child_session_id,
            child_trigger_message_id=activation.child_trigger_message_id,
            token=token,
            owner=actual_owner,
            local_deadline=time.monotonic() + SUBAGENT_CLAIM_SECONDS,
        )


async def bind_claimed_activation(claim: ActivationClaim, lease: Any) -> bool:
    """Bind a claim to the exact child Driver generation before wake."""
    if time.monotonic() >= claim.local_deadline:
        return False
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.id == claim.activation_id,
                    SubagentActivation.user_id == claim.user_id,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if activation is None or activation.state != "claimed":
            return False
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == activation.descriptor_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if descriptor is None:
            return False
        clock = await db.execute(select(_database_now(db)))
        now = _aware(clock.scalar_one())
        clock.close()
        assert now is not None
        claim_expiry = _aware(activation.claim_expires_at)
        exact_claim = (
            activation.claim_token == claim.token
            and activation.claim_owner == claim.owner
            and activation.descriptor_generation == claim.descriptor_generation
            and activation.child_session_id == claim.child_session_id
            and activation.child_trigger_message_id == claim.child_trigger_message_id
            and claim_expiry is not None
            and claim_expiry > now
        )
        if not exact_claim:
            return False
        if descriptor.interrupt_requested_generation == claim.descriptor_generation:
            now = datetime.now(timezone.utc)
            await _complete_locked(
                db,
                activation=activation,
                descriptor=descriptor,
                outcome=OUTCOME_INTERRUPTED,
                raw_result=_error_result(
                    activation,
                    descriptor,
                    outcome=OUTCOME_INTERRUPTED,
                    message="Subagent activation was interrupted before wake.",
                    recovery_code="subagent_interrupted_before_wake",
                ),
                now=now,
            )
            descriptor.interrupt_applied_generation = claim.descriptor_generation
            return False
        driver = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == claim.child_session_id,
                    AgentDriverState.user_id == claim.user_id,
                    AgentDriverState.run_id == lease.run_id,
                    AgentDriverState.generation == lease.generation,
                    AgentDriverState.owner_id == lease.owner_id,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.trigger_message_id == claim.child_trigger_message_id,
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > _database_now(db),
                )
            )
        ).scalar_one_or_none()
        if driver is None:
            raise SubagentFenceError("child Driver no longer matches activation claim")
        activation.child_run_id = lease.run_id
        activation.child_generation = lease.generation
        activation.state = "bound"
        activation.claim_expires_at = None
        activation.updated_at = datetime.now(timezone.utc)
        return True


async def claim_is_dispatchable(claim: ActivationClaim) -> bool:
    """Recheck DB/local claim and interrupt immediately before child reserve."""
    if time.monotonic() >= claim.local_deadline:
        return False
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.id == claim.activation_id,
                    SubagentActivation.user_id == claim.user_id,
                    SubagentActivation.state == "claimed",
                    SubagentActivation.claim_token == claim.token,
                    SubagentActivation.claim_owner == claim.owner,
                    SubagentActivation.claim_expires_at.is_not(None),
                    SubagentActivation.claim_expires_at > _database_now(db),
                )
            )
        ).scalar_one_or_none()
        if activation is None:
            return False
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == claim.descriptor_id,
                    SubagentDescriptor.user_id == claim.user_id,
                    SubagentDescriptor.active_activation_id == claim.activation_id,
                    SubagentDescriptor.generation == claim.descriptor_generation,
                    SubagentDescriptor.state == "active",
                )
            )
        ).scalar_one_or_none()
        return bool(
            descriptor is not None
            and descriptor.interrupt_requested_generation
            != claim.descriptor_generation
        )


async def bind_recovered_activation(
    lease: Any,
    record: Any,
) -> RecoveredActivationBinding:
    """Revalidate any exact-trigger activation after Driver takeover.

    Absence means this is an ordinary/legacy prompt. A terminal match is
    intentionally distinct: another worker may have completed the activation
    after the pre-takeover scan, and that accepted trigger must not run again.
    """
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.child_session_id == lease.session_id,
                    SubagentActivation.user_id == lease.user_id,
                    SubagentActivation.child_trigger_message_id == record.trigger_message_id,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if activation is None:
            return RecoveredActivationBinding(state="absent", activation_id=None)
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == activation.descriptor_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if descriptor is None:
            raise SubagentFenceError("recovered activation descriptor disappeared")
        if (
            record.phase != "reserved"
            or not record.run_id
            or record.session_id != activation.child_session_id
            or record.user_id != activation.user_id
            or record.trigger_message_id != activation.child_trigger_message_id
            or lease.generation <= record.generation
        ):
            raise SubagentFenceError("recovered activation marker mismatch")
        driver = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == lease.session_id,
                    AgentDriverState.user_id == lease.user_id,
                    AgentDriverState.run_id == lease.run_id,
                    AgentDriverState.generation == lease.generation,
                    AgentDriverState.owner_id == lease.owner_id,
                    AgentDriverState.phase == "reserved",
                    AgentDriverState.trigger_message_id == record.trigger_message_id,
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > _database_now(db),
                )
            )
        ).scalar_one_or_none()
        if driver is None:
            raise SubagentFenceError("recovered activation takeover lease mismatch")
        if activation.state in {"completed", "abandoned"}:
            return RecoveredActivationBinding(
                state="terminal",
                activation_id=activation.id,
            )
        if (
            descriptor.active_activation_id != activation.id
            or descriptor.generation != activation.descriptor_generation
            or descriptor.state != "active"
        ):
            raise SubagentFenceError("recovered activation descriptor mismatch")
        if descriptor.interrupt_requested_generation == activation.descriptor_generation:
            raise SubagentFenceError("recovered activation is interrupted")
        if activation.child_generation is None:
            activation.child_run_id = record.run_id
            activation.child_generation = record.generation
        elif (
            activation.child_generation != record.generation
            or activation.child_run_id != record.run_id
        ):
            raise SubagentFenceError("recovered activation old bind mismatch")
        activation.child_run_id = lease.run_id
        activation.child_generation = lease.generation
        activation.state = "bound"
        activation.claim_token = uuid.uuid4().hex
        activation.claim_owner = lease.owner_id
        activation.claim_expires_at = None
        activation.updated_at = datetime.now(timezone.utc)
        return RecoveredActivationBinding(
            state="bound",
            activation_id=activation.id,
        )


async def activation_interrupt_pending(record: Any) -> bool:
    """Consume an interrupt before recovery is allowed to reserve/wake."""
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.child_session_id == record.session_id,
                    SubagentActivation.user_id == record.user_id,
                    SubagentActivation.child_trigger_message_id == record.trigger_message_id,
                    SubagentActivation.state.in_(("accepted", "claimed", "bound")),
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if activation is None:
            return False
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == activation.descriptor_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            descriptor is None
            or descriptor.interrupt_requested_generation
            != activation.descriptor_generation
        ):
            return False
        # An unstarted/reserved activation has no ambiguous provider boundary.
        if record.phase == "reserved":
            await _complete_locked(
                db,
                activation=activation,
                descriptor=descriptor,
                outcome=OUTCOME_INTERRUPTED,
                raw_result=_error_result(
                    activation,
                    descriptor,
                    outcome=OUTCOME_INTERRUPTED,
                    message="Subagent activation was interrupted before provider execution.",
                    recovery_code="subagent_interrupted_reserved",
                ),
                now=datetime.now(timezone.utc),
            )
            descriptor.interrupt_applied_generation = activation.descriptor_generation
        return True


async def abandon_claim(claim: ActivationClaim) -> bool:
    """Yield an unbound claim immediately; exact-CAS and idempotent."""
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.id == claim.activation_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            activation is None
            or activation.state != "claimed"
            or activation.claim_token != claim.token
            or activation.claim_owner != claim.owner
        ):
            return False
        activation.state = "accepted"
        activation.claim_token = None
        activation.claim_owner = None
        activation.claim_expires_at = None
        activation.updated_at = datetime.now(timezone.utc)
        return True


def _error_result(
    activation: SubagentActivation,
    descriptor: SubagentDescriptor,
    *,
    outcome: str,
    message: str,
    recovery_code: str,
) -> dict:
    return {
        "title": activation.task_title,
        "output": "\n".join((
            f"task_id: {descriptor.id}",
            f"child_session_id: {descriptor.child_session_id}",
            "",
            "<task_result>",
            message,
            "</task_result>",
        )),
        "metadata": {
            "subagent_id": descriptor.id,
            "subagent_activation_id": activation.id,
            "child_session_id": descriptor.child_session_id,
            "subagent_type": descriptor.subagent_type,
            "subagent_generation": activation.descriptor_generation,
            "subagent_outcome": outcome,
            "subagent_outbox_completed": True,
            "task_handoff_id": activation.id,
            "task_outbox_completed": True,
            "error": True,
            "recovery_code": recovery_code,
            "truncated": False,
        },
    }


async def _project_result(raw: dict) -> dict:
    """Store only a bounded, public ToolResult projection in the outbox."""
    from tool.truncation import truncate_output

    truncated = await truncate_output(str(raw.get("output") or ""))
    raw_metadata = raw.get("metadata")
    raw_metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    metadata: dict[str, Any] = {}
    for key in {
        "subagent_id",
        "subagent_activation_id",
        "child_session_id",
        "subagent_type",
        "subagent_outcome",
        "recovery_code",
        "task_handoff_id",
    }:
        value = raw_metadata.get(key)
        if isinstance(value, str):
            metadata[key] = value[:256]
    for key in {"subagent_generation"}:
        value = raw_metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            metadata[key] = value
    for key in {"subagent_outbox_completed", "task_outbox_completed", "error"}:
        value = raw_metadata.get(key)
        if isinstance(value, bool):
            metadata[key] = value
    structured = raw_metadata.get("structured_result")
    if isinstance(structured, dict):
        from agent.subagent_composition import MAX_STRUCTURED_RESULT_BYTES

        try:
            encoded = json.dumps(
                structured,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            encoded = b""
        if encoded and len(encoded) <= MAX_STRUCTURED_RESULT_BYTES:
            metadata["structured_result"] = json.loads(encoded.decode("utf-8"))
    metadata["truncated"] = truncated.truncated
    return {
        "title": str(raw.get("title") or "")[:255],
        "output": truncated.content,
        "metadata": metadata,
    }


async def _complete_locked(
    db,
    *,
    activation: SubagentActivation,
    descriptor: SubagentDescriptor,
    outcome: str,
    raw_result: dict,
    now: datetime,
) -> dict:
    """Complete activation/outbox/descriptor together while rows are locked."""
    if outcome not in TERMINAL_OUTCOMES:
        raise ValueError("invalid terminal subagent outcome")
    outbox = (
        await db.execute(
            select(SubagentOutbox).where(
                SubagentOutbox.activation_id == activation.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if outbox is None:
        raise SubagentFenceError("subagent outbox disappeared")
    if outbox.state in {"ready", "delivered"}:
        if outbox.outcome != outcome or not isinstance(outbox.result_payload, dict):
            raise SubagentFenceError("terminal subagent outcome changed")
        return dict(outbox.result_payload.get("projected") or {})
    projected = await _project_result(raw_result)
    outbox.state = "ready"
    outbox.outcome = outcome
    outbox.result_payload = {"projected": projected}
    outbox.ready_at = now
    outbox.updated_at = now
    activation.state = "completed"
    activation.completed_at = now
    activation.claim_expires_at = None
    activation.updated_at = now
    if (
        descriptor.active_activation_id == activation.id
        and descriptor.generation == activation.descriptor_generation
    ):
        descriptor.active_activation_id = None
        if descriptor.lifecycle == "one_shot":
            descriptor.state = (
                "settled" if outcome == OUTCOME_SUCCEEDED else
                "interrupted" if outcome == OUTCOME_INTERRUPTED else "error"
            )
            descriptor.settled_at = now
        descriptor.updated_at = now
    return projected


async def complete_activation(
    activation_id: str,
    *,
    child_run_id: str,
    child_generation: int,
    outcome: str,
    raw_result: dict,
) -> dict:
    """Fence and durably complete one child activation."""
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.id == activation_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if activation is None:
            raise SubagentFenceError("subagent activation not found")
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == activation.descriptor_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if descriptor is None:
            raise SubagentFenceError("subagent descriptor not found")
        if (
            activation.child_run_id != child_run_id
            or activation.child_generation != child_generation
        ):
            raise SubagentFenceError("stale child result rejected by activation fence")
        outbox = (
            await db.execute(
                select(SubagentOutbox).where(
                    SubagentOutbox.activation_id == activation.id
                )
            )
        ).scalar_one_or_none()
        if activation.state == "completed":
            if outbox is None or outbox.outcome != outcome or not isinstance(
                outbox.result_payload, dict
            ):
                raise SubagentFenceError("completed activation outbox mismatch")
            return dict(outbox.result_payload.get("projected") or {})
        if activation.state != "bound":
            raise SubagentFenceError(f"cannot complete {activation.state} activation")
        child = (
            await db.execute(
                select(Session).where(
                    Session.id == activation.child_session_id,
                    Session.user_id == activation.user_id,
                    Session.project_id == activation.project_id,
                    Session.parent_id == activation.parent_session_id,
                    Session.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        driver = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == activation.child_session_id,
                    AgentDriverState.user_id == activation.user_id,
                    AgentDriverState.generation == child_generation,
                )
            )
        ).scalar_one_or_none()
        if child is None or driver is None or driver.run_id not in {None, child_run_id}:
            raise SubagentFenceError("child completion identity mismatch")
        return await _complete_locked(
            db,
            activation=activation,
            descriptor=descriptor,
            outcome=outcome,
            raw_result=raw_result,
            now=datetime.now(timezone.utc),
        )


async def _result_from_transcript(
    activation_id: str,
    *,
    forced_outcome: str | None = None,
    recovery_code: str | None = None,
) -> tuple[str, dict]:
    """Read only a terminal answer to the activation's exact trigger."""
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.id == activation_id
                )
            )
        ).scalar_one_or_none()
        if activation is None:
            raise SubagentFenceError("subagent activation not found")
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == activation.descriptor_id
                )
            )
        ).scalar_one_or_none()
        if descriptor is None:
            raise SubagentFenceError("subagent descriptor not found")
        outcome = forced_outcome
        text = ""
        structured_result: dict[str, Any] | None = None
        if outcome is None:
            child_status = (
                await db.execute(
                    select(Session.status).where(
                        Session.id == activation.child_session_id,
                        Session.user_id == activation.user_id,
                        Session.project_id == activation.project_id,
                        Session.parent_id == activation.parent_session_id,
                        Session.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            terminal = None
            if child_status == "idle":
                terminal = (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == activation.child_session_id,
                            Message.user_id == activation.user_id,
                            Message.role == "assistant",
                            Message.parent_id == activation.child_trigger_message_id,
                            Message.finish == "stop",
                            _message_error_is_empty(),
                        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
                    )
                ).scalar_one_or_none()
            if terminal is not None:
                text_parts = list((await db.execute(
                    select(Part).where(
                        Part.message_id == terminal.id,
                        Part.session_id == activation.child_session_id,
                        Part.user_id == activation.user_id,
                        Part.type == "text",
                    ).order_by(Part.created_at.desc(), Part.id.desc())
                )).scalars().all())
                for part in text_parts:
                    data = dict(part.data or {})
                    if data.get("text"):
                        text = str(data["text"])
                        break
                from agent.subagent_authority import (
                    SubagentAuthorityError,
                    parse_subagent_authority,
                )

                try:
                    authority = parse_subagent_authority(
                        descriptor.authority_snapshot
                    )
                except SubagentAuthorityError as exc:
                    raise SubagentFenceError(str(exc)) from exc
                schema = (
                    authority.composition.output_schema
                    if authority.composition is not None else None
                )
                if schema is not None:
                    from agent.subagent_composition import (
                        SubagentCompositionError,
                        validate_structured_result,
                    )

                    try:
                        structured_result = validate_structured_result(
                            schema,
                            terminal.structured,
                        )
                    except SubagentCompositionError:
                        outcome = OUTCOME_ERROR
                        recovery_code = "subagent_structured_result_invalid"
                    else:
                        outcome = OUTCOME_SUCCEEDED
                else:
                    outcome = OUTCOME_SUCCEEDED
            else:
                outcome = OUTCOME_ERROR
                recovery_code = recovery_code or "subagent_no_terminal_result"
        if outcome == OUTCOME_SUCCEEDED:
            body = (
                json.dumps(
                    structured_result,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                if structured_result is not None else text
            )
            metadata = {
                "subagent_id": descriptor.id,
                "subagent_activation_id": activation.id,
                "child_session_id": descriptor.child_session_id,
                "subagent_type": descriptor.subagent_type,
                "subagent_generation": activation.descriptor_generation,
                "subagent_outcome": outcome,
                "subagent_outbox_completed": True,
                "task_handoff_id": activation.id,
                "task_outbox_completed": True,
            }
            if structured_result is not None:
                metadata["structured_result"] = structured_result
            raw = {
                "title": activation.task_title,
                "output": "\n".join((
                    f"task_id: {descriptor.id}",
                    f"child_session_id: {descriptor.child_session_id}",
                    "",
                    "<task_result>",
                    body,
                    "</task_result>",
                )) if body else f"task_id: {descriptor.id}\n\nTask completed with no text output.",
                "metadata": metadata,
            }
        else:
            messages = {
                OUTCOME_INTERRUPTED: "Subagent activation was interrupted and produced no successful terminal answer.",
                OUTCOME_UNKNOWN: (
                    "Subagent execution was interrupted after provider or tool execution may have started. "
                    "Its external outcome is unknown and was not replayed."
                ),
                OUTCOME_ERROR: (
                    "Subagent did not produce a terminal successful answer. Partial output was not reported as success."
                ),
            }
            raw = _error_result(
                activation,
                descriptor,
                outcome=outcome,
                message=messages[outcome],
                recovery_code=recovery_code or f"subagent_{outcome}",
            )
        return outcome, raw


async def complete_activation_from_transcript(
    activation_id: str,
    *,
    child_run_id: str,
    child_generation: int,
    forced_outcome: str | None = None,
    recovery_code: str | None = None,
) -> dict:
    outcome, raw = await _result_from_transcript(
        activation_id,
        forced_outcome=forced_outcome,
        recovery_code=recovery_code,
    )
    return await complete_activation(
        activation_id,
        child_run_id=child_run_id,
        child_generation=child_generation,
        outcome=outcome,
        raw_result=raw,
    )


async def complete_activation_for_child(
    child_session_id: str,
    *,
    child_run_id: str,
    child_generation: int,
) -> str | None:
    async with get_db_session() as db:
        activation_id = (
            await db.execute(
                select(SubagentActivation.id).where(
                    SubagentActivation.child_session_id == child_session_id,
                    SubagentActivation.child_run_id == child_run_id,
                    SubagentActivation.child_generation == child_generation,
                    SubagentActivation.state == "bound",
                )
            )
        ).scalar_one_or_none()
    if activation_id is None:
        return None
    await complete_activation_from_transcript(
        activation_id,
        child_run_id=child_run_id,
        child_generation=child_generation,
    )
    return activation_id


async def activation_completion_disposition(
    activation_id: str,
    *,
    child_run_id: str,
    child_generation: int,
) -> str:
    """Classify the exact Driver boundary after a foreground loop returns.

    ``reserved`` is still replay-safe and must remain in the inbox. A started
    provider/tool boundary is terminal but uncertain. Only an exact idle
    release permits ordinary transcript completion.
    """
    async with get_db_session() as db:
        activation = (
            await db.execute(
                select(SubagentActivation).where(
                    SubagentActivation.id == activation_id
                )
            )
        ).scalar_one_or_none()
        if activation is None:
            raise SubagentFenceError("subagent activation not found")
        if activation.state == "completed":
            return "wait"
        if (
            activation.state != "bound"
            or activation.child_run_id != child_run_id
            or activation.child_generation != child_generation
        ):
            return "wait"
        driver = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == activation.child_session_id,
                    AgentDriverState.user_id == activation.user_id,
                )
            )
        ).scalar_one_or_none()
        if driver is None or driver.generation != child_generation:
            return "wait"
        if driver.phase == "idle" and driver.run_id is None:
            return "terminal"
        if driver.run_id != child_run_id:
            return "wait"
        if driver.phase == "reserved":
            return "replay"
        if driver.phase in {"running", "finalizing"}:
            return OUTCOME_UNKNOWN
        return "wait"


async def activation_blocks_reserved_replay(record: Any) -> bool:
    """Fail closed when a terminal activation's old trigger is rediscovered."""
    if not record.trigger_message_id:
        return False
    async with get_db_session() as db:
        state = (
            await db.execute(
                select(SubagentActivation.state).where(
                    SubagentActivation.child_session_id == record.session_id,
                    SubagentActivation.user_id == record.user_id,
                    SubagentActivation.child_trigger_message_id
                    == record.trigger_message_id,
                )
            )
        ).scalar_one_or_none()
        return state in {"completed", "abandoned"}


async def wait_for_outbox(
    activation_id: str,
    *,
    user_id: str,
    abort: asyncio.Event | None = None,
    poll_seconds: float = 0.2,
) -> dict:
    """Wait for whichever exact claim owner completes this activation."""
    while True:
        async with get_db_session() as db:
            row = (
                await db.execute(
                    select(SubagentOutbox).where(
                        SubagentOutbox.activation_id == activation_id,
                        SubagentOutbox.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError("subagent activation outbox not found")
            if row.state in {"ready", "delivered"}:
                if not isinstance(row.result_payload, dict):
                    raise SubagentFenceError("terminal outbox has no payload")
                return dict(row.result_payload.get("projected") or {})
        if abort is not None and abort.is_set():
            raise asyncio.CancelledError
        await asyncio.sleep(poll_seconds)


async def interrupt_subagent(
    descriptor_id: str,
    *,
    user_id: str,
    parent_session_id: str,
    project_id: str,
) -> dict:
    """Commit a generation-scoped interrupt, then nudge its current Driver."""
    abort_target: tuple[str, str, int] | None = None
    generation: int | None = None
    for _attempt in range(3):
        retry = False
        async with get_db_session() as db:
            active_activation_id = (
                await db.execute(
                    select(SubagentDescriptor.active_activation_id).where(
                        SubagentDescriptor.id == descriptor_id
                    )
                )
            ).scalar_one_or_none()
            activation = None
            if active_activation_id is not None:
                # Global subagent order is activation -> descriptor. Snapshot
                # the pointer first, then revalidate it after both locks.
                activation = (
                    await db.execute(
                        select(SubagentActivation).where(
                            SubagentActivation.id == active_activation_id
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
            descriptor = (
                await db.execute(
                    select(SubagentDescriptor).where(
                        SubagentDescriptor.id == descriptor_id
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if descriptor is None:
                raise LookupError("subagent not found")
            if (
                descriptor.user_id != user_id
                or descriptor.parent_session_id != parent_session_id
                or descriptor.project_id != project_id
            ):
                raise SubagentFenceError(
                    "subagent direct lineage or project mismatch"
                )
            generation = descriptor.generation
            if descriptor.active_activation_id != active_activation_id:
                retry = True
            elif active_activation_id is None:
                return {
                    "task_id": descriptor.id,
                    "state": descriptor.state,
                    "generation": generation,
                    "interrupt_requested": False,
                }
            elif (
                activation is None
                or activation.descriptor_id != descriptor.id
                or activation.descriptor_generation != generation
            ):
                raise SubagentFenceError("interrupt activation identity mismatch")
            else:
                descriptor.interrupt_requested_generation = generation
                descriptor.updated_at = datetime.now(timezone.utc)
                if activation.state == "bound":
                    if (
                        activation.child_run_id is None
                        or activation.child_generation is None
                    ):
                        raise SubagentFenceError(
                            "bound activation has no exact child generation"
                        )
                    abort_target = (
                        activation.child_session_id,
                        activation.child_run_id,
                        activation.child_generation,
                    )
        if not retry:
            break
    else:
        raise SubagentBusyError("subagent activation changed during interrupt")
    # The request is already durable. A crash here is closed by the periodic
    # interrupt scan before any accepted activation may be reserved or woken.
    if abort_target is not None:
        from agent.driver import request_abort

        child_session_id, child_run_id, child_generation = abort_target
        applied = await request_abort(
            child_session_id,
            user_id,
            expected_run_id=child_run_id,
            expected_generation=child_generation,
        )
        if applied:
            await _mark_interrupt_applied(descriptor_id, generation)
    return {
        "task_id": descriptor_id,
        "state": "interrupt_requested",
        "generation": generation,
        "interrupt_requested": True,
    }


async def _mark_interrupt_applied(descriptor_id: str, generation: int) -> None:
    async with get_db_session() as db:
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == descriptor_id
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            descriptor is not None
            and descriptor.interrupt_requested_generation == generation
        ):
            descriptor.interrupt_applied_generation = generation
            descriptor.updated_at = datetime.now(timezone.utc)


async def consume_interrupt_requests() -> int:
    """Converge committed interrupts before activation dispatch scanning."""
    async with get_db_session() as db:
        ids = list((await db.execute(
            select(SubagentDescriptor.id).where(
                SubagentDescriptor.active_activation_id.is_not(None),
                SubagentDescriptor.interrupt_requested_generation.is_not(None),
                or_(
                    SubagentDescriptor.interrupt_applied_generation.is_(None),
                    SubagentDescriptor.interrupt_applied_generation
                    != SubagentDescriptor.interrupt_requested_generation,
                ),
            ).order_by(SubagentDescriptor.id)
        )).scalars().all())
    changed = 0
    for descriptor_id in ids:
        request_abort_target: tuple[str, str, str, int, int] | None = None
        async with get_db_session() as db:
            # Claim/bind/completion consistently lock activation -> descriptor.
            # Snapshot the pointer without a write lock, then take that same
            # order here and revalidate the descriptor under lock. This avoids
            # a PostgreSQL claim-vs-interrupt deadlock while remaining exact if
            # an activation settles between the two reads.
            active_activation_id = (
                await db.execute(
                    select(SubagentDescriptor.active_activation_id).where(
                        SubagentDescriptor.id == descriptor_id
                    )
                )
            ).scalar_one_or_none()
            if active_activation_id is None:
                continue
            activation = (
                await db.execute(
                    select(SubagentActivation).where(
                        SubagentActivation.id == active_activation_id
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            descriptor = (
                await db.execute(
                    select(SubagentDescriptor).where(
                        SubagentDescriptor.id == descriptor_id
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if descriptor is None:
                continue
            generation = descriptor.interrupt_requested_generation
            if (
                generation is None
                or generation != descriptor.generation
                or descriptor.active_activation_id != active_activation_id
            ):
                continue
            if activation is None or activation.descriptor_generation != generation:
                raise SubagentFenceError("interrupt activation identity mismatch")
            if activation.state in {"accepted", "claimed"}:
                await _complete_locked(
                    db,
                    activation=activation,
                    descriptor=descriptor,
                    outcome=OUTCOME_INTERRUPTED,
                    raw_result=_error_result(
                        activation,
                        descriptor,
                        outcome=OUTCOME_INTERRUPTED,
                        message="Subagent activation was interrupted before provider execution.",
                        recovery_code="subagent_interrupted_before_start",
                    ),
                    now=datetime.now(timezone.utc),
                )
                descriptor.interrupt_applied_generation = generation
                changed += 1
            elif activation.state == "bound":
                if (
                    activation.child_run_id is None
                    or activation.child_generation is None
                ):
                    raise SubagentFenceError(
                        "bound interrupt activation has no child generation"
                    )
                request_abort_target = (
                    descriptor.child_session_id,
                    descriptor.user_id,
                    activation.child_run_id,
                    activation.child_generation,
                    generation,
                )
            else:
                descriptor.interrupt_applied_generation = generation
        if request_abort_target is not None:
            from agent.driver import request_abort

            (
                child_id,
                owner_id,
                child_run_id,
                child_generation,
                generation,
            ) = request_abort_target
            applied = await request_abort(
                child_id,
                owner_id,
                expected_run_id=child_run_id,
                expected_generation=child_generation,
            )
            if applied:
                await _mark_interrupt_applied(descriptor_id, generation)
                changed += 1
    return changed


async def report_subagent(
    descriptor_id: str,
    *,
    user_id: str,
    parent_session_id: str,
    project_id: str,
) -> dict:
    """Return a bounded direct-parent status snapshot; never partial success."""
    async with get_db_session() as db:
        descriptor = (
            await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == descriptor_id
                )
            )
        ).scalar_one_or_none()
        if descriptor is None:
            raise LookupError("subagent not found")
        if (
            descriptor.user_id != user_id
            or descriptor.parent_session_id != parent_session_id
            or descriptor.project_id != project_id
        ):
            raise SubagentFenceError("subagent direct lineage or project mismatch")
        latest = (
            await db.execute(
                select(SubagentActivation, SubagentOutbox)
                .join(
                    SubagentOutbox,
                    SubagentOutbox.activation_id == SubagentActivation.id,
                )
                .where(SubagentActivation.descriptor_id == descriptor.id)
                .order_by(SubagentActivation.descriptor_generation.desc())
                .limit(1)
            )
        ).one_or_none()
        result = None
        activation_state = None
        outcome = None
        if latest is not None:
            activation, outbox = latest
            activation_state = activation.state
            outcome = outbox.outcome
            if outbox.state in {"ready", "delivered"} and isinstance(
                outbox.result_payload, dict
            ):
                projected = outbox.result_payload.get("projected")
                if isinstance(projected, dict):
                    result = {
                        "title": str(projected.get("title") or "")[:255],
                        "output": str(projected.get("output") or ""),
                        "metadata": dict(projected.get("metadata") or {}),
                    }
        return {
            "task_id": descriptor.id,
            "child_session_id": descriptor.child_session_id,
            "subagent_type": descriptor.subagent_type,
            "lifecycle": descriptor.lifecycle,
            "state": descriptor.state,
            "generation": descriptor.generation,
            "activation_state": activation_state,
            "outcome": outcome,
            # Waiting/bound activations deliberately return no transcript text.
            "result": result,
        }


async def list_subagent_reports(
    *,
    user_id: str,
    parent_session_id: str,
    project_id: str,
) -> list[dict]:
    async with get_db_session() as db:
        rows = list((await db.execute(
            select(SubagentDescriptor).where(
                SubagentDescriptor.user_id == user_id,
                SubagentDescriptor.parent_session_id == parent_session_id,
                SubagentDescriptor.project_id == project_id,
            ).order_by(
                SubagentDescriptor.created_at.desc(),
                SubagentDescriptor.id.desc(),
            ).limit(MAX_LIST_RESULTS)
        )).scalars().all())
    return [
        {
            "task_id": row.id,
            "child_session_id": row.child_session_id,
            "subagent_type": row.subagent_type,
            "lifecycle": row.lifecycle,
            "state": row.state,
            "generation": row.generation,
            "active": row.active_activation_id is not None,
        }
        for row in rows
    ]


async def claimable_activation_ids() -> list[str]:
    """Snapshot accepted and expired-claim inbox work in stable order."""
    async with get_db_session() as db:
        return list((await db.execute(
            select(SubagentActivation.id).where(
                or_(
                    SubagentActivation.state == "accepted",
                    (
                        (SubagentActivation.state == "claimed")
                        & SubagentActivation.claim_expires_at.is_not(None)
                        & (SubagentActivation.claim_expires_at <= _database_now(db))
                    ),
                )
            ).order_by(SubagentActivation.created_at, SubagentActivation.id)
        )).scalars().all())


async def has_subagent_state() -> bool:
    """Cheap gate that keeps legacy-only recovery passes short on SQLite."""
    async with get_db_session() as db:
        result = await db.execute(select(SubagentDescriptor.id).limit(1))
        found = result.scalar_one_or_none() is not None
        result.close()
        return found


async def recover_subagent_outboxes(records: Iterable[Any]) -> int:
    """Materialize exact terminal outcomes without replaying uncertain work."""
    completed = 0
    reserved_children: set[str] = set()
    for record in records:
        if record.phase == "reserved":
            reserved_children.add(record.session_id)
            continue
        if record.phase not in {"running", "finalizing"} or not record.run_id:
            continue
        async with get_db_session() as db:
            activation = (
                await db.execute(
                    select(SubagentActivation).where(
                        SubagentActivation.child_session_id == record.session_id,
                        SubagentActivation.user_id == record.user_id,
                        SubagentActivation.child_trigger_message_id
                        == record.trigger_message_id,
                        SubagentActivation.state.in_(("claimed", "bound")),
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if activation is None:
                continue
            if activation.child_generation is None:
                activation.child_run_id = record.run_id
                activation.child_generation = record.generation
                activation.state = "bound"
            elif (
                activation.child_run_id != record.run_id
                or activation.child_generation != record.generation
            ):
                continue
            activation_id = activation.id
        await complete_activation_from_transcript(
            activation_id,
            child_run_id=record.run_id,
            child_generation=record.generation,
            forced_outcome=OUTCOME_UNKNOWN,
            recovery_code="subagent_child_outcome_unknown",
        )
        completed += 1

    # A successful child releases its Driver before its parent coroutine can
    # commit the outbox. Scan idle exact generations even with no expired rows.
    async with get_db_session() as db:
        candidates = list((await db.execute(
            select(
                SubagentActivation.id,
                SubagentActivation.child_session_id,
                SubagentActivation.child_run_id,
                SubagentActivation.child_generation,
            ).join(
                AgentDriverState,
                AgentDriverState.session_id == SubagentActivation.child_session_id,
            ).where(
                SubagentActivation.state == "bound",
                SubagentActivation.child_run_id.is_not(None),
                SubagentActivation.child_generation.is_not(None),
                AgentDriverState.phase == "idle",
                AgentDriverState.generation == SubagentActivation.child_generation,
            )
        )).all())
    for activation_id, child_id, run_id, generation in candidates:
        if child_id in reserved_children or run_id is None or generation is None:
            continue
        await complete_activation_from_transcript(
            activation_id,
            child_run_id=run_id,
            child_generation=generation,
        )
        completed += 1
    return completed


async def _parent_tail_eligible(db, activation: SubagentActivation) -> bool:
    newest = (
        await db.execute(
            select(Message.id).where(
                Message.session_id == activation.parent_session_id,
                Message.user_id == activation.user_id,
            ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if newest != activation.parent_message_id:
        return False
    part = (
        await db.execute(
            select(Part).where(
                Part.id == activation.parent_part_id,
                Part.message_id == activation.parent_message_id,
                Part.session_id == activation.parent_session_id,
                Part.user_id == activation.user_id,
                Part.type == "tool",
            )
        )
    ).scalar_one_or_none()
    return part is not None and _is_task_part(part)


def _part_has_exact_outbox_delivery(
    part: Part,
    *,
    outbox: SubagentOutbox,
    activation: SubagentActivation,
    descriptor: SubagentDescriptor,
) -> bool:
    """Return whether the live parent already persisted this exact result.

    Foreground Task execution completes the child outbox before the Processor
    commits the parent ToolPart.  The Processor may then append the terminal
    Assistant message before the periodic scanner runs, so the exact Task part
    is historical rather than the literal transcript tail.  Its immutable
    activation/generation markers still authoritatively prove delivery.
    """
    if not _is_task_part(part):
        return False
    data = dict(part.data or {})
    metadata = dict(data.get("metadata") or {})
    status = getattr(data.get("status"), "value", data.get("status"))
    exact_pointer = (
        (
            metadata.get("subagent_id") == descriptor.id
            and metadata.get("subagent_activation_id") == activation.id
            and metadata.get("subagent_generation")
            == activation.descriptor_generation
        )
        or metadata.get("task_handoff_id") == activation.id
    )
    exact_delivery = (
        (
            metadata.get("subagent_outbox_completed") is True
            and metadata.get("subagent_outcome") == outbox.outcome
        )
        or (
            metadata.get("task_handoff_id") == activation.id
            and metadata.get("task_outbox_completed") is True
        )
    )
    return bool(exact_pointer and exact_delivery and status in {"completed", "error"})


async def apply_ready_subagent_outboxes_locked(
    db,
    *,
    parent_session_id: str,
    user_id: str,
    maintenance_run_id: str,
    maintenance_generation: int,
) -> ApplyResult:
    """Project ready outboxes only into their exact independent ToolPart."""
    rows = list((await db.execute(
        select(SubagentOutbox, SubagentActivation, SubagentDescriptor)
        .join(
            SubagentActivation,
            SubagentActivation.id == SubagentOutbox.activation_id,
        )
        .join(
            SubagentDescriptor,
            SubagentDescriptor.id == SubagentOutbox.descriptor_id,
        )
        .where(
            SubagentOutbox.parent_session_id == parent_session_id,
            SubagentOutbox.user_id == user_id,
            SubagentOutbox.state == "ready",
        )
        .order_by(SubagentActivation.created_at, SubagentActivation.id)
        .with_for_update()
    )).all())
    if not rows:
        return ApplyResult(rejoined=0, updates=(), message_ids=())
    parent = (
        await db.execute(
            select(Session).where(
                Session.id == parent_session_id,
                Session.user_id == user_id,
                Session.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if parent is None:
        raise SubagentFenceError("parent Session disappeared during outbox delivery")
    now = datetime.now(timezone.utc)
    updates: list[dict] = []
    message_ids: list[str] = []
    delivered = 0
    for outbox, activation, descriptor in rows:
        if activation.parent_generation >= maintenance_generation:
            continue
        if (
            outbox.project_id != activation.project_id
            or outbox.parent_part_id != activation.parent_part_id
            or descriptor.user_id != user_id
            or descriptor.project_id != activation.project_id
            or descriptor.parent_session_id != parent_session_id
        ):
            raise SubagentFenceError("outbox lineage identity mismatch")
        part = (
            await db.execute(
                select(Part).where(
                    Part.id == activation.parent_part_id,
                    Part.message_id == activation.parent_message_id,
                    Part.session_id == parent_session_id,
                    Part.user_id == user_id,
                    Part.type == "tool",
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if part is None or not _is_task_part(part):
            continue
        already_delivered = _part_has_exact_outbox_delivery(
            part,
            outbox=outbox,
            activation=activation,
            descriptor=descriptor,
        )
        if not already_delivered and not await _parent_tail_eligible(db, activation):
            continue
        payload = outbox.result_payload if isinstance(outbox.result_payload, dict) else {}
        projected = payload.get("projected") if isinstance(payload, dict) else None
        if not isinstance(projected, dict):
            continue
        data = dict(part.data or {})
        metadata = dict(data.get("metadata") or {})
        exact_pointer = (
            (
                metadata.get("subagent_id") == descriptor.id
                and metadata.get("subagent_activation_id") == activation.id
                and metadata.get("subagent_generation")
                == activation.descriptor_generation
            )
            or metadata.get("task_handoff_id") == activation.id
        )
        if not exact_pointer:
            continue
        status = getattr(data.get("status"), "value", data.get("status"))
        if not already_delivered:
            if status not in {"pending", "running", "error"}:
                continue
            result_metadata = dict(projected.get("metadata") or {})
            public_metadata = {
                key: value
                for key, value in result_metadata.items()
                if key in {
                    "subagent_id",
                    "subagent_activation_id",
                    "child_session_id",
                    "subagent_type",
                    "subagent_generation",
                    "subagent_outcome",
                    "subagent_outbox_completed",
                    "truncated",
                    "recovery_code",
                    "task_handoff_id",
                    "task_outbox_completed",
                }
            }
            public_metadata.update(_activation_metadata(
                descriptor_id=descriptor.id,
                activation_id=activation.id,
                child_session_id=descriptor.child_session_id,
                subagent_type=descriptor.subagent_type,
                generation=activation.descriptor_generation,
            ))
            public_metadata.update({
                "subagent_outcome": outbox.outcome,
                "subagent_outbox_completed": True,
                "task_handoff_id": activation.id,
                "task_outbox_completed": True,
            })
            is_error = outbox.outcome != OUTCOME_SUCCEEDED
            output = str(projected.get("output") or "")
            data.update({
                "status": "error" if is_error else "completed",
                "output": output,
                "title": str(projected.get("title") or activation.task_title),
                "error": output if is_error else None,
                "metadata": public_metadata,
            })
            part.data = public_part_data(data)
            updates.append(dict(part.data))
            message = (
                await db.execute(
                    select(Message).where(
                        Message.id == activation.parent_message_id,
                        Message.session_id == parent_session_id,
                        Message.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if message is None:
                raise SubagentFenceError("parent Message disappeared during delivery")
            from session.agent_event_log import append_part_event_locked

            await append_part_event_locked(
                db,
                parent,
                part,
                message,
                operation="updated",
                run_fence=(
                    parent_session_id,
                    maintenance_run_id,
                    maintenance_generation,
                ),
            )
        outbox.state = "delivered"
        outbox.delivered_at = now
        outbox.updated_at = now
        delivered += 1
        message_ids.append(activation.parent_message_id)
    return ApplyResult(
        rejoined=delivered,
        updates=tuple(updates),
        message_ids=tuple(message_ids),
    )


async def ready_subagent_parent_sessions() -> list[tuple[str, str, str]]:
    """Return safe idle parent tails eligible for exact outbox projection."""
    async with get_db_session() as db:
        rows = list((await db.execute(
            select(SubagentOutbox, SubagentActivation, SubagentDescriptor)
            .join(
                SubagentActivation,
                SubagentActivation.id == SubagentOutbox.activation_id,
            )
            .join(
                SubagentDescriptor,
                SubagentDescriptor.id == SubagentOutbox.descriptor_id,
            )
            .where(SubagentOutbox.state == "ready")
            .order_by(SubagentOutbox.created_at, SubagentOutbox.activation_id)
            .with_for_update()
        )).all())
        candidates: dict[tuple[str, str], str] = {}
        for outbox, activation, descriptor in rows:
            live_parent = (
                await db.execute(
                    select(AgentDriverState.session_id).where(
                        AgentDriverState.session_id == activation.parent_session_id,
                        AgentDriverState.user_id == activation.user_id,
                        AgentDriverState.phase != "idle",
                        AgentDriverState.lease_expires_at.is_not(None),
                        AgentDriverState.lease_expires_at > _database_now(db),
                    )
                )
            ).scalar_one_or_none()
            if live_parent is not None:
                continue
            part = (
                await db.execute(
                    select(Part).where(
                        Part.id == activation.parent_part_id,
                        Part.message_id == activation.parent_message_id,
                        Part.session_id == activation.parent_session_id,
                        Part.user_id == activation.user_id,
                        Part.type == "tool",
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if part is not None and _part_has_exact_outbox_delivery(
                part,
                outbox=outbox,
                activation=activation,
                descriptor=descriptor,
            ):
                now = datetime.now(timezone.utc)
                outbox.state = "delivered"
                outbox.delivered_at = now
                outbox.updated_at = now
                continue
            if not await _parent_tail_eligible(db, activation):
                continue
            status = (
                await db.execute(
                    select(Session.status).where(
                        Session.id == activation.parent_session_id,
                        Session.user_id == activation.user_id,
                    )
                )
            ).scalar_one_or_none()
            if status is not None:
                candidates[(activation.parent_session_id, activation.user_id)] = status
        return [
            (session_id, owner_id, status)
            for (session_id, owner_id), status in candidates.items()
        ]
