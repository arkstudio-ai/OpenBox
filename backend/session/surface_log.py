"""Append-only provenance for changes to the live Session Surface.

``messages`` plus their public ``parts`` are still OpenBox's model-visible
projection.  This module does not change how that projection is read.  It
captures a recovery image before a projection rewrite so regenerate/dismiss
remain auditable without teaching every context reader about tombstones.

Callers must hold the owning session row lock and use the same ``AsyncSession``
for this append and the subsequent deletes.  The per-session row lock is the
sequence allocator/fence on PostgreSQL; ``BEGIN IMMEDIATE`` provides the same
write fence for desktop SQLite.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.identifier import ascending
from db.models.message import Message
from db.models.part import Part, public_part_data
from db.models.session import Session
from db.models.session_surface_event import SessionSurfaceEvent


SurfaceEventKind = Literal["regenerate", "dismiss"]
SURFACE_SNAPSHOT_VERSION = 1


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _message_snapshot(message: Message, parts: list[Part]) -> dict:
    """Return exactly the recoverable public ``MessageWithParts`` Surface."""
    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "created_at": _iso(message.created_at),
        "client_message_id": message.client_message_id,
        "agent": message.agent,
        "model": message.model,
        "variant": message.variant,
        "parent_id": message.parent_id,
        "finish": message.finish,
        "summary": message.summary,
        "tokens": deepcopy(message.tokens),
        "error": deepcopy(message.error),
        "reaction": message.reaction,
        "format": deepcopy(message.format),
        "structured": deepcopy(message.structured),
        "parts": [
            {
                "id": part.id,
                "type": part.type,
                "created_at": _iso(part.created_at),
                "data": deepcopy(public_part_data(part.data)),
            }
            for part in parts
        ],
    }


async def append_surface_change_locked(
    db: AsyncSession,
    session_row: Session,
    *,
    kind: SurfaceEventKind,
    anchor_message_id: str,
    hidden_message_ids: Sequence[str],
    replacement_run_id: str | None = None,
    replacement_generation: int | None = None,
) -> SessionSurfaceEvent:
    """Archive one Surface rewrite before its rows are removed.

    ``session_row`` must have been loaded by ``lock_owned_session`` in this
    transaction.  A flush makes the event durable *inside the transaction*
    before control returns to the caller to delete the live rows.  A later
    exception rolls both operations back together.
    """
    if kind not in ("regenerate", "dismiss"):
        raise ValueError(f"unsupported Surface event kind: {kind}")
    if not anchor_message_id:
        raise ValueError("Surface event anchor is required")

    requested = list(hidden_message_ids)
    if not requested:
        raise ValueError("Surface event must hide at least one message")
    if len(set(requested)) != len(requested):
        raise ValueError("Surface event hidden message ids must be unique")
    if anchor_message_id not in requested:
        raise ValueError("Surface event anchor must be part of the hidden branch")
    if (replacement_run_id is None) != (replacement_generation is None):
        raise ValueError("replacement run id and generation must be recorded together")
    if kind == "dismiss" and replacement_run_id is not None:
        raise ValueError("dismiss events cannot name a replacement run")
    if (
        replacement_generation is not None
        and (isinstance(replacement_generation, bool) or replacement_generation < 1)
    ):
        raise ValueError("replacement generation must be positive")

    messages = list((await db.execute(
        select(Message).where(
            Message.session_id == session_row.id,
            Message.user_id == session_row.user_id,
            Message.id.in_(requested),
        ).order_by(Message.created_at.asc(), Message.id.asc())
    )).scalars().all())
    found_ids = {message.id for message in messages}
    missing = set(requested) - found_ids
    if missing:
        # Failing closed is essential: a partial snapshot followed by a full
        # delete would make the supposedly append-only history unrecoverable.
        raise LookupError("Surface snapshot is missing hidden messages")

    parts = list((await db.execute(
        select(Part).where(
            Part.message_id.in_(requested),
        ).order_by(Part.created_at.asc(), Part.id.asc())
    )).scalars().all())
    if any(
        part.session_id != session_row.id or part.user_id != session_row.user_id
        for part in parts
    ):
        raise LookupError("Surface snapshot contains an out-of-scope public part")
    parts_by_message: dict[str, list[Part]] = {}
    for part in parts:
        parts_by_message.setdefault(part.message_id, []).append(part)

    sequence = int((await db.execute(
        select(func.coalesce(func.max(SessionSurfaceEvent.sequence), 0) + 1).where(
            SessionSurfaceEvent.session_id == session_row.id
        )
    )).scalar_one())
    ordered_ids = [message.id for message in messages]
    now = datetime.now(timezone.utc)
    event = SessionSurfaceEvent(
        id=ascending("surface"),
        session_id=session_row.id,
        user_id=session_row.user_id,
        sequence=sequence,
        kind=kind,
        anchor_message_id=anchor_message_id,
        replacement_run_id=replacement_run_id,
        replacement_generation=replacement_generation,
        hidden_message_ids=ordered_ids,
        public_snapshot={
            "version": SURFACE_SNAPSHOT_VERSION,
            "messages": [
                _message_snapshot(message, parts_by_message.get(message.id, []))
                for message in messages
            ],
        },
        created_at=now,
    )
    db.add(event)
    await db.flush()
    return event
