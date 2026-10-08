"""Original evidence times, independent of extraction and storage clocks.

Legacy receipts stay immutable. A missing automatic-source time can be read
from the same canonical user/part revision after current authorization; this
does not turn an unknown manual note's storage time into an occurrence time.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import and_, or_, select

from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from memory.policy import MemoryAccessScope


def _value(event: Any, field: str) -> Any:
    return event.get(field) if isinstance(event, Mapping) else getattr(event, field, None)


def _utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # Canonical public timestamps have an offset. An ambiguous legacy
        # string is unknown; SQL datetime values are UTC even on SQLite.
        if value.tzinfo is None:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_user_occurrence(events: Iterable[Any], message_id: str, *,
                              end_sequence: int | None = None,
                              session_id: str | None = None) -> datetime | None:
    """Read the original User creation time, never a later snapshot/time.

    A legacy surface seed may preserve the real original Message timestamp;
    its import event timestamp cannot establish when the utterance occurred.
    """
    for event in sorted(events, key=lambda row: int(_value(row, "sequence"))):
        if end_sequence is not None and int(_value(event, "sequence")) > end_sequence:
            continue
        event_session = _value(event, "session_id")
        if session_id is not None and event_session != session_id:
            continue
        payload = _value(event, "payload")
        if not isinstance(payload, Mapping):
            continue
        kind = _value(event, "kind")
        message = None
        if kind == "message.created":
            candidate = payload.get("message")
            if isinstance(candidate, Mapping) and candidate.get("id") == message_id:
                message = candidate
        elif kind == "surface.seed":
            surface = payload.get("surface")
            if isinstance(surface, Mapping) and surface.get("session_id") == event_session:
                message = next((item for item in surface.get("messages", [])
                                if isinstance(item, Mapping) and item.get("id") == message_id), None)
        if message is None:
            continue
        if (message.get("role") != "user" or message.get("agent") == "compaction"
                or message.get("session_id") != event_session):
            return None
        if "created_at" in message:
            return _utc(message["created_at"])
        if kind == "message.created":
            return _utc(_value(event, "created_at"))
        return None
    return None


async def source_occurred_at(db, *, access: MemoryAccessScope, source) -> datetime | None:
    """Resolve authorized source time without rewriting any frozen row/hash.

    Persisted times can accompany readable provenance. Deriving a legacy NULL
    time additionally requires the original body and exact current canonical
    Part revision; revoked/changed sources and unanchored notes remain unknown.
    Inside one read-only authority pass each source is resolved once, like its
    availability (a memory and a Wiki page often cite the same words).
    """
    from memory.service import _facts, _memo_key
    facts = _facts(db, access)
    if facts is None:
        return await _source_occurred_at(db, access, source)
    key = _memo_key(source)
    if key not in facts.occurred:
        facts.occurred[key] = await _source_occurred_at(db, access, source)
    return facts.occurred[key]


async def _source_occurred_at(db, access: MemoryAccessScope, source) -> datetime | None:
    from memory.service import source_body_is_available, source_is_available

    if source.occurred_at is not None:
        return _utc(source.occurred_at) if await source_is_available(db, access, source) else None
    if (source.source_kind != "user_statement" or not source.session_id
            or not source.message_id or not source.part_id
            or not await source_body_is_available(db, access, source)):
        return None
    message = await db.scalar(select(Message).where(
        Message.id == source.message_id, Message.user_id == access.user_id,
        Message.session_id == source.session_id, Message.role == "user"))
    part = await db.scalar(select(Part).where(
        Part.id == source.part_id, Part.user_id == access.user_id,
        Part.message_id == source.message_id, Part.session_id == source.session_id,
        Part.type == "text"))
    if (message is None or message.agent == "compaction" or part is None
            or (part.data or {}).get("synthetic") or (part.data or {}).get("ignored")):
        return None
    events = list((await db.scalars(select(AgentEvent).where(
        AgentEvent.session_id == source.session_id, AgentEvent.user_id == access.user_id,
        or_(AgentEvent.kind == "surface.seed", AgentEvent.kind == "surface.model_exclusion",
            and_(AgentEvent.kind == "message.created", AgentEvent.message_id == source.message_id),
            and_(AgentEvent.part_id == source.part_id, AgentEvent.kind.in_(("part.created", "part.updated")))),
    ).order_by(AgentEvent.sequence))).all())
    from session.agent_event_log import model_excluded_message_ids
    if source.message_id in model_excluded_message_ids(events):
        return None
    revisions = [event for event in events if event.part_id == source.part_id
                 and event.kind in {"part.created", "part.updated"}]
    latest_revision = max((int(event.sequence) for event in revisions), default=1)
    if latest_revision != source.source_revision:
        return None
    if revisions:
        canonical_part = (revisions[-1].payload or {}).get("part") or {}
    else:
        canonical_part = next((item for event in events if event.kind == "surface.seed"
                               for seeded in (event.payload or {}).get("surface", {}).get("messages", [])
                               if seeded.get("id") == source.message_id
                               for item in seeded.get("parts", []) if item.get("id") == source.part_id), {})
    data = canonical_part.get("data") if isinstance(canonical_part.get("data"), Mapping) else canonical_part
    expected_hash = (source.source_metadata or {}).get("full_content_hash") or source.content_hash
    if (canonical_part.get("id") != source.part_id or canonical_part.get("message_id") != source.message_id
            or canonical_part.get("session_id") != source.session_id
            or data.get("synthetic") or data.get("ignored")
            or sha256(str(data.get("text", "")).encode()).hexdigest() != expected_hash):
        return None
    return canonical_user_occurrence(events, source.message_id, end_sequence=latest_revision,
                                     session_id=source.session_id)
