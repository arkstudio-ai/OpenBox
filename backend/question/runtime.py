"""Execution fencing for durable questions; no task is kept alive while waiting.

Fencing is enforced here whether trajectory recording is on or off. Writes use
the write rule (a run's detached work may outlive the run, never its turn);
new side effects (steps, requests, tools, spawns) need the live lease itself.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import event as sa_event, exists, or_, select, update
from sqlalchemy.orm import Session as SyncSession

from bus import bus
from core.log import create_logger
from db.base import get_db_session
from db.models.question import QuestionCheckpoint, SessionExecution
from session.internal_parts import begin_session_write, lock_owned_session, session_exposure_lock

log = create_logger("question.runtime")

LEASE_SECONDS = 60
#: A request may reuse this context's own lease check for this long. The loop
#: checks the lease immediately before it hands a step's request to an adapter.
VERIFIED_REUSE_SECONDS = 2.0
#: Remembered run and turn identities per process (revocations, terminal facts).
_MEMORY_LIMIT = 4096
_AFTER_COMMIT = "question_runtime_after_commit"
_PENDING_CLAIMS = "question_runtime_pending_claims"


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


@dataclass(frozen=True)
class AuxiliaryTicket:
    """Title and suggestions work: it may outlive its run, never its turn."""
    session_id: str
    user_id: str
    generation: int
    run_id: str
    purpose: str


class RunRevoked(Exception):
    """A superseded, replaced, expired or deleted run reached a fenced boundary.

    Deliberately neither a ValueError nor a TrajectoryError: ordinary handlers
    must not turn it into a tool or step failure. Raising it revokes the run in
    this process, so a handler that swallows it still stops the loop at its
    next boundary.
    """

    def __init__(self, ticket, reason: str, boundary: str):
        super().__init__(f"Run {ticket.run_id} is {reason}; {boundary} refused")
        self.ticket = ticket
        self.reason = reason
        self.boundary = boundary
        revoke(ticket.run_id, reason)


class _Recent:
    """Bounded, insertion-ordered memory of identities."""

    def __init__(self, limit: int = _MEMORY_LIMIT):
        self._items: OrderedDict[str, str] = OrderedDict()
        self._limit = limit

    def add(self, key: str, value: str = "") -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def get(self, key: str) -> str | None:
        return self._items.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self._items


current_run: ContextVar[RunTicket | None] = ContextVar("question_current_run", default=None)
auxiliary_run: ContextVar[AuxiliaryTicket | None] = ContextVar("question_auxiliary_run", default=None)
_verified: ContextVar[tuple[str, float] | None] = ContextVar("question_run_verified", default=None)
_trace_run_started: dict[str, float] = {}
_revoked = _Recent()
_terminal_runs = _Recent()
_finished_turns = _Recent()


def revoke(run_id: str, reason: str) -> None:
    """Stop a run in this process: boundaries refuse it and its loop is signalled.

    Only that run's own abort signal is set. A session-wide stop would reach a
    newer run of the session, or wait for the next run to start.
    """
    if not run_id:
        return
    _revoked.add(run_id, reason)
    from session.status import abort_run
    abort_run(run_id)


def is_revoked(run_id: str) -> bool:
    return run_id in _revoked


def write_verdict(execution, ticket) -> str | None:
    """The write rule: a run's detached writers may outlive the run, never its turn."""
    if execution is None:
        return "deleted"
    if execution.generation != ticket.generation:
        return "superseded"
    if execution.run_id is not None and execution.run_id != ticket.run_id:
        return "replaced"
    return None


def start_verdict(execution, ticket, moment: datetime) -> str | None:
    """The start rule: a new side effect needs the run's live lease."""
    if execution is None:
        return "deleted"
    if execution.generation != ticket.generation:
        return "superseded"
    if execution.run_id != ticket.run_id:
        return "replaced" if execution.run_id else "lease_lost"
    if not execution.lease_until or utc(execution.lease_until) <= moment:
        return "lease_lost"
    return None


def bound_ticket(session_id: str) -> RunTicket | AuxiliaryTicket | None:
    """The run or auxiliary identity this context acts for on one session."""
    ticket = current_run.get()
    if ticket is not None and ticket.session_id == session_id:
        return ticket
    auxiliary = auxiliary_run.get()
    if auxiliary is not None and auxiliary.session_id == session_id:
        return auxiliary
    return None


def _write_reason(execution, ticket) -> str | None:
    if isinstance(ticket, AuxiliaryTicket):
        if execution is None:
            return "deleted"
        return None if execution.generation == ticket.generation else "superseded"
    return _revoked.get(ticket.run_id) or write_verdict(execution, ticket)


