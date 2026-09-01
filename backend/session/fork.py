"""Session forking from an immutable, complete-turn Agent-event prefix."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import select

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.internal_part import InternalPart
from db.models.message import Message as MessageORM
from db.models.part import Part as PartORM
from db.models.project import Project
from session.agent_event_log import (
    append_agent_event_locked,
    ensure_surface_seed_locked,
    model_excluded_message_ids,
    prepare_agent_event_write,
    project_private_event_state,
)
from session.event_range import (
    StableEventRangeError,
    freeze_fork_event_range,
    revalidate_stable_event_range_locked,
)


log = create_logger("session.fork")


def _created_at(value: Any, fallback: datetime) -> datetime:
    if not isinstance(value, str):
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _part_data(part: Mapping[str, Any]) -> dict[str, Any]:
    data = part.get("data")
    return deepcopy(dict(data)) if isinstance(data, Mapping) else deepcopy(dict(part))


def _remap_part_links(
    data: dict[str, Any],
    *,
    message_id_map: Mapping[str, str],
    part_id_map: Mapping[str, str],
) -> None:
    """Remap public cross-Part/Message links carried inside JSON data."""
    if data.get("type") == "compaction":
        tail = data.get("tail_start_id")
        if isinstance(tail, str) and tail:
            data["tail_start_id"] = message_id_map.get(tail, tail)
        covered = data.get("covered_message_ids")
        if isinstance(covered, list):
            data["covered_message_ids"] = [
                message_id_map.get(str(message_id), str(message_id))
                for message_id in covered
            ]
    relation = data.get("relation")
    if isinstance(relation, dict):
        source_part_id = relation.get("source_part_id")
        if isinstance(source_part_id, str) and source_part_id:
            relation["source_part_id"] = part_id_map.get(
                source_part_id,
                source_part_id,
            )


def _remap_replacement_for_child(
    event: AgentEvent,
    *,
    destination_session_id: str,
    message_id_map: Mapping[str, str],
    part_id_map: Mapping[str, str],
) -> dict[str, Any] | None:
    """Import one fully-contained compaction authority into a fork.

    The original ``source`` range remains byte-for-byte provenance for the
    parent Event prefix. Child ids live in explicit projection fields so the
    fork never pretends that the parent's digest describes its own Event log.
    """
    if event.kind != "surface.replacement":
        return None
    payload = deepcopy(event.payload or {})
    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Fork source replacement has no provenance")
    covered = source.get("covered_message_ids")
    boundary_id = str(payload.get("boundary_user_message_id") or "")
    summary_id = str(payload.get("summary_message_id") or "")
    tail_id = payload.get("tail_start_id")
    summary_part_id = payload.get("summary_part_id")
    if (
        not isinstance(covered, list)
        or not covered
        or boundary_id not in message_id_map
        or summary_id not in message_id_map
        or any(str(message_id) not in message_id_map for message_id in covered)
        or (tail_id is not None and str(tail_id) not in message_id_map)
        or (
            summary_part_id is not None
            and str(summary_part_id) not in part_id_map
        )
    ):
        # A fork ending before this compaction boundary must not import a
        # partial semantic replacement.
        return None
    original_replacement_id = str(
        payload.get("replacement_id") or event.event_key
    )
    payload.update({
        "replacement_id": (
            f"fork:{destination_session_id}:"
            f"{hashlib.sha256(original_replacement_id.encode()).hexdigest()[:24]}"
        ),
        "projected_covered_message_ids": [
            message_id_map[str(message_id)] for message_id in covered
        ],
        "boundary_user_message_id": message_id_map[boundary_id],
        "summary_message_id": message_id_map[summary_id],
        "tail_start_id": (
            message_id_map[str(tail_id)] if tail_id is not None else None
        ),
        "imported_from": {
            "session_id": event.session_id,
            "event_sequence": int(event.sequence),
            "event_key": event.event_key,
            "replacement_id": original_replacement_id,
        },
    })
    if summary_part_id is not None:
        payload["summary_part_id"] = part_id_map[str(summary_part_id)]
    payload.pop("version", None)
    return payload


def _remap_model_exclusion_for_child(
    event: AgentEvent,
    *,
    destination_session_id: str,
    message_id_map: Mapping[str, str],
) -> dict[str, Any] | None:
    """Carry model-only delivery exclusions across a public-audit fork."""
    if event.kind != "surface.model_exclusion":
        return None
    # Reuse the canonical validator before trusting or remapping provenance.
    model_excluded_message_ids((event,))
    payload = deepcopy(event.payload or {})
    source_ids = [str(message_id) for message_id in payload["message_ids"]]
    if any(message_id not in message_id_map for message_id in source_ids):
        raise ValueError(
            "Fork model exclusion is not contained by the stable Event range"
        )
    payload["message_ids"] = [
        message_id_map[message_id] for message_id in source_ids
    ]
    payload["imported_from"] = {
        "session_id": event.session_id,
        "event_sequence": int(event.sequence),
        "event_key": event.event_key,
    }
    payload.pop("version", None)
    return payload


async def clone_stable_event_prefix_locked(
    db,
    *,
    source_row,
    destination_row,
    frozen,
    now: datetime,
) -> dict[str, Any]:
    """CAS-copy one canonical prefix into an already-added destination.

    The caller owns the transaction and both Session lifecycles.  This lets a
    normal UI fork and a Task fork share the exact same public/model-private
    import protocol while the latter commits its descriptor, activation,
    outbox, prompt trigger, and parent pointer atomically with the seed.
    """
    source_states = await revalidate_stable_event_range_locked(
        db,
        source_row,
        frozen,
    )
    source_part_ids = [
        str(part.get("id"))
        for message in source_states
        for part in message.get("parts") or []
        if isinstance(part, Mapping) and part.get("id")
    ]
    source_events = list((await db.execute(select(AgentEvent).where(
        AgentEvent.session_id == source_row.id,
        AgentEvent.user_id == source_row.user_id,
        AgentEvent.sequence <= frozen.end_sequence,
    ).order_by(AgentEvent.sequence))).scalars().all())
    identity_by_part, provider_replay = project_private_event_state(source_events)

    message_id_map = {
        str(message["id"]): ascending("message")
        for message in source_states
    }
    part_id_map = {
        source_part_id: ascending("part")
        for source_part_id in source_part_ids
    }
    latest_created_at = now
    for ordinal, state in enumerate(source_states):
        source_message_id = str(state["id"])
        message_id = message_id_map[source_message_id]
        role = str(state.get("role") or "")
        created_at = _created_at(
            state.get("created_at"),
            now.replace(microsecond=min(999_999, now.microsecond + ordinal)),
        )
        latest_created_at = max(latest_created_at, created_at)
        parent_id = state.get("parent_id")
        row = MessageORM(
            id=message_id,
            session_id=destination_row.id,
            user_id=source_row.user_id,
            role=role,
            agent=state.get("agent"),
            model=state.get("model") if role == "user" else None,
            model_id=state.get("model") if role == "assistant" else None,
            client_message_id=state.get("client_message_id"),
            variant=state.get("variant"),
            parent_id=message_id_map.get(str(parent_id)) if parent_id else None,
            finish=state.get("finish"),
            summary=state.get("summary"),
            tokens=deepcopy(state.get("tokens")),
            error=deepcopy(state.get("error")),
            reaction=state.get("reaction"),
            format=deepcopy(state.get("format")),
            structured=deepcopy(state.get("structured")),
            created_at=created_at,
        )
        db.add(row)
        for part_state in state.get("parts") or []:
            if not isinstance(part_state, Mapping):
                continue
            source_part_id = str(part_state.get("id") or "")
            if not source_part_id:
                raise ValueError("Fork source Part has no stable id")
            part_id = part_id_map[source_part_id]
            data = _part_data(part_state)
            data.update({
                "id": part_id,
                "message_id": message_id,
                "session_id": destination_row.id,
            })
            _remap_part_links(
                data,
                message_id_map=message_id_map,
                part_id_map=part_id_map,
            )
            db.add(PartORM(
                id=part_id,
                message_id=message_id,
                session_id=destination_row.id,
                user_id=source_row.user_id,
                type=str(part_state.get("type") or data.get("type") or "text"),
                data=data,
                **identity_by_part.get(source_part_id, {}),
                created_at=_created_at(part_state.get("created_at"), created_at),
            ))
    await db.flush()

    # The seed is the complete child Surface image. The imports that follow
    # are model-private or model-only; public projection stays row-identical.
    await ensure_surface_seed_locked(db, destination_row)
    for source_event in source_events:
        replacement = _remap_replacement_for_child(
            source_event,
            destination_session_id=destination_row.id,
            message_id_map=message_id_map,
            part_id_map=part_id_map,
        )
        if replacement is not None:
            await append_agent_event_locked(
                db,
                destination_row,
                kind="surface.replacement",
                payload=replacement,
                turn_id=str(replacement["boundary_user_message_id"]),
                message_id=str(replacement["summary_message_id"]),
                part_id=(
                    str(replacement["summary_part_id"])
                    if replacement.get("summary_part_id") is not None
                    else None
                ),
                idempotency_key=(
                    f"fork-replacement:"
                    f"{destination_row.id}:{source_event.event_key}"
                ),
            )
        exclusion = _remap_model_exclusion_for_child(
            source_event,
            destination_session_id=destination_row.id,
            message_id_map=message_id_map,
        )
        if exclusion is None:
            continue
        source_message_id = str(source_event.message_id or "")
        if source_message_id and source_message_id not in message_id_map:
            raise ValueError(
                "Fork model exclusion Message association is outside the range"
            )
        source_turn_id = str(source_event.turn_id or "")
        await append_agent_event_locked(
            db,
            destination_row,
            kind="surface.model_exclusion",
            payload=exclusion,
            turn_id=message_id_map.get(source_turn_id),
            message_id=(
                message_id_map[source_message_id]
                if source_message_id
                else None
            ),
            idempotency_key=(
                f"fork-model-exclusion:"
                f"{destination_row.id}:{source_event.event_key}"
            ),
        )
    imported_provider_replay: list[dict[str, Any]] = []
    imported_origin_seq = 1
    covered_ids = set(frozen.covered_message_ids)
    for record in provider_replay:
        if record.message_id not in covered_ids:
            continue
        imported_id = ascending("ipart")
        imported_message_id = message_id_map[record.message_id]
        imported = {
            "id": imported_id,
            "message_id": imported_message_id,
            "kind": record.kind,
            "capability_key_digest": record.capability_key_digest,
            "response_chain_id": record.response_chain_id,
            "stream_seq": record.stream_seq,
            "origin_seq": imported_origin_seq,
            "dedupe_key": None,
            "data": deepcopy(record.data),
            "created_at": record.created_at,
        }
        imported_provider_replay.append(imported)
        db.add(InternalPart(
            id=imported_id,
            session_id=destination_row.id,
            message_id=imported_message_id,
            user_id=source_row.user_id,
            kind=record.kind,
            capability_key_digest=record.capability_key_digest,
            response_chain_id=record.response_chain_id,
            stream_seq=record.stream_seq,
            origin_seq=imported_origin_seq,
            dedupe_key=None,
            data=deepcopy(record.data),
            created_at=_created_at(record.created_at, now),
        ))
        imported_origin_seq += 1
    if imported_provider_replay:
        await db.flush()
        await append_agent_event_locked(
            db,
            destination_row,
            kind="surface.model_import",
            payload={
                "model": {
                    "version": 1,
                    "part_replay": {},
                    "provider_replay": imported_provider_replay,
                }
            },
            idempotency_key=f"fork-model-import:{destination_row.id}",
        )
    part_map_digest = hashlib.sha256(json.dumps(
        part_id_map,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    await append_agent_event_locked(
        db,
        destination_row,
        kind="session.forked",
        payload={
            "source": frozen.as_provenance(),
            "source_to_destination_messages": message_id_map,
            "source_part_mapping": {
                "count": len(part_id_map),
                "canonical_digest": part_map_digest,
            },
        },
        idempotency_key=f"fork-lineage:{destination_row.id}",
    )
    return {
        "message_id_map": message_id_map,
        "part_id_map": part_id_map,
        "message_count": len(source_states),
        "next_created_at": latest_created_at + timedelta(microseconds=1),
    }


async def fork_session(
    source_session_id: str,
    up_to_message_id: str | None = None,
    user_id: str = "default",
) -> dict:
    """Fork through a terminal Assistant at a completely closed turn.

    ``None`` means the latest closed turn and deliberately excludes an open
    current turn. The copied rows come from an Event projection frozen before
    destination creation, then the source is locked and CAS-revalidated before
    child Surface rows and lineage evidence are committed.
    """
    from session.session import get_session

    source = await get_session(source_session_id, user_id=user_id)
    if not source:
        raise ValueError(f"Session {source_session_id} not found")

    try:
        frozen = await freeze_fork_event_range(
            source_session_id,
            user_id=user_id,
            up_to_message_id=up_to_message_id,
        )
    except StableEventRangeError as exc:
        raise ValueError(str(exc)) from exc

    now = datetime.now(timezone.utc)
    new_session = None
    async with get_db_session() as db:
        # Source lock, CAS, project authorization, child creation, copied
        # Surface, seed and lineage are one transaction. A fault at any later
        # point therefore cannot publish or retain a visible empty child.
        source_row = await prepare_agent_event_write(
            db,
            session_id=source_session_id,
            user_id=user_id,
            run_fence=None,
        )
        project = (await db.execute(select(Project).where(
            Project.id == source_row.project_id,
            Project.user_id == user_id,
            Project.is_deleted == False,  # noqa: E712
        ).with_for_update())).scalar_one_or_none()
        if project is None:
            raise ValueError("Fork source project is missing or no longer available")

        from session.session import _new_session_record

        destination_row, new_session = _new_session_record(
            model=source_row.model or "",
            agent=source_row.agent or "build",
            variant=source_row.variant,
            title=f"Fork: {source_row.title or 'Untitled'}",
            parent_id=None,
            user_id=user_id,
            project_id=project.id,
            kind="normal",
            now=now,
        )
        db.add(destination_row)
        await db.flush()

        await clone_stable_event_prefix_locked(
            db,
            source_row=source_row,
            destination_row=destination_row,
            frozen=frozen,
            now=now,
        )

    assert new_session is not None
    from session.session import _publish_session_created

    _publish_session_created(new_session)
    log.info(
        f"Forked stable Event range {source_session_id} "
        f"1..{frozen.end_sequence} -> {new_session.id} "
        f"({len(frozen.covered_message_ids)} messages)"
    )
    return new_session
