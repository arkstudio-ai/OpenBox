"""Execution fencing for durable questions; no task is kept alive while waiting."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from bus import bus
from db.base import get_db_session
from db.models.question import QuestionCheckpoint, SessionExecution
from session.internal_parts import begin_session_write, lock_owned_session, session_exposure_lock

LEASE_SECONDS = 60


def now() -> datetime:
    return datetime.now(timezone.utc)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass(frozen=True)
class RunTicket:
    session_id: str
    user_id: str
    generation: int
    run_id: str


current_run: ContextVar[RunTicket | None] = ContextVar("question_current_run", default=None)
_trace_run_started: dict[str, float] = {}


async def get_run_trace(ticket: RunTicket):
    """Load the accepted task identity; never infer it from the latest message."""
    from trajectory import TraceContext, enabled
    if not enabled(ticket.user_id):
        return None
    async with get_db_session() as db:
        execution = await db.get(SessionExecution, ticket.session_id)
        if (execution is None or execution.user_id != ticket.user_id
                or not execution.trace_context):
            return None
        context = TraceContext.from_dict(execution.trace_context)
        if context.run_id != ticket.run_id or context.generation != ticket.generation:
            return None
        return context


async def _record_run_started(db, session, execution, ticket, *, resumed: bool):
    from trajectory import current, record
    from trajectory.producers import activity_context
    inherited = current()
    saved = execution.trace_context
    if inherited and inherited.user_id == ticket.user_id and (
            inherited.source_session_id or inherited.session_id) == ticket.session_id:
        saved = inherited.to_dict()
    context = await activity_context(db, ticket.user_id, ticket.session_id, saved=saved)
    if context is None:
        return
    old_run = context.run_id
    context = context.derive(run_id=ticket.run_id, generation=ticket.generation,
                             step_id=None, request_id=None, call_id=None)
    if not context.turn_id:
        # Existing pre-rollout pending questions have an explicit continuation
        # origin. Their new execution gets a task identity without fake history.
        context = context.derive(turn_id=uuid4().hex)
        await record("turn.started", {"origin": "continuation" if resumed else "execution"},
                     context=context, db=db)
    if not context.agent_id:
        context = context.derive(agent_id=uuid4().hex)
    execution.trace_context = context.to_dict()
    await record("run.started", {
        "origin": execution.run_origin, "generation": ticket.generation,
        "resume_of_run_id": old_run if resumed else None,
        "resume_reason": "question" if resumed else None,
        "timing_source": "producer_monotonic",
    }, context=context, db=db, event_id=f"run_start:{ticket.run_id}")
    _trace_run_started[ticket.run_id] = time.monotonic()


async def _record_run_terminal(db, execution, ticket, *, status: str, reason: str | None = None,
                               event_type: str = "run.finished"):
    from trajectory import TraceContext, enabled, record
    if not enabled(ticket.user_id) or not execution.trace_context:
        return
    context = TraceContext.from_dict(execution.trace_context)
    if context.run_id != ticket.run_id:
        return
    started = _trace_run_started.pop(ticket.run_id, None)
    await record(event_type, {
        "status": status, "reason": reason,
        "duration_ms": round((time.monotonic() - started) * 1000, 3) if started is not None else None,
        "timing_source": "producer_monotonic" if started is not None else "not_recorded",
    }, context=context, db=db, event_id=f"{event_type}:{ticket.run_id}")


@asynccontextmanager
async def transaction(session_id: str, user_id: str):
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            session = await lock_owned_session(db, session_id, user_id)
            execution = await execution_locked(db, session_id, user_id)
            yield db, session, execution


async def execution_locked(db, session_id: str, user_id: str) -> SessionExecution:
    """Caller must already hold the owned session row's write lock."""
    execution = await db.get(SessionExecution, session_id)
    if execution is None:
        execution = SessionExecution(session_id=session_id, user_id=user_id, generation=0,
                                     run_progress=False, resume_pending=False, updated_at=now())
        db.add(execution)
        await db.flush()
    return execution


def is_live(execution: SessionExecution) -> bool:
    return bool(execution.run_id and execution.lease_until and utc(execution.lease_until) > now())


def owns(execution: SessionExecution, ticket: RunTicket) -> bool:
    return (execution.run_id == ticket.run_id and execution.generation == ticket.generation
            and is_live(execution))


def publish_status(session_id: str, user_id: str, status: str) -> None:
    bus.publish("session.status", {"userId": user_id, "sessionId": session_id, "status": status})