def assert_current_locked(execution, session_id: str) -> None:
    """The write fence inside a transaction that holds the session row lock (0 SQL)."""
    ticket = bound_ticket(session_id)
    if ticket is None:
        return
    reason = _write_reason(execution, ticket)
    if reason is not None:
        raise RunRevoked(ticket, reason, "write")


def assert_not_revoked(boundary: str, ticket: RunTicket | None = None) -> None:
    """The free check for streaming and dispatch paths (0 SQL)."""
    ticket = ticket or current_run.get()
    if ticket is None:
        return
    reason = _revoked.get(ticket.run_id)
    if reason is not None:
        raise RunRevoked(ticket, reason, boundary)


def write_fence(ticket: RunTicket | AuxiliaryTicket):
    """The write rule as a predicate, for writes that do not lock the session row."""
    conditions = [
        SessionExecution.session_id == ticket.session_id,
        SessionExecution.user_id == ticket.user_id,
        SessionExecution.generation == ticket.generation,
    ]
    if isinstance(ticket, RunTicket):
        conditions.append(or_(SessionExecution.run_id.is_(None), SessionExecution.run_id == ticket.run_id))
    return exists().where(*conditions)


async def write_refusal(db, ticket: RunTicket | AuxiliaryTicket) -> str | None:
    """Why a predicate-fenced write matched nothing, or None when the fence holds."""
    return _write_reason(await db.get(SessionExecution, ticket.session_id), ticket)


async def _check_run(ticket: RunTicket, boundary: str, *, progress: bool) -> None:
    reason = _revoked.get(ticket.run_id)
    if reason is not None:
        raise RunRevoked(ticket, reason, boundary)
    verified = _verified.get()
    if (boundary == "request" and not progress and verified is not None
            and verified[0] == ticket.run_id
            and time.monotonic() - verified[1] <= VERIFIED_REUSE_SECONDS):
        return
    moment = now()
    async with get_db_session() as db:
        # Progress and liveness in one statement: zero rows means the lease is gone.
        result = await db.execute(update(SessionExecution).where(
            SessionExecution.session_id == ticket.session_id,
            SessionExecution.user_id == ticket.user_id,
            SessionExecution.run_id == ticket.run_id,
            SessionExecution.generation == ticket.generation,
            SessionExecution.lease_until > moment,
        ).values(run_progress=True if progress else SessionExecution.run_progress)
            .execution_options(synchronize_session=False))
        if result.rowcount == 1:
            reason = None
        else:
            execution = await db.get(SessionExecution, ticket.session_id)
            reason = start_verdict(execution, ticket, moment) or "lease_lost"
    if reason is not None:
        raise RunRevoked(ticket, reason, boundary)
    _verified.set((ticket.run_id, time.monotonic()))


async def _check_auxiliary(ticket: AuxiliaryTicket, boundary: str) -> None:
    async with get_db_session() as db:
        generation = await db.scalar(select(SessionExecution.generation).where(
            SessionExecution.session_id == ticket.session_id,
            SessionExecution.user_id == ticket.user_id,
        ))
    if generation != ticket.generation:
        raise RunRevoked(ticket, "deleted" if generation is None else "superseded", boundary)


async def assert_current(boundary: str, *, progress: bool = False) -> None:
    """Refuse a new side effect from a revoked run with at most one statement.

    A run ticket needs its live lease; ``progress`` also marks the run as past
    its first side effect. An auxiliary ticket needs only its turn.
    """
    ticket = current_run.get()
    if ticket is not None:
        await _check_run(ticket, boundary, progress=progress)
        return
    auxiliary = auxiliary_run.get()
    if auxiliary is not None:
        await _check_auxiliary(auxiliary, boundary)


async def run_auxiliary(ticket: RunTicket, purpose: str, work):
    """Await work that may outlive its run but never its turn (title, suggestions)."""
    run_token = current_run.set(None)
    auxiliary_token = auxiliary_run.set(AuxiliaryTicket(
        ticket.session_id, ticket.user_id, ticket.generation, ticket.run_id, purpose))
    try:
        return await work
    finally:
        auxiliary_run.reset(auxiliary_token)
        current_run.reset(run_token)


def _after_commit(db, action) -> None:
    """Apply in-process state only once the owning transaction commits."""
    db.sync_session.info.setdefault(_AFTER_COMMIT, []).append(action)


def _claim_once(db, memory: _Recent, key: str) -> bool:
    """True the first time a fact is claimed, counting this transaction's claims."""
    pending = db.sync_session.info.setdefault(_PENDING_CLAIMS, set())
    marker = (id(memory), key)
    if key in memory or marker in pending:
        return False
    pending.add(marker)
    _after_commit(db, lambda: memory.add(key))
    return True


