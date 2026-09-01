"""Stable canonical Agent-event ranges used by compaction and Session forks.

These helpers freeze an Event-projected Surface, cite its exact immutable
prefix, and revalidate that prefix while the Session row is locked before a
semantic replacement or fork commits. SQL transcript rows are read models,
never the source of a compaction/fork decision.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from models.message import MessageWithParts
from session.agent_event_log import (
    AgentEventProjectionError,
    RunFence,
    append_agent_event_locked,
    append_message_events_locked,
    append_part_event_locked,
    ensure_model_seed_locked,
    ensure_surface_seed_locked,
    model_excluded_message_ids,
    prepare_agent_event_write,
    project_agent_events,
    project_model_agent_events,
)


class StableEventRangeError(ValueError):
    """No complete, projectable range exists for the requested operation."""


class StableEventRangeDriftError(StableEventRangeError):
    """The cited range no longer projects the Surface that a caller froze."""


class SummaryNotCompactError(StableEventRangeError):
    """A provider output is not smaller than the content it would replace."""


@dataclass(frozen=True)
class StableEventRange:
    """One immutable Event prefix plus an ordered, complete Surface subset."""

    session_id: str
    start_sequence: int
    end_sequence: int
    canonical_digest: str
    covered_message_ids: tuple[str, ...]
    # Public Message states in the exact order supplied to the semantic
    # operation.  They are detached JSON snapshots, never live ORM objects.
    surface_messages: tuple[dict[str, Any], ...]

    def as_provenance(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "canonical_digest": self.canonical_digest,
            "covered_message_ids": list(self.covered_message_ids),
        }

    def messages(self) -> list[MessageWithParts]:
        return [_surface_message_to_model(item) for item in self.surface_messages]


@dataclass(frozen=True)
class CompactionRange:
    source: StableEventRange
    tail_start_id: str | None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _event_state(event: AgentEvent) -> dict[str, Any]:
    """The immutable fields whose exact prefix is cited by a range digest."""
    return {
        "sequence": int(event.sequence),
        "event_key": event.event_key,
        "kind": event.kind,
        "run_id": event.run_id,
        "generation": event.generation,
        "turn_id": event.turn_id,
        "step_id": event.step_id,
        "message_id": event.message_id,
        "part_id": event.part_id,
        "tool_call_id": event.tool_call_id,
        "payload": deepcopy(event.payload),
    }


def _range_digest(
    *,
    session_id: str,
    start_sequence: int,
    end_sequence: int,
    events: Sequence[AgentEvent],
    messages: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "session_id": session_id,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "events": [_event_state(event) for event in events],
        "covered_messages": [_json_copy(message) for message in messages],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _part_data(part: Mapping[str, Any]) -> dict[str, Any]:
    data = part.get("data")
    return dict(data) if isinstance(data, Mapping) else dict(part)


def _surface_message_to_model(state: Mapping[str, Any]) -> MessageWithParts:
    value = deepcopy(dict(state))
    value["parts"] = [
        _part_data(part)
        for part in value.get("parts") or []
        if isinstance(part, Mapping)
    ]
    return MessageWithParts.model_validate(value)


def _role(message: MessageWithParts) -> str:
    return message.role if isinstance(message.role, str) else message.role.value


def _part_balanced(message: MessageWithParts) -> bool:
    starts: dict[int, int] = {}
    finishes: dict[int, int] = {}
    for raw in message.parts or []:
        part = raw if isinstance(raw, dict) else raw.model_dump()
        part_type = part.get("type")
        if part_type == "step-start":
            step = int(part.get("step") or 0)
            starts[step] = starts.get(step, 0) + 1
        elif part_type == "step-finish":
            step = int(part.get("step") or 0)
            finishes[step] = finishes.get(step, 0) + 1
        elif part_type == "tool":
            status = getattr(part.get("status"), "value", part.get("status"))
            if status in {"pending", "running"}:
                return False
    return starts == finishes


def _terminal(message: MessageWithParts) -> bool:
    if message.error:
        return True
    return bool(
        message.finish
        and message.finish not in {"tool_calls", "tool-calls", "compact"}
    )


def _seeded_message_ids(events: Sequence[AgentEvent]) -> set[str]:
    if not events or events[0].kind != "surface.seed":
        return set()
    surface = (events[0].payload or {}).get("surface")
    if not isinstance(surface, Mapping):
        return set()
    return {
        str(message.get("id"))
        for message in surface.get("messages") or []
        if isinstance(message, Mapping) and message.get("id")
    }


def _closed_turn_boundaries(
    messages: Sequence[MessageWithParts],
    events: Sequence[AgentEvent],
) -> tuple[int, ...]:
    """Return exclusive message indexes ending on complete turn boundaries.

    A turn contains every user message materialized across one Inbox logical
    generation and every interleaved Assistant step that answers them.
    Intermediate assistant messages may finish
    with ``tool_calls``; the last one must be terminal, every step marker must
    balance, and no ToolPart may remain pending/running. Modern (non-seeded)
    turns additionally require their immutable ``turn.finished`` marker.
    """
    seeded = _seeded_message_ids(events)
    excluded_message_ids = model_excluded_message_ids(events)
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
    finished: set[tuple[str, int, str, str]] = set()
    for event in events:
        if event.kind != "turn.finished":
            continue
        run_identity = (str(event.run_id or ""), int(event.generation or 0))
        finished.add((
            *run_identity,
            canonical_turn_by_run.get(run_identity, str(event.turn_id or "")),
            str(event.message_id or ""),
        ))
    message_turn: dict[str, tuple[str, int, str]] = {}
    ambiguous: set[str] = set()
    for event in events:
        if (
            event.kind not in {"message.created", "message.updated"}
            or not event.message_id
            or not event.run_id
            or event.generation is None
            or not event.turn_id
        ):
            continue
        run_identity = (str(event.run_id), int(event.generation))
        key = (
            *run_identity,
            canonical_turn_by_run.get(run_identity, str(event.turn_id)),
        )
        message_id = str(event.message_id)
        previous = message_turn.get(message_id)
        if previous is not None and previous != key:
            ambiguous.add(message_id)
        else:
            message_turn[message_id] = key
    for message_id in ambiguous:
        message_turn.pop(message_id, None)

    def valid_group(
        group: Sequence[MessageWithParts],
        identity: tuple[str, int, str] | None,
    ) -> bool:
        semantic_group = [
            message
            for message in group
            if message.id not in excluded_message_ids
        ]
        # Fully failed attachment turns remain closed public audit records;
        # mixed turns are validated only against the Messages that could
        # actually reach the model.
        if semantic_group:
            group = semantic_group
        users: list[MessageWithParts] = []
        assistants: list[MessageWithParts] = []
        for message in group:
            if _role(message) == "user":
                users.append(message)
                continue
            if _role(message) != "assistant" or not users:
                return False
            if (
                message.parent_id != users[-1].id
                or not _part_balanced(message)
                or (message.finish is None and not message.error)
            ):
                return False
            assistants.append(message)
        if (
            not users
            or not assistants
            or _role(group[-1]) != "assistant"
            or assistants[-1].parent_id != users[-1].id
            or not _terminal(assistants[-1])
        ):
            return False
        final = assistants[-1]
        if identity is not None:
            return (*identity, final.id) in finished
        boundary_user = users[-1]
        if (
            any(user.id not in seeded for user in users)
            or final.id not in seeded
        ):
            return ("", 0, boundary_user.id, final.id) in finished
        return True

    result: list[int] = []
    index = 0
    while index < len(messages):
        if _role(messages[index]) != "user":
            break
        logical = message_turn.get(messages[index].id)
        if logical is not None:
            end = index + 1
            while (
                end < len(messages)
                and message_turn.get(messages[end].id) == logical
            ):
                end += 1
            if not valid_group(messages[index:end], logical):
                break
            result.append(end)
            index = end
            continue

        # Unfenced/seeded legacy history has no run identity. Preserve the
        # deterministic consecutive-user compatibility rule for that prefix.
        user_end = index + 1
        while user_end < len(messages) and _role(messages[user_end]) == "user":
            user_end += 1
        end = user_end
        while end < len(messages) and _role(messages[end]) != "user":
            end += 1
        group = messages[index:end]
        # Task/direct triggers are durably accepted before Driver reservation,
        # so their User event can be unfenced while the answering Assistant
        # and turn.finished carry the exact run identity. Infer only the one
        # identity whose logical turn id is this User id; the ordinary parent
        # and terminal checks in valid_group still fail closed across turns.
        inferred = {
            identity
            for message in group[1:]
            if (identity := message_turn.get(message.id)) is not None
        }
        logical = None
        if len(inferred) == 1:
            candidate = next(iter(inferred))
            if candidate[2] == messages[index].id:
                logical = candidate
        if not valid_group(group, logical):
            break
        result.append(end)
        index = end
    return tuple(result)


async def _events_and_surface_locked(
    db: AsyncSession,
    session_row: Session,
) -> tuple[list[AgentEvent], list[dict[str, Any]]]:
    events = list((await db.execute(
        select(AgentEvent).where(
            AgentEvent.session_id == session_row.id,
            AgentEvent.user_id == session_row.user_id,
        ).order_by(AgentEvent.sequence)
    )).scalars().all())
    if not events:
        raise StableEventRangeError("Session has no Agent events after seeding")
    sequences = [int(event.sequence) for event in events]
    if sequences != list(range(1, len(events) + 1)):
        raise StableEventRangeError("Agent event sequence is not contiguous")
    try:
        surface = project_agent_events(events)
    except AgentEventProjectionError as exc:
        raise StableEventRangeError(str(exc)) from exc
    return events, list(surface.get("messages") or [])


def _build_range(
    session_row: Session,
    events: Sequence[AgentEvent],
    selected: Sequence[Mapping[str, Any]],
) -> StableEventRange:
    if not events or not selected:
        raise StableEventRangeError("A stable Event range must cover at least one message")
    ids = tuple(str(message.get("id") or "") for message in selected)
    if any(not message_id for message_id in ids) or len(set(ids)) != len(ids):
        raise StableEventRangeError("Stable Event range Message ids are invalid")
    start = 1
    terminal_id = ids[-1]
    terminal = next(
        (
            event for event in reversed(events)
            if event.kind == "turn.finished"
            and str(event.message_id or "") == terminal_id
        ),
        None,
    )
    if terminal is not None:
        end = int(terminal.sequence)
        # A semantic close may append canonical records after turn.finished
        # (notably surface.replacement for compaction). They still belong to
        # the closed turn. Stop immediately before the next turn starts so an
        # already-open parent turn never enters fork provenance.
        for event in events[end:]:
            if event.kind == "turn.started":
                break
            end = int(event.sequence)
    else:
        # A legacy surface seed is one immutable event containing an already
        # closed transcript. It cannot be split further, but later canonical
        # events (including an open current turn) must never enter its cited
        # prefix. New histories always use the turn.finished path above.
        seeded = _seeded_message_ids(events)
        if not set(ids).issubset(seeded):
            raise StableEventRangeError(
                "Stable Event range has no terminal turn.finished marker"
            )
        end = 0
        for event in events:
            if event.kind not in {
                "surface.seed",
                "surface.model_seed",
                "surface.model_import",
                "surface.replacement",
                "surface.model_exclusion",
                "session.forked",
            }:
                break
            end = int(event.sequence)
        if end <= 0:
            raise StableEventRangeError("Stable Event range has no seed prefix")
    prefix = events[:end]
    if not prefix or int(prefix[-1].sequence) != end:
        raise StableEventRangeError("Stable Event range prefix is not contiguous")
    clean = tuple(_json_copy(message) for message in selected)
    digest = _range_digest(
        session_id=session_row.id,
        start_sequence=start,
        end_sequence=end,
        events=prefix,
        messages=clean,
    )
    return StableEventRange(
        session_id=session_row.id,
        start_sequence=start,
        end_sequence=end,
        canonical_digest=digest,
        covered_message_ids=ids,
        surface_messages=clean,
    )


async def freeze_compaction_event_range(
    session_id: str,
    *,
    user_id: str,
    compaction_user_id: str,
    requested_tail_start_id: str | None,
    run_fence: RunFence | None,
) -> CompactionRange:
    """Freeze the exact complete-turn context a summarizer is allowed to see."""
    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        await ensure_surface_seed_locked(db, session_row)
        await ensure_model_seed_locked(db, session_row)
        events, raw_surface = await _events_and_surface_locked(db, session_row)

        context = list(project_model_agent_events(events).messages)
        boundary_index = next(
            (index for index, message in enumerate(context)
             if message.id == compaction_user_id),
            -1,
        )
        if boundary_index < 0:
            raise StableEventRangeError("Compaction request is absent from Event projection")
        source_context = context[:boundary_index]
        desired_end = len(source_context)
        if requested_tail_start_id:
            tail_index = next(
                (index for index, message in enumerate(source_context)
                 if message.id == requested_tail_start_id),
                -1,
            )
            if tail_index >= 0:
                desired_end = tail_index

        boundaries = _closed_turn_boundaries(source_context, events)
        eligible = [boundary for boundary in boundaries if boundary <= desired_end]
        if not eligible:
            raise StableEventRangeError(
                "Compaction has no complete closed turn before its preserved tail"
            )
        stable_end = eligible[-1]
        selected_models = source_context[:stable_end]
        adjusted_tail = (
            source_context[stable_end].id
            if stable_end < len(source_context)
            else None
        )
        by_id = {str(item.get("id")): item for item in raw_surface}
        selected = [by_id[message.id] for message in selected_models]
        return CompactionRange(
            source=_build_range(session_row, events, selected),
            tail_start_id=adjusted_tail,
        )


async def freeze_fork_event_range(
    session_id: str,
    *,
    user_id: str,
    up_to_message_id: str | None,
) -> StableEventRange:
    """Freeze a raw Event-projected prefix ending at a complete turn."""
    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        await ensure_surface_seed_locked(db, session_row)
        await ensure_model_seed_locked(db, session_row)
        events, raw_surface = await _events_and_surface_locked(db, session_row)
        projected = [_surface_message_to_model(item) for item in raw_surface]
        boundaries = _closed_turn_boundaries(projected, events)
        if not boundaries:
            raise StableEventRangeError("Session has no complete closed turn to fork")
        if up_to_message_id is None:
            end = boundaries[-1]
        else:
            index = next(
                (index for index, message in enumerate(projected)
                 if message.id == up_to_message_id),
                -1,
            )
            if index < 0:
                raise StableEventRangeError("Fork cutoff Message does not exist")
            end = index + 1
            if end not in boundaries:
                raise StableEventRangeError(
                    "Fork cutoff must be the terminal Assistant of a complete turn"
                )
        return _build_range(session_row, events, raw_surface[:end])


async def revalidate_stable_event_range_locked(
    db: AsyncSession,
    session_row: Session,
    frozen: StableEventRange,
) -> tuple[dict[str, Any], ...]:
    """CAS-check one frozen range while its owning Session row is locked."""
    if frozen.session_id != session_row.id or frozen.start_sequence != 1:
        raise StableEventRangeDriftError("Stable Event range targets another Session")
    events, raw_surface = await _events_and_surface_locked(db, session_row)
    if len(events) < frozen.end_sequence:
        raise StableEventRangeDriftError("Stable Event range was truncated")
    prefix = events[:frozen.end_sequence]
    by_id = {str(message.get("id")): message for message in raw_surface}
    try:
        selected = tuple(by_id[message_id] for message_id in frozen.covered_message_ids)
    except KeyError as exc:
        raise StableEventRangeDriftError(
            "A Message covered by the stable Event range disappeared"
        ) from exc
    digest = _range_digest(
        session_id=session_row.id,
        start_sequence=frozen.start_sequence,
        end_sequence=frozen.end_sequence,
        events=prefix,
        messages=selected,
    )
    if digest != frozen.canonical_digest:
        raise StableEventRangeDriftError(
            "Stable Event range changed while the semantic operation was in flight"
        )
    return tuple(_json_copy(message) for message in selected)


async def finalize_compaction_replacement(
    *,
    frozen: StableEventRange,
    user_id: str,
    compaction_user_id: str,
    assistant_message_id: str,
    text_part_id: str,
    summary_text: str,
    tail_start_id: str | None,
    source_token_count: int,
    summary_token_count: int,
    model_id: str,
    usage: Mapping[str, Any] | None,
    run_fence: RunFence | None,
) -> int:
    """CAS and atomically commit summary, descriptor, and provenance event."""
    if source_token_count <= 0 or summary_token_count >= source_token_count:
        raise SummaryNotCompactError(
            "Compaction summary is not shorter than its replaced input"
        )
    replacement_id = f"compaction:{assistant_message_id}"
    async with get_db_session() as db:
        session_row = await prepare_agent_event_write(
            db,
            session_id=frozen.session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        await revalidate_stable_event_range_locked(db, session_row, frozen)

        assistant = (await db.execute(select(Message).where(
            Message.id == assistant_message_id,
            Message.session_id == frozen.session_id,
            Message.user_id == user_id,
            Message.parent_id == compaction_user_id,
            Message.role == "assistant",
        ))).scalar_one_or_none()
        boundary = (await db.execute(select(Message).where(
            Message.id == compaction_user_id,
            Message.session_id == frozen.session_id,
            Message.user_id == user_id,
            Message.role == "user",
        ))).scalar_one_or_none()
        text_part = (await db.execute(select(Part).where(
            Part.id == text_part_id,
            Part.message_id == assistant_message_id,
            Part.session_id == frozen.session_id,
            Part.user_id == user_id,
            Part.type == "text",
        ))).scalar_one_or_none()
        compaction_part = (await db.execute(select(Part).where(
            Part.message_id == compaction_user_id,
            Part.session_id == frozen.session_id,
            Part.user_id == user_id,
            Part.type == "compaction",
        ))).scalar_one_or_none()
        if not assistant or not boundary or not text_part or not compaction_part:
            raise StableEventRangeDriftError(
                "Compaction boundary rows disappeared before commit"
            )
        # SQLite's historical ``server_default='false'`` Boolean is surfaced as
        # truthy text by some drivers, so ``finish`` is the reliable settlement
        # marker. A committed replacement always sets it in this transaction.
        if assistant.finish is not None:
            raise StableEventRangeDriftError("Compaction Assistant is already settled")

        text_data = deepcopy(text_part.data or {})
        text_data["text"] = summary_text
        text_part.data = text_data
        descriptor = deepcopy(compaction_part.data or {})
        descriptor.update({
            "tail_start_id": tail_start_id,
            "source_event_start": frozen.start_sequence,
            "source_event_end": frozen.end_sequence,
            "source_event_digest": frozen.canonical_digest,
            "covered_message_ids": list(frozen.covered_message_ids),
            "replacement_id": replacement_id,
        })
        compaction_part.data = descriptor
        assistant.summary = True
        assistant.finish = "stop"
        assistant.model_id = model_id
        if usage:
            assistant.tokens = {
                "input": int(usage.get("input", 0) or 0),
                "output": int(usage.get("output", 0) or 0),
                "cache": int(usage.get("cache", 0) or 0),
                "total": int(usage.get("total", 0) or 0),
                "limit": int(usage.get("limit", 0) or 0),
                "cost": float(usage.get("cost", 0.0) or 0.0),
                "context": int(usage.get("context", 0) or 0),
            }
        await db.flush()
        await append_part_event_locked(
            db,
            session_row,
            text_part,
            assistant,
            operation="updated",
            run_fence=run_fence,
        )
        await append_part_event_locked(
            db,
            session_row,
            compaction_part,
            boundary,
            operation="updated",
            run_fence=run_fence,
        )
        await append_message_events_locked(
            db,
            session_row,
            assistant,
            operation="updated",
            run_fence=run_fence,
        )
        replacement = await append_agent_event_locked(
            db,
            session_row,
            kind="surface.replacement",
            payload={
                "replacement_id": replacement_id,
                "operation": "compaction",
                "source": frozen.as_provenance(),
                "boundary_user_message_id": compaction_user_id,
                "summary_message_id": assistant_message_id,
                "summary_part_id": text_part_id,
                "tail_start_id": tail_start_id,
                "source_token_count": source_token_count,
                "summary_token_count": summary_token_count,
                "model_id": model_id,
            },
            run_fence=run_fence,
            turn_id=compaction_user_id,
            message_id=assistant_message_id,
            part_id=text_part_id,
            idempotency_key=replacement_id,
        )
        return int(replacement.sequence)
