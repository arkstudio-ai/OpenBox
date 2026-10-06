"""Frozen full-replay projectors: the oracle for session.agent_event_log.EventFold.

These are the projectors as they were before the incremental fold (V2 P3b),
copied verbatim except for imports. Tests replay the same events through both
and require identical public Surfaces, model Surfaces, digests and turn maps.
Do not change this file to make a test pass.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
import hashlib
import re
from typing import Any

from models.message import MessageWithParts
from session.agent_event_log import (
    EVENT_SCHEMA_VERSION,
    SURFACE_SCHEMA_VERSION,
    AgentEventProjectionError,
    CanonicalModelSurface,
    ProviderReplayRecord,
    _apply_replacement_projection,
    _canonical_bytes,
    _event_value,
    _immutable_event_state,
    _json_copy,
    _model_exclusion_ids,
    _validate_model_seed,
    _validate_tool_identity,
    model_excluded_message_ids,
)


def _oracle_validate_provider_replay(value: Any, *, session_message_ids: set[str]) -> dict[str, Any]:
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



def oracle_public(
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
            "turn.recovered",
            "surface.replacement",
            "surface.model_seed",
            "surface.model_import",
            "provider.transcript",
            "model.requested",
            "resource.runtime_requested",
            "resource.observed",
            "attachment.delivery_failed",
            "session.forked",
            "inbox.accepted",
            "inbox.claimed",
            "inbox.canceled",
            "inbox.settled",
            "assistant.submission.accepted",
            "assistant.isolation.created",
            "assistant.task.linked",
            "assistant.queue.claimed",
            "assistant.continuation.queued",
            "assistant.continuation.resolved",
            "assistant.continuation.sources_projected",
            "assistant.budget.started",
            "assistant.budget.request",
            "assistant.budget.tool",
            "assistant.task.changed",
            "assistant.submission.applied",
            "assistant.control.changed",
            "assistant.request.changed",
            "assistant.permission.asked",
            "assistant.permission.closed",
            "assistant.request.displayed",
            "assistant.control.accepted",
            "assistant.control.observed",
            "assistant.control.resumed",
            "assistant.control.blocked",
            "assistant.execution.completed",
            "assistant.result.accepted",
            "assistant.result.processed",
            "assistant.report.failed",
            "assistant.report.sources_read",
            "assistant.report.sources_projected",
            "assistant.result.sources_read",
            "assistant.history.read",
            "assistant.business.read",
            "assistant.message.committed",
            "assistant.context.consumed",
            "assistant.decision.proposed",
            "assistant.decision.recorded",
            "assistant.compaction.requested",
            "assistant.compaction.consumed",
            "assistant.compaction.committed",
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



def _oracle_model_surface(
    ordered: Sequence[AgentEvent | Mapping[str, Any]],
    public: Mapping[str, Any],
) -> CanonicalModelSurface:
    """Add private model state to this exact, already validated public prefix."""
    # Model replay and the prefix digest need the same normalized payloads.
    # Normalize once, keeping the existing canonical JSON/hash format. These
    # values belong to this projection only; no cache outlives the read lock.
    immutable_events = [_immutable_event_state(event) for event in ordered]
    excluded_message_ids = model_excluded_message_ids(ordered)
    message_ids = {
        str(message.get("id")) for message in public.get("messages") or []
    }
    known_message_ids = set(message_ids)
    for event in immutable_events:
        payload = event["payload"]
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

    for event in immutable_events:
        kind = str(event["kind"])
        payload = event["payload"]
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
                item = _oracle_validate_provider_replay(
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
            item = _oracle_validate_provider_replay(
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

    # Replacement/exclusion only select and reorder whole messages. Detach
    # their contents once below, after discarded messages have been removed.
    model_states = list(public.get("messages") or [])
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
            # ``value`` already owns a deep copy of this Part's full data.
            data = dict(part.get("data") or {})
            identity = identities.get(part_id)
            if identity is not None and identity.get("provider_dialect") == "nested":
                # Nested invocations are durable UI/audit evidence. The model
                # requested only their parent batch, whose result includes
                # their output; never invent additional provider tool calls.
                continue
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
        # The public projector above has already checked the complete
        # sequence. Hash exactly the same immutable states as the standalone
        # event_prefix_digest(), without normalizing every payload again.
        event_digest=hashlib.sha256(_canonical_bytes(immutable_events)).hexdigest(),
        replacement_generation=len(replacements),
        messages=tuple(models),
        provider_replay=replay,
    )



def oracle_model(events):
    ordered = list(events)
    return _oracle_model_surface(ordered, oracle_public(ordered))


def oracle_turn_maps(events):
    """The two event loops of _repair_projected_tail_locked, verbatim."""
    started_by_message: dict[str, tuple[str, int, str]] = {}
    canonical_turn_by_run: dict[tuple[str, int], str] = {}
    for event in events:
        if (
            _event_value(event, "kind") == "turn.started"
            and _event_value(event, "run_id")
            and _event_value(event, "generation") is not None
            and _event_value(event, "turn_id")
        ):
            canonical_turn_by_run.setdefault(
                (str(_event_value(event, "run_id")), int(_event_value(event, "generation"))),
                str(_event_value(event, "turn_id")),
            )
    message_turn: dict[str, tuple[str, int, str]] = {}
    for event in events:
        if not _event_value(event, "run_id") or _event_value(event, "generation") is None:
            continue
        run_identity = (str(_event_value(event, "run_id")), int(_event_value(event, "generation")))
        logical = (
            *run_identity,
            canonical_turn_by_run.get(
                run_identity,
                str(_event_value(event, "turn_id") or _event_value(event, "message_id") or ""),
            ),
        )
        kind = _event_value(event, "kind")
        message_id = _event_value(event, "message_id")
        if kind == "turn.started" and message_id:
            started_by_message[str(message_id)] = logical
        if kind in {"message.created", "message.updated", "turn.started"} and message_id:
            existing = message_turn.get(str(message_id))
            if existing is not None and existing[2] != logical[2]:
                raise AgentEventProjectionError(
                    f"Message {message_id} crosses logical Agent turns: {existing[2]} -> {logical[2]}"
                )
            message_turn[str(message_id)] = logical
    return canonical_turn_by_run, message_turn, started_by_message


def _outcome(projector):
    try:
        return "ok", projector()
    except Exception as exc:  # compared by kind, not message
        return "error", type(exc).__name__


async def verify_fold_against_oracle(db, session_row, fold) -> None:
    """Every fold a test loads must equal a full replay of the same prefix."""
    from sqlalchemy import select
    from db.models.agent_event import AgentEvent
    from session.agent_event_log import event_prefix_digest
    # Sorted here, not in SQL, so tests that count history scans ignore this check.
    rows = sorted((await db.execute(select(AgentEvent).where(
        AgentEvent.session_id == session_row.id, AgentEvent.user_id == session_row.user_id,
        AgentEvent.sequence <= fold.sequence))).scalars().all(), key=lambda row: row.sequence)
    fields = ("id", "session_id", "sequence", "event_key", "kind", "run_id", "generation", "turn_id",
              "step_id", "message_id", "part_id", "tool_call_id", "payload")
    events = [{name: getattr(row, name) for name in fields} for row in rows]
    assert len(events) == fold.sequence, (len(events), fold.sequence)
    assert fold.public_surface() == oracle_public(events)
    assert fold.digest == event_prefix_digest(events)
    expected, actual = _outcome(lambda: oracle_model(events)), _outcome(fold.model_surface)
    assert expected[0] == actual[0], (expected, actual)
    if expected[0] == "ok":
        assert actual[1] == expected[1]
    expected, actual = _outcome(lambda: oracle_turn_maps(events)), _outcome(fold.turn_index)
    assert expected == actual, (expected, actual)