async def start_run(session_id: str, user_id: str, *, expected_generation: int | None = None) -> RunTicket | None:
    async with transaction(session_id, user_id) as (db, session, execution):
        # A reconnect or duplicate prompt delivery is not an answer. Only a
        # committed replacement message advances the generation past an ask.
        pending = await db.scalar(select(QuestionCheckpoint.id).where(
            QuestionCheckpoint.session_id == session_id,
            QuestionCheckpoint.generation == execution.generation,
            QuestionCheckpoint.status == "pending",
        ).limit(1))
        if pending or (expected_generation is None and execution.resume_pending):
            return None
        if expected_generation is not None:
            if execution.generation != expected_generation or not execution.resume_pending:
                return None
        if is_live(execution) and execution.run_generation == execution.generation:
            return None
        if expected_generation is not None:
            # Answers are accepted even at capacity. Only activation consumes
            # a slot, with a user-row lock serializing continuation workers.
            from sqlalchemy import func
            from core.config import get_config
            from db.models.session import Session
            from db.models.user import User
            await db.execute(select(User.id).where(User.id == user_id).with_for_update())
            busy = await db.scalar(select(func.count()).select_from(Session).where(
                Session.user_id == user_id, Session.is_deleted == False,  # noqa: E712
                Session.status.in_(("busy", "compacting")), Session.id != session_id,
            ))
            if busy >= get_config().max_concurrent_agents:
                session.status = "queued"
                execution.next_attempt_at = now() + timedelta(seconds=5)
                return None
        ticket = RunTicket(session_id, user_id, execution.generation, uuid4().hex)
        execution.run_id = ticket.run_id
        execution.run_generation = ticket.generation
        execution.run_origin = "question" if expected_generation is not None else "prompt"
        execution.run_progress = False
        execution.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        execution.resume_pending = False
        execution.resume_error = None
        execution.next_attempt_at = None
        execution.updated_at = now()
        session.status = "busy"
        await _record_run_started(db, session, execution, ticket,
                                  resumed=expected_generation is not None)
    publish_status(session_id, user_id, "busy")
    return ticket


async def still_current(ticket: RunTicket | None = None, *, progress: bool = False) -> bool:
    ticket = ticket or current_run.get()
    if ticket is None:
        return True
    async with transaction(ticket.session_id, ticket.user_id) as (_, _, execution):
        if not owns(execution, ticket):
            return False
        if progress:
            execution.run_progress = True
        return True