@sa_event.listens_for(SyncSession, "after_commit")
def _apply_after_commit(session) -> None:
    if session.in_nested_transaction():
        return
    session.info.pop(_PENDING_CLAIMS, None)
    for action in session.info.pop(_AFTER_COMMIT, ()):
        try:
            action()
        except Exception:
            log.exception("Execution commit hook failed")


@sa_event.listens_for(SyncSession, "after_soft_rollback")
def _discard_after_rollback(session, previous_transaction) -> None:
    if previous_transaction.nested:
        return
    session.info.pop(_PENDING_CLAIMS, None)
    session.info.pop(_AFTER_COMMIT, None)


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
    from trajectory.producers import activity_context, markers
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
    # The recording markers stay (SPEC §5.6): later epochs count on from the
    # stored one, and a pause flag this start resumed is left to the next session write.
    execution.trace_context = {**markers(execution.trace_context), **context.to_dict()}
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
    # One terminal fact per run, whichever of finish, invalidation and recovery
    # commits first; a repeat under the same id could carry a different payload.
    if not _claim_once(db, _terminal_runs, ticket.run_id):
        return
    started = _trace_run_started.pop(ticket.run_id, None)
    await _record_first(record, event_type, {
        "status": status, "reason": reason,
        "duration_ms": round((time.monotonic() - started) * 1000, 3) if started is not None else None,
        "timing_source": "producer_monotonic" if started is not None else "not_recorded",
    }, context=context, db=db, event_id=f"{event_type}:{ticket.run_id}")


async def _record_first(record, event_type: str, data: dict, *, context, db, event_id: str) -> None:
    """Record a fact with a deterministic id, keeping the first one committed.

    The in-process guard forgets facts committed before a restart; the
    trajectory worker keeps the first copy of a repeated event id.
    """
    await record(event_type, data, context=context, db=db, event_id=event_id)


@asynccontextmanager
async def transaction(session_id: str, user_id: str, *, fence: bool = True):
    """Lock the owned session and its execution row.

    ``fence`` refuses a write from a revoked run bound to this session. The
    runtime's own transitions pass ``fence=False`` and apply their own rules.
    """
    async with session_exposure_lock(session_id):
        async with get_db_session() as db:
            await begin_session_write(db)
            session = await lock_for_write(db, session_id, user_id, fence=fence)
            execution = await execution_locked(db, session_id, user_id)
            if fence:
                assert_current_locked(execution, session_id)
            yield db, session, execution


async def lock_for_write(db, session_id: str, user_id: str, *, fence: bool = True):
    """Lock the owned session row for a write.

    A session its owner deleted is gone for everyone; for a run (or its title
    and suggestions work) bound to it, that is a revocation like any other, so
    the run ends as an abort rather than as a failed turn of a deleted chat.
    """
    try:
        return await lock_owned_session(db, session_id, user_id)
    except LookupError:
        ticket = bound_ticket(session_id) if fence else None
        if ticket is None or ticket.user_id != user_id:
            raise
        from db.models.session import Session
        deleted = await db.scalar(select(Session.id).where(
            Session.id == session_id, Session.user_id == user_id, Session.is_deleted.is_(True)))
        if deleted is None:
            raise
        raise RunRevoked(ticket, "deleted", "write") from None


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
    return start_verdict(execution, ticket, now()) is None


def publish_status(session_id: str, user_id: str, status: str) -> None:
    bus.publish("session.status", {"userId": user_id, "sessionId": session_id, "status": status})


async def start_run(session_id: str, user_id: str, *, expected_generation: int | None = None) -> RunTicket | None:
    async with transaction(session_id, user_id, fence=False) as (db, session, execution):
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
    try:
        await _check_run(ticket, "check", progress=progress)
    except RunRevoked:
        return False
    return True


async def _extend_lease(ticket: RunTicket) -> str | None:
    moment = now()
    async with get_db_session() as db:
        result = await db.execute(update(SessionExecution).where(
            SessionExecution.session_id == ticket.session_id,
            SessionExecution.user_id == ticket.user_id,
            SessionExecution.run_id == ticket.run_id,
            SessionExecution.generation == ticket.generation,
            SessionExecution.lease_until > moment,
        ).values(lease_until=moment + timedelta(seconds=LEASE_SECONDS))
            .execution_options(synchronize_session=False))
        if result.rowcount == 1:
            return None
        execution = await db.get(SessionExecution, ticket.session_id)
        return start_verdict(execution, ticket, moment) or "lease_lost"


