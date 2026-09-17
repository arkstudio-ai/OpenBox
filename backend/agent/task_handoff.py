"""Durable delivery for one-shot Task subagents.

This module deliberately does not resume a parent Agent loop.  Once a parent
generation entered ``running``, recovery cannot prove whether another model
or sibling-tool boundary was crossed.  Instead, a child result is written to
an outbox and a fresh maintenance generation may project it into the exact
parent ToolPart while closing the interrupted tail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select

from bus import bus
from bus.events import PART_UPDATED
from core.identifier import ascending
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.message import Message
from db.models.part import Part, public_part_data
from db.models.session import Session
from db.models.task_handoff import TaskHandoff


TASK_CHILD_INTERRUPTED = "task_child_outcome_unknown"
TASK_CHILD_NO_TERMINAL_RESULT = "task_child_no_terminal_result"


class TaskHandoffFenceError(RuntimeError):
    """A descriptor, tenant, or run-generation identity did not match."""


@dataclass(frozen=True, slots=True)
class TaskHandoffRef:
    id: str
    user_id: str
    parent_session_id: str
    parent_part_id: str
    child_session_id: str
    child_trigger_message_id: str


@dataclass(frozen=True, slots=True)
class TaskHandoffApplyResult:
    rejoined: int
    updates: tuple[dict, ...]
    message_ids: tuple[str, ...]


def _database_now(db):
    if db.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return func.current_timestamp()


def _ref(row: TaskHandoff) -> TaskHandoffRef:
    return TaskHandoffRef(
        id=row.id,
        user_id=row.user_id,
        parent_session_id=row.parent_session_id,
        parent_part_id=row.parent_part_id,
        child_session_id=row.child_session_id,
        child_trigger_message_id=row.child_trigger_message_id,
    )


def _is_task_part(part: Part) -> bool:
    data = dict(part.data or {})
    canonical = part.canonical_tool_id
    if canonical is not None:
        return canonical == "task"
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


async def create_task_handoff(
    *,
    user_id: str,
    parent_session_id: str,
    parent_message_id: str,
    parent_part_id: str,
    parent_run_id: str,
    parent_generation: int,
    child_session_id: str,
    child_trigger_message_id: str,
    task_title: str,
    subagent_type: str,
) -> TaskHandoffRef:
    """Atomically persist the descriptor and parent child-pointer.

    The active parent generation is checked in the same transaction.  A
    stale tool body therefore cannot attach a new child after takeover.
    """
    if not parent_run_id or parent_generation <= 0:
        raise TaskHandoffFenceError("task handoff requires a parent run fence")

    now = datetime.now(timezone.utc)
    published: dict | None = None
    async with get_db_session() as db:
        from session.internal_parts import begin_session_write

        await begin_session_write(db)
        # Session -> Driver is the global lock order used by Agent transcript
        # writes. It also serializes the per-Session Agent event sequence.
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
            raise TaskHandoffFenceError("parent Session is no longer live")
        live_parent = (
            await db.execute(
                select(AgentDriverState)
                .where(
                    AgentDriverState.session_id == parent_session_id,
                    AgentDriverState.user_id == user_id,
                    AgentDriverState.run_id == parent_run_id,
                    AgentDriverState.generation == parent_generation,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > _database_now(db),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if live_parent is None:
            raise TaskHandoffFenceError("parent task generation is no longer live")

        child = (
            await db.execute(
                select(Session).where(
                    Session.id == child_session_id,
                    Session.user_id == user_id,
                    Session.parent_id == parent_session_id,
                    Session.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if parent is None or child is None or child.project_id != parent.project_id:
            raise TaskHandoffFenceError("parent/child tenant or project mismatch")

        parent_message = (
            await db.execute(
                select(Message).where(
                    Message.id == parent_message_id,
                    Message.session_id == parent_session_id,
                    Message.user_id == user_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one_or_none()
        trigger = (
            await db.execute(
                select(Message).where(
                    Message.id == child_trigger_message_id,
                    Message.session_id == child_session_id,
                    Message.user_id == user_id,
                    Message.role == "user",
                )
            )
        ).scalar_one_or_none()
        part = (
            await db.execute(
                select(Part)
                .where(
                    Part.id == parent_part_id,
                    Part.message_id == parent_message_id,
                    Part.session_id == parent_session_id,
                    Part.user_id == user_id,
                    Part.type == "tool",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if parent_message is None or trigger is None or part is None or not _is_task_part(part):
            raise TaskHandoffFenceError("task handoff transcript identity mismatch")

        from session.agent_event_log import ensure_surface_seed_locked

        await ensure_surface_seed_locked(db, parent)

        existing = (
            await db.execute(
                select(TaskHandoff).where(
                    (TaskHandoff.parent_part_id == parent_part_id)
                    | (TaskHandoff.child_session_id == child_session_id)
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            expected = (
                existing.user_id == user_id
                and existing.parent_session_id == parent_session_id
                and existing.parent_message_id == parent_message_id
                and existing.parent_part_id == parent_part_id
                and existing.parent_run_id == parent_run_id
                and existing.parent_generation == parent_generation
                and existing.child_session_id == child_session_id
                and existing.child_trigger_message_id == child_trigger_message_id
            )
            if not expected:
                raise TaskHandoffFenceError("task handoff uniqueness collision")
            return _ref(existing)

        part_data = dict(part.data or {})
        status = getattr(part_data.get("status"), "value", part_data.get("status"))
        if status not in {"pending", "running"}:
            raise TaskHandoffFenceError("parent Task part is already terminal")
        metadata = dict(part_data.get("metadata") or {})
        handoff_id = ascending("handoff")
        metadata.update({
            "child_session_id": child_session_id,
            "subagent_type": subagent_type,
            "task_handoff_id": handoff_id,
        })
        part_data["metadata"] = metadata
        part.data = public_part_data(part_data)
        published = dict(part.data)

        row = TaskHandoff(
            id=handoff_id,
            user_id=user_id,
            parent_session_id=parent_session_id,
            parent_message_id=parent_message_id,
            parent_part_id=parent_part_id,
            parent_run_id=parent_run_id,
            parent_generation=parent_generation,
            child_session_id=child_session_id,
            child_trigger_message_id=child_trigger_message_id,
            state="accepted",
            task_title=task_title[:255],
            subagent_type=subagent_type,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        await db.flush()
        from session.agent_event_log import append_part_event_locked

        await append_part_event_locked(
            db,
            parent,
            part,
            parent_message,
            operation="updated",
            run_fence=(parent_session_id, parent_run_id, parent_generation),
        )
        ref = _ref(row)

    if published is not None:
        _publish_part(user_id, published)
    return ref


async def _parent_tail_is_eligible(
    db,
    row: TaskHandoff,
    *,
    allow_delivered: bool = False,
) -> bool:
    newest = (
        await db.execute(
            select(Message.id)
            .where(
                Message.session_id == row.parent_session_id,
                Message.user_id == row.user_id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if newest != row.parent_message_id:
        return False
    part = (
        await db.execute(
            select(Part).where(
                Part.id == row.parent_part_id,
                Part.message_id == row.parent_message_id,
                Part.session_id == row.parent_session_id,
                Part.user_id == row.user_id,
                Part.type == "tool",
            )
        )
    ).scalar_one_or_none()
    if part is None or not _is_task_part(part):
        return False
    data = dict(part.data or {})
    status = getattr(
        data.get("status"),
        "value",
        data.get("status"),
    )
    if status in {"pending", "running", "error"}:
        return True
    metadata = dict(data.get("metadata") or {})
    return bool(
        allow_delivered
        and status == "completed"
        and metadata.get("child_session_id") == row.child_session_id
        and metadata.get("task_handoff_id") == row.id
        and metadata.get("task_outbox_completed") is True
    )


async def bind_task_handoff_child(
    handoff_id: str,
    lease: Any,
    *,
    mode: str = "normal",
) -> bool:
    """Fence the descriptor to the exact child generation about to run.

    ``normal`` requires the originating parent generation to remain live.
    ``unbound_recovery`` requires no live parent generation and rechecks the
    transcript tail after child reservation, closing the startup TOCTOU.
    ``takeover`` is only for a descriptor already bound to durable accepted
    child work whose expired ``reserved`` generation is being replaced.
    """
    if mode not in {"normal", "unbound_recovery", "takeover"}:
        raise ValueError(f"invalid Task handoff bind mode: {mode}")
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(TaskHandoff)
                .where(
                    TaskHandoff.id == handoff_id,
                    TaskHandoff.user_id == lease.user_id,
                    TaskHandoff.child_session_id == lease.session_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.state != "accepted":
            return False

        live_parent = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == row.parent_session_id,
                    AgentDriverState.user_id == row.user_id,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > _database_now(db),
                )
            )
        ).scalar_one_or_none()
        if mode == "normal":
            if (
                live_parent is None
                or live_parent.run_id != row.parent_run_id
                or live_parent.generation != row.parent_generation
            ):
                raise TaskHandoffFenceError(
                    "parent generation was lost before child bind"
                )
            if row.child_generation is not None and (
                row.child_generation != lease.generation
                or row.child_run_id != lease.run_id
            ):
                raise TaskHandoffFenceError(
                    "normal Task bind cannot replace a child generation"
                )
        elif mode == "unbound_recovery":
            if live_parent is not None or not await _parent_tail_is_eligible(db, row):
                raise TaskHandoffFenceError(
                    "unbound Task recovery lost its parent/tail fence"
                )
            if row.child_generation is not None or row.child_run_id is not None:
                raise TaskHandoffFenceError("unbound Task was already reserved")
        elif row.child_generation is None or row.child_run_id is None:
            raise TaskHandoffFenceError(
                "Task takeover requires a previously bound child generation"
            )

        driver = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == row.child_session_id,
                    AgentDriverState.user_id == row.user_id,
                    AgentDriverState.run_id == lease.run_id,
                    AgentDriverState.generation == lease.generation,
                    AgentDriverState.owner_id == lease.owner_id,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.trigger_message_id == row.child_trigger_message_id,
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > _database_now(db),
                )
            )
        ).scalar_one_or_none()
        if driver is None:
            raise TaskHandoffFenceError("child generation is no longer live or bound")

        if row.child_generation is not None:
            if lease.generation < row.child_generation:
                raise TaskHandoffFenceError("stale child generation cannot replace handoff")
            if (
                lease.generation == row.child_generation
                and row.child_run_id != lease.run_id
            ):
                raise TaskHandoffFenceError("child run changed without generation takeover")

        row.child_run_id = lease.run_id
        row.child_generation = lease.generation
        row.updated_at = datetime.now(timezone.utc)
        return True


async def bind_task_handoff_for_recovered_child(
    lease: Any,
    record: Any,
) -> str | None:
    """Bind a takeover generation when the recovered Session is a Task child."""
    async with get_db_session() as db:
        handoff_id = (
            await db.execute(
                select(TaskHandoff.id).where(
                    TaskHandoff.child_session_id == lease.session_id,
                    TaskHandoff.user_id == lease.user_id,
                    TaskHandoff.state == "accepted",
                )
            )
        ).scalar_one_or_none()
    if handoff_id is None:
        return None
    # A crash can land after the child Driver bind but before the descriptor
    # bind. The expired reserved record is the durable proof needed to fill
    # that exact old identity before applying the takeover generation.
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(TaskHandoff)
                .where(TaskHandoff.id == handoff_id)
                .with_for_update()
            )
        ).scalar_one()
        if row.state != "accepted":
            raise TaskHandoffFenceError(
                "recovered Task handoff is no longer accepted"
            )
        if row.child_generation is None:
            if (
                record.phase != "reserved"
                or not record.run_id
                or record.session_id != row.child_session_id
                or record.user_id != row.user_id
                or record.trigger_message_id != row.child_trigger_message_id
                or lease.generation <= record.generation
            ):
                raise TaskHandoffFenceError("recovered child descriptor gap is invalid")
            row.child_run_id = record.run_id
            row.child_generation = record.generation
            row.updated_at = datetime.now(timezone.utc)
    bound = await bind_task_handoff_child(handoff_id, lease, mode="takeover")
    if not bound:
        raise TaskHandoffFenceError("recovered Task handoff is no longer accepted")
    return handoff_id


async def abandon_task_handoff(handoff_id: str) -> bool:
    """Suppress recovery after a start failure was handled by the live parent."""
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(TaskHandoff)
                .where(TaskHandoff.id == handoff_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.state != "accepted":
            return False
        row.state = "abandoned"
        row.updated_at = datetime.now(timezone.utc)
        return True


async def _project_result(raw: dict) -> dict:
    """Create the bounded ToolPart image stored in the outbox."""
    from tool.truncation import truncate_output

    output = str(raw.get("output") or "")
    truncated = await truncate_output(output)
    raw_metadata = raw.get("metadata")
    raw_metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    metadata: dict[str, Any] = {}
    for key in {
        "child_session_id",
        "subagent_type",
        "task_handoff_id",
        "task_outbox_completed",
        "error",
        "recovery_code",
    }:
        value = raw_metadata.get(key)
        if isinstance(value, str):
            metadata[key] = value[:256]
        elif isinstance(value, bool):
            metadata[key] = value
    metadata["truncated"] = truncated.truncated
    return {
        "title": str(raw.get("title") or "")[:255],
        "output": truncated.content,
        "metadata": metadata,
    }


async def complete_task_handoff(
    handoff_id: str,
    *,
    child_run_id: str,
    child_generation: int,
    raw_result: dict,
) -> dict:
    """Persist a child result before the parent coroutine may return it."""
    projected = await _project_result(raw_result)
    payload = {"projected": projected}
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(TaskHandoff)
                .where(TaskHandoff.id == handoff_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise TaskHandoffFenceError("task handoff disappeared before completion")
        if (
            row.child_run_id != child_run_id
            or row.child_generation != child_generation
        ):
            raise TaskHandoffFenceError("stale child result rejected by handoff fence")
        if row.state in {"completed", "rejoined"}:
            if not isinstance(row.result_payload, dict):
                raise TaskHandoffFenceError("completed handoff has no result payload")
            # The full child answer stays in its transcript.  An idempotent
            # caller receives only the bounded projection already accepted by
            # the outbox, never an unbounded duplicate database copy.
            return dict(row.result_payload.get("projected") or {})
        if row.state != "accepted":
            raise TaskHandoffFenceError(f"cannot complete {row.state} handoff")

        child = (
            await db.execute(
                select(Session).where(
                    Session.id == row.child_session_id,
                    Session.user_id == row.user_id,
                    Session.parent_id == row.parent_session_id,
                )
            )
        ).scalar_one_or_none()
        trigger = (
            await db.execute(
                select(Message.id).where(
                    Message.id == row.child_trigger_message_id,
                    Message.session_id == row.child_session_id,
                    Message.user_id == row.user_id,
                    Message.role == "user",
                )
            )
        ).scalar_one_or_none()
        driver = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == row.child_session_id,
                    AgentDriverState.user_id == row.user_id,
                    AgentDriverState.generation == child_generation,
                )
            )
        ).scalar_one_or_none()
        if child is None or trigger is None or driver is None:
            raise TaskHandoffFenceError("child completion tenant identity mismatch")
        if driver.run_id not in {None, child_run_id}:
            raise TaskHandoffFenceError("child driver run no longer matches result")

        row.result_payload = payload
        row.state = "completed"
        row.completed_at = now
        row.updated_at = now
    return raw_result


async def _raw_result_from_transcript(
    handoff_id: str,
    *,
    recovery_code: str | None = None,
) -> dict:
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(TaskHandoff).where(TaskHandoff.id == handoff_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise TaskHandoffFenceError("task handoff not found")

        text = ""
        if recovery_code is None:
            child_status = (
                await db.execute(
                    select(Session.status).where(
                        Session.id == row.child_session_id,
                        Session.user_id == row.user_id,
                        Session.parent_id == row.parent_session_id,
                        Session.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            terminal_message_id = None
            if child_status == "idle":
                terminal_message_id = (
                    await db.execute(
                        select(Message.id)
                        .where(
                            Message.session_id == row.child_session_id,
                            Message.user_id == row.user_id,
                            Message.role == "assistant",
                            Message.parent_id == row.child_trigger_message_id,
                            Message.finish == "stop",
                        )
                        .order_by(Message.created_at.desc(), Message.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if terminal_message_id is None:
                recovery_code = TASK_CHILD_NO_TERMINAL_RESULT
            else:
                parts = list(
                    (
                        await db.execute(
                            select(Part)
                            .where(
                                Part.message_id == terminal_message_id,
                                Part.session_id == row.child_session_id,
                                Part.user_id == row.user_id,
                                Part.type == "text",
                            )
                            .order_by(Part.created_at.desc(), Part.id.desc())
                        )
                    ).scalars().all()
                )
                for part in parts:
                    data = dict(part.data or {})
                    if data.get("text"):
                        text = str(data["text"])
                        break

        metadata = {
            "child_session_id": row.child_session_id,
            "subagent_type": row.subagent_type,
            "task_handoff_id": row.id,
            "task_outbox_completed": True,
        }
        if recovery_code is not None:
            if recovery_code == TASK_CHILD_INTERRUPTED:
                text = (
                    "Subagent execution was interrupted after it may have started. "
                    "Its external outcome is unknown and was not replayed."
                )
            else:
                text = (
                    "Subagent did not produce a terminal successful answer. "
                    "It may have failed or been cancelled; partial output was "
                    "not reported as success."
                )
            metadata.update({"error": True, "recovery_code": recovery_code})

        output = (
            "\n".join([
                f"task_id: {row.child_session_id}",
                "",
                "<task_result>",
                text,
                "</task_result>",
            ])
            if text
            else "Task completed with no text output."
        )
        return {
            "title": row.task_title,
            "output": output,
            "metadata": metadata,
        }


async def complete_task_handoff_from_transcript(
    handoff_id: str,
    *,
    child_run_id: str,
    child_generation: int,
    recovery_code: str | None = None,
) -> dict:
    raw = await _raw_result_from_transcript(
        handoff_id,
        recovery_code=recovery_code,
    )
    return await complete_task_handoff(
        handoff_id,
        child_run_id=child_run_id,
        child_generation=child_generation,
        raw_result=raw,
    )


async def complete_task_handoff_for_child(
    child_session_id: str,
    *,
    child_run_id: str,
    child_generation: int,
) -> str | None:
    """Complete the accepted descriptor owned by one finished child Session."""
    async with get_db_session() as db:
        handoff_id = (
            await db.execute(
                select(TaskHandoff.id).where(
                    TaskHandoff.child_session_id == child_session_id,
                    TaskHandoff.child_run_id == child_run_id,
                    TaskHandoff.child_generation == child_generation,
                    TaskHandoff.state == "accepted",
                )
            )
        ).scalar_one_or_none()
    if handoff_id is None:
        return None
    await complete_task_handoff_from_transcript(
        handoff_id,
        child_run_id=child_run_id,
        child_generation=child_generation,
    )
    return handoff_id


async def recover_task_handoff_outboxes(records: Iterable[Any]) -> int:
    """Materialize outboxes stranded by expired child generations.

    ``reserved`` remains replayable and is left to normal prompt recovery.
    ``running``/``finalizing`` is converted to an explicit unknown-outcome
    result, never re-executed.
    """
    completed = 0
    reserved_children: set[str] = set()
    for record in records:
        if record.phase == "reserved":
            reserved_children.add(record.session_id)
            continue
        if record.phase not in {"running", "finalizing"} or not record.run_id:
            continue
        async with get_db_session() as db:
            row = (
                await db.execute(
                    select(TaskHandoff)
                    .where(
                        TaskHandoff.child_session_id == record.session_id,
                        TaskHandoff.user_id == record.user_id,
                        TaskHandoff.state == "accepted",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            if row.child_generation is None:
                if row.child_trigger_message_id != record.trigger_message_id:
                    continue
                row.child_run_id = record.run_id
                row.child_generation = record.generation
                row.updated_at = datetime.now(timezone.utc)
            elif (
                row.child_run_id != record.run_id
                or row.child_generation != record.generation
            ):
                continue
            handoff_id = row.id
        await complete_task_handoff_from_transcript(
            handoff_id,
            child_run_id=record.run_id,
            child_generation=record.generation,
            recovery_code=TASK_CHILD_INTERRUPTED,
        )
        completed += 1

    # A child may have finished and released its driver just before the parent
    # worker died, leaving no expired child record.  Its transcript plus the
    # exact stored generation is enough to finish the outbox.
    async with get_db_session() as db:
        candidates = list(
            (
                await db.execute(
                    select(TaskHandoff.id)
                    .join(
                        AgentDriverState,
                        AgentDriverState.session_id == TaskHandoff.child_session_id,
                    )
                    .where(
                        TaskHandoff.state == "accepted",
                        TaskHandoff.child_run_id.is_not(None),
                        TaskHandoff.child_generation.is_not(None),
                        AgentDriverState.phase == "idle",
                        AgentDriverState.generation == TaskHandoff.child_generation,
                    )
                )
            ).scalars().all()
        )
    for handoff_id in candidates:
        async with get_db_session() as db:
            row = (
                await db.execute(
                    select(TaskHandoff).where(TaskHandoff.id == handoff_id)
                )
            ).scalar_one_or_none()
            if row is None or row.child_session_id in reserved_children:
                continue
            run_id = row.child_run_id
            generation = row.child_generation
        if run_id is None or generation is None:
            continue
        await complete_task_handoff_from_transcript(
            handoff_id,
            child_run_id=run_id,
            child_generation=generation,
        )
        completed += 1
    return completed


async def apply_completed_task_handoffs_locked(
    db,
    *,
    parent_session_id: str,
    user_id: str,
    maintenance_run_id: str,
    maintenance_generation: int,
) -> TaskHandoffApplyResult:
    """Project completed outboxes while a maintenance generation is held.

    A mutation is allowed only while the original parent message is still the
    transcript tail.  If a later message exists, changing an earlier tool
    result could invalidate a model answer that already consumed it.
    """
    rows = list(
        (
            await db.execute(
                select(TaskHandoff)
                .where(
                    TaskHandoff.parent_session_id == parent_session_id,
                    TaskHandoff.user_id == user_id,
                    TaskHandoff.state == "completed",
                )
                .with_for_update()
            )
        ).scalars().all()
    )
    if not rows:
        return TaskHandoffApplyResult(rejoined=0, updates=(), message_ids=())

    newest_message_id = (
        await db.execute(
            select(Message.id)
            .where(
                Message.session_id == parent_session_id,
                Message.user_id == user_id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    updates: list[dict] = []
    message_ids: list[str] = []
    rejoined = 0
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
        raise TaskHandoffFenceError("parent Session disappeared during rejoin")
    for row in rows:
        if row.parent_generation >= maintenance_generation:
            continue
        part = (
            await db.execute(
                select(Part)
                .where(
                    Part.id == row.parent_part_id,
                    Part.message_id == row.parent_message_id,
                    Part.session_id == row.parent_session_id,
                    Part.user_id == row.user_id,
                    Part.type == "tool",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if part is None or not _is_task_part(part):
            continue
        payload = row.result_payload if isinstance(row.result_payload, dict) else {}
        projected = payload.get("projected") if isinstance(payload, dict) else None
        if not isinstance(projected, dict):
            continue

        data = dict(part.data or {})
        metadata = dict(data.get("metadata") or {})
        status = getattr(data.get("status"), "value", data.get("status"))
        exact_delivery = (
            metadata.get("child_session_id") == row.child_session_id
            and metadata.get("task_handoff_id") == row.id
            and metadata.get("task_outbox_completed") is True
        )
        already_terminal = exact_delivery and status in {"completed", "error"}
        if not already_terminal:
            if newest_message_id != row.parent_message_id:
                continue
            if status not in {"pending", "running", "error"}:
                continue
            if metadata.get("child_session_id") not in {None, row.child_session_id}:
                continue

            result_metadata = dict(projected.get("metadata") or {})
            is_error = result_metadata.get("error") is True
            public_metadata = {
                key: value
                for key, value in result_metadata.items()
                if key in {
                    "child_session_id",
                    "subagent_type",
                    "task_handoff_id",
                    "task_outbox_completed",
                    "truncated",
                    "recovery_code",
                }
            }
            public_metadata.update({
                "child_session_id": row.child_session_id,
                "subagent_type": row.subagent_type,
                "task_handoff_id": row.id,
                "task_outbox_completed": True,
            })
            output = str(projected.get("output") or "")
            data.update({
                "status": "error" if is_error else "completed",
                "output": output,
                "title": str(projected.get("title") or row.task_title),
                "error": output if is_error else None,
                "metadata": public_metadata,
            })
            part.data = public_part_data(data)
            updates.append(dict(part.data))
            message = (
                await db.execute(
                    select(Message).where(
                        Message.id == row.parent_message_id,
                        Message.session_id == parent_session_id,
                        Message.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if message is None:
                raise TaskHandoffFenceError("parent Message disappeared during rejoin")
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

        row.state = "rejoined"
        row.rejoined_at = now
        row.updated_at = now
        rejoined += 1
        message_ids.append(row.parent_message_id)
    return TaskHandoffApplyResult(
        rejoined=rejoined,
        updates=tuple(updates),
        message_ids=tuple(message_ids),
    )


async def completed_task_parent_sessions() -> list[tuple[str, str, str]]:
    """Return only idle, current-tail parents eligible for rejoin-only repair."""
    async with get_db_session() as db:
        rows = list(
            (
                await db.execute(
                    select(TaskHandoff)
                    .where(TaskHandoff.state == "completed")
                    .order_by(TaskHandoff.created_at, TaskHandoff.id)
                )
            ).scalars().all()
        )
        candidates: dict[tuple[str, str], str] = {}
        for row in rows:
            live_parent = (
                await db.execute(
                    select(AgentDriverState.session_id).where(
                        AgentDriverState.session_id == row.parent_session_id,
                        AgentDriverState.user_id == row.user_id,
                        AgentDriverState.phase != "idle",
                        AgentDriverState.lease_expires_at.is_not(None),
                        AgentDriverState.lease_expires_at > _database_now(db),
                    )
                )
            ).scalar_one_or_none()
            if live_parent is not None or not await _parent_tail_is_eligible(
                db,
                row,
                allow_delivered=True,
            ):
                continue
            session_status = (
                await db.execute(
                    select(Session.status).where(
                        Session.id == row.parent_session_id,
                        Session.user_id == row.user_id,
                    )
                )
            ).scalar_one_or_none()
            if session_status is not None:
                candidates[(row.parent_session_id, row.user_id)] = session_status
        return [
            (session_id, user_id, status)
            for (session_id, user_id), status in candidates.items()
        ]


async def unbound_task_handoffs() -> list[TaskHandoffRef]:
    """Return accepted descriptors that crashed before child reservation."""
    async with get_db_session() as db:
        rows = list(
            (
                await db.execute(
                    select(TaskHandoff).where(
                        TaskHandoff.state == "accepted",
                        TaskHandoff.child_generation.is_(None),
                        TaskHandoff.child_run_id.is_(None),
                    )
                )
            ).scalars().all()
        )
        refs: list[TaskHandoffRef] = []
        for row in rows:
            live_parent = (
                await db.execute(
                    select(AgentDriverState.session_id).where(
                        AgentDriverState.session_id == row.parent_session_id,
                        AgentDriverState.user_id == row.user_id,
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
                        Part.id == row.parent_part_id,
                        Part.session_id == row.parent_session_id,
                        Part.user_id == row.user_id,
                    )
                )
            ).scalar_one_or_none()
            newest = (
                await db.execute(
                    select(Message.id)
                    .where(
                        Message.session_id == row.parent_session_id,
                        Message.user_id == row.user_id,
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if part is None or newest != row.parent_message_id or not _is_task_part(part):
                continue
            data = dict(part.data or {})
            status = getattr(data.get("status"), "value", data.get("status"))
            if status not in {"pending", "running", "error"}:
                continue
            refs.append(_ref(row))
        return refs


async def publish_rejoined_parts(user_id: str, updates: Iterable[dict]) -> None:
    for data in updates:
        _publish_part(user_id, data)