async def heartbeat(ticket: RunTicket, abort: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(LEASE_SECONDS / 3)
        try:
            async with transaction(ticket.session_id, ticket.user_id) as (_, _, execution):
                if not owns(execution, ticket):
                    abort.set()
                    return
                execution.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        except Exception:
            # Lost ownership cannot safely keep authorizing external effects.
            abort.set()
            return


async def waiting_status(db, execution: SessionExecution, fallback: str = "idle") -> str:
    pending = await db.scalar(select(QuestionCheckpoint.id).where(
        QuestionCheckpoint.session_id == execution.session_id,
        QuestionCheckpoint.generation == execution.generation,
        QuestionCheckpoint.status == "pending",
    ).limit(1))
    if pending:
        return "waiting_input"
    return "queued" if execution.resume_pending else fallback


async def finish_run(ticket: RunTicket, *, failed: bool = False, interrupted: bool = False,
                     completed: bool = False) -> None:
    async with transaction(ticket.session_id, ticket.user_id) as (db, session, execution):
        if not owns(execution, ticket):
            return
        if interrupted and execution.run_origin == "question" and not execution.run_progress:
            # Shutdown between claiming an answer and making any progress is
            # safe to redeliver. Once work has started, never replay blindly.
            execution.resume_pending = True
        elif interrupted and execution.run_progress:
            failed = True
            execution.resume_error = "Execution interrupted. Your answers are saved; send a message to continue."
        execution.run_id = None
        execution.lease_until = None
        status = await waiting_status(db, execution, "error" if failed else "idle")
        session.status = status
        execution.updated_at = now()
        if status == "error" or (status == "idle" and completed and not interrupted):
            from notifications.events import task_finished
            await task_finished(db, session, ticket, failed=status == "error")
        trace_status = ("cancelled" if interrupted else "failed" if failed else
                        "waiting" if status in ("waiting_input", "queued") else "completed")
        await _record_run_terminal(db, execution, ticket, status=trace_status,
                                   reason="interrupted" if interrupted else None)
        if completed and not interrupted and status == "idle" and execution.trace_context:
            from trajectory import TraceContext, record
            context = TraceContext.from_dict(execution.trace_context)
            if context.session_id == ticket.session_id and session.kind != "cron":
                await record("turn.finished", {"status": "completed"}, context=context, db=db,
                             event_id=f"turn_finish:{context.turn_id}")
    publish_status(ticket.session_id, ticket.user_id, status)


async def invalidate_locked(db, execution: SessionExecution, status: str = "superseded") -> list:
    """Called in the SAME transaction that accepts a replacement message."""
    from db.models.part import Part
    rows = (await db.scalars(select(QuestionCheckpoint).where(
        QuestionCheckpoint.session_id == execution.session_id,
        QuestionCheckpoint.user_id == execution.user_id,
        QuestionCheckpoint.generation == execution.generation,
        QuestionCheckpoint.applied == False,  # noqa: E712
        QuestionCheckpoint.status.in_(("pending", "answered", "rejected")),
    ))).all()
    for row in rows:
        from question.question import checkpoint_context, record_checkpoint
        # Adopt a legacy pending question before changing its persisted state.
        await checkpoint_context(db, row, execution)
        from notifications.events import cancel_event
        await cancel_event(db, row.user_id, f"question:{row.id}")
        row.status = status
        row.updated_at = now()
        await record_checkpoint(db, row, execution, "question.cancelled",
                                {"status": "cancelled", "reason": status})
        part = await db.get(Part, row.part_id) if row.part_id else None
        if part is not None and part.user_id == execution.user_id:
            part.data = {**part.data, "status": "error", "error": status,
                         "title": "Question superseded" if status == "superseded" else "Question cancelled",
                         "metadata": {**(part.data.get("metadata") or {}), "question_status": status}}
    if execution.run_id:
        ticket = RunTicket(execution.session_id, execution.user_id,
                           execution.run_generation, execution.run_id)
        await _record_run_terminal(db, execution, ticket, status="cancelled", reason=status,
                                   event_type="run.interrupted")
    execution.generation += 1
    execution.resume_pending = False
    execution.resume_error = None
    execution.next_attempt_at = None
    execution.updated_at = now()
    return list(rows)


def publish_invalidated(rows: list) -> None:
    for row in rows:
        bus.publish("question.cancelled", {
            "userId": row.user_id, "session_id": row.session_id,
            "id": row.id, "request_id": row.id, "status": row.status,
        })


async def cancel_session(session_id: str, user_id: str) -> None:
    async with transaction(session_id, user_id) as (db, session, execution):
        if execution.run_id and execution.trace_context:
            from trajectory import TraceContext, record
            await record("run.cancel_requested", {"reason": "user_cancel"}, db=db,
                         context=TraceContext.from_dict(execution.trace_context))
        rows = await invalidate_locked(db, execution, "cancelled")
        session.status = "idle"
    publish_invalidated(rows)
    publish_status(session_id, user_id, "idle")


async def recover_expired_runs() -> None:
    """Recover pre-start delivery; never blindly replay an uncertain side effect."""
    async with get_db_session() as db:
        candidates = (await db.execute(select(SessionExecution.session_id, SessionExecution.user_id).where(
            SessionExecution.run_id.is_not(None), SessionExecution.lease_until < now(),
        ))).all()
    for session_id, user_id in candidates:
        try:
            async with transaction(session_id, user_id) as (db, session, execution):
                if not execution.run_id or is_live(execution):
                    continue
                if (execution.run_generation == execution.generation
                        and execution.run_origin == "question" and not execution.run_progress):
                    execution.resume_pending = True
                status = await waiting_status(db, execution, "error")
                expired_ticket = RunTicket(session_id, user_id, execution.run_generation, execution.run_id)
                await _record_run_terminal(db, execution, expired_ticket, status="unknown",
                                           reason="lease_expired", event_type="run.interrupted")
                execution.run_id = None
                execution.lease_until = None
                session.status = status
                if status == "error":
                    execution.resume_error = "Execution interrupted. Your answers are saved; send a message to continue."
                    if expired_ticket.generation == execution.generation:
                        from notifications.events import task_finished
                        await task_finished(db, session, expired_ticket, failed=True)
            publish_status(session_id, user_id, status)
            if status == "error":
                bus.publish("session.error", {"userId": user_id, "sessionId": session_id,
                    "error": {"code": "EXECUTION_INTERRUPTED", "message": execution.resume_error}})
        except LookupError:
            continue