async def heartbeat(ticket: RunTicket, abort: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(LEASE_SECONDS / 3)
        reason = _revoked.get(ticket.run_id)
        if reason is None:
            try:
                reason = await _extend_lease(ticket)
            except Exception:
                # Lost ownership cannot safely keep authorizing external effects.
                abort.set()
                return
        if reason is not None:
            revoke(ticket.run_id, reason)
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
                     completed: bool = False, aborted: bool = False) -> None:
    async with transaction(ticket.session_id, ticket.user_id, fence=False) as (db, session, execution):
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
        # An abort signal (user stop, shutdown, revocation) is not a completion.
        trace_status = ("cancelled" if interrupted or aborted else "failed" if failed else
                        "waiting" if status in ("waiting_input", "queued") else "completed")
        await _record_run_terminal(db, execution, ticket, status=trace_status,
                                   reason="interrupted" if interrupted else "aborted" if aborted else None)
        if completed and not interrupted and not aborted and status == "idle" and execution.trace_context:
            from trajectory import TraceContext, record
            context = TraceContext.from_dict(execution.trace_context)
            # A regenerated reply or an accepted plan completes the same turn again.
            if (context.session_id == ticket.session_id and session.kind != "cron"
                    and _claim_once(db, _finished_turns, str(context.turn_id))):
                await _record_first(record, "turn.finished", {"status": "completed"}, context=context, db=db,
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
        superseded = RunTicket(execution.session_id, execution.user_id,
                               execution.run_generation, execution.run_id)
        # A lease left from an older turn already had its terminal fact when
        # that turn was superseded; recording it again could conflict.
        if execution.run_generation == execution.generation:
            await _record_run_terminal(db, execution, superseded, status="cancelled", reason=status,
                                       event_type="run.interrupted")
        # The superseded run no longer holds the row: recovery never mistakes it
        # for a crashed run, and its late finish cannot claim the session.
        execution.run_id = None
        execution.lease_until = None
        _after_commit(db, lambda: revoke(superseded.run_id, status))
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
    async with transaction(session_id, user_id, fence=False) as (db, session, execution):
        if execution.run_id and execution.trace_context:
            from trajectory import TraceContext, record
            await record("run.cancel_requested", {"reason": "user_cancel"}, db=db,
                         context=TraceContext.from_dict(execution.trace_context))
        rows = await invalidate_locked(db, execution, "cancelled")
        session.status = "idle"
    publish_invalidated(rows)
    publish_status(session_id, user_id, "idle")


async def _recover_expired_run(session_id: str, user_id: str) -> None:
    status = None
    async with transaction(session_id, user_id, fence=False) as (db, session, execution):
        if not execution.run_id or is_live(execution):
            return
        expired = RunTicket(session_id, user_id, execution.run_generation, execution.run_id)
        if expired.generation != execution.generation:
            # A lease left behind by a superseded turn: its terminal fact and the
            # session's status belong to that invalidation. Only release the row.
            execution.run_id = None
            execution.lease_until = None
            _after_commit(db, lambda: revoke(expired.run_id, "superseded"))
            return
        if execution.run_origin == "question" and not execution.run_progress:
            execution.resume_pending = True
        status = await waiting_status(db, execution, "error")
        await _record_run_terminal(db, execution, expired, status="unknown",
                                   reason="lease_expired", event_type="run.interrupted")
        execution.run_id = None
        execution.lease_until = None
        session.status = status
        if status == "error":
            execution.resume_error = "Execution interrupted. Your answers are saved; send a message to continue."
            from notifications.events import task_finished
            await task_finished(db, session, expired, failed=True)
        resume_error = execution.resume_error
        _after_commit(db, lambda: revoke(expired.run_id, "lease_lost"))
    publish_status(session_id, user_id, status)
    if status == "error":
        bus.publish("session.error", {"userId": user_id, "sessionId": session_id,
            "error": {"code": "EXECUTION_INTERRUPTED", "message": resume_error}})


async def recover_expired_runs() -> None:
    """Recover pre-start delivery; never blindly replay an uncertain side effect."""
    async with get_db_session() as db:
        candidates = (await db.execute(select(SessionExecution.session_id, SessionExecution.user_id).where(
            SessionExecution.run_id.is_not(None), SessionExecution.lease_until < now(),
        ))).all()
    for session_id, user_id in candidates:
        try:
            await _recover_expired_run(session_id, user_id)
        except LookupError:
            continue
        except Exception:
            # One unrecoverable row must not stall recovery for every other session.
            log.exception("Execution lease recovery failed for %s", session_id)
