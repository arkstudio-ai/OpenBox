"""Explicit identity propagation and recording markers for business operations.

These helpers do not listen to the UI bus and never read trajectory tables.
Callers invoke them at the actual transaction or dispatch boundary and keep the
returned context with the job. Recording is fail-open: an identity that cannot
be resolved means the activity is not recorded, never that it fails.

``SessionExecution.trace_context`` carries the recording markers of SPEC §5.6
beside the saved identity: ``recording_epoch`` names the baseline of the
current (or, while paused, the next) recording period and ``recording_paused``
is set while recording is disabled for the owner.

Every ``recording.state`` control carries an ``epoch`` (contract 6); the worker
applies a control only when its epoch is newer than the last one it applied. A
resume carries the epoch of the period it opens and a pause the epoch just
below it, so every report of one transition names the same epoch and each
later transition a larger one. A pause is one above the stored epoch. A run
start rewrites the saved identity from ``TraceContext.to_dict()``, which drops
both markers; the clock then keeps the next pause above every earlier epoch.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SyncSession

from trajectory.config import enabled
from trajectory.context import TraceContext, current
from trajectory.emitter import emit_after_commit, emit_control
from trajectory.types import TrajectoryError, iso, now

log = logging.getLogger(__name__)

EPOCH_KEY = "recording_epoch"
PAUSED_KEY = "recording_paused"
MARKER_KEYS = frozenset({EPOCH_KEY, PAUSED_KEY})
#: Identity that describes one write rather than the saved execution.
_PER_WRITE_IDS = {"step_id": None, "request_id": None, "call_id": None, "parent_call_id": None,
                  "message_id": None, "part_id": None}
_PENDING_ONCE = "trajectory_recorded_once"
_ONCE_LIMIT = 16384


class _Recent:
    """Bounded, insertion-ordered memory of keys."""

    def __init__(self, limit: int = 4096):
        self._items: OrderedDict = OrderedDict()
        self._limit = limit

    def add(self, key) -> None:
        self._items[key] = None
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def discard(self, key) -> None:
        self._items.pop(key, None)

    def __contains__(self, key) -> bool:
        return key in self._items


#: Facts this process recorded once: recording periods (root session, epoch)
#: and asset uses (``artifact.recorded`` event ids).
_opened = _Recent(_ONCE_LIMIT)
_warned = _Recent(256)


def reset_for_tests() -> None:
    """Forget opened periods and recorded uses: tests reuse session ids across databases."""
    global _opened, _warned
    _opened, _warned = _Recent(_ONCE_LIMIT), _Recent(256)


def _not_recorded(reason: str) -> None:
    if reason not in _warned:
        _warned.add(reason)
        log.warning("Trajectory activity not recorded: %s", reason)


def identity(saved) -> dict:
    """The persisted TraceContext fields, without recording markers."""
    if not isinstance(saved, dict):
        return {}
    return {key: value for key, value in saved.items() if key not in MARKER_KEYS}


def markers(saved) -> dict:
    if not isinstance(saved, dict):
        return {}
    return {key: saved[key] for key in MARKER_KEYS if key in saved}


def saved_context(context: TraceContext | None = None) -> dict | None:
    context = context or current()
    return context.to_dict() if context and enabled(context.user_id) else None


async def session_context(db, user_id: str, session_id: str, **ids) -> TraceContext | None:
    """The owned root context of a session, or None when it cannot be recorded."""
    from trajectory.recorder import context_for_session
    try:
        return await context_for_session(db, user_id, session_id, **ids)
    except (TrajectoryError, TypeError, ValueError):
        return None


async def activity_context(db, user_id: str, session_id: str, *, saved: dict | None = None,
                           **ids) -> TraceContext | None:
    if not enabled(user_id):
        return None
    base = identity(saved)
    try:
        context = TraceContext.from_dict(base) if base else current()
    except (TypeError, ValueError):
        _not_recorded("stored activity identity is invalid")
        return None
    if context is not None:
        if context.user_id != user_id:
            _not_recorded("activity owner does not match")
            return None
        if (context.source_session_id or context.session_id) == session_id:
            if base and saved.get(PAUSED_KEY):
                await _resume_saved(db, context, saved)
            elif not base:
                await _open_first_period(db, context)
            try:
                return context.derive(**ids)
            except (TypeError, ValueError):
                return None
        if base:
            _not_recorded("stored activity source does not match")
            return None
    context = await session_context(db, user_id, session_id, **ids)
    if context is not None:
        await _open_first_period(db, context)
    return context


# Facts recorded once per process ----------------------------------------------

def _claim(db, key) -> bool:
    """True the first time a fact is recorded, counting this transaction's claims."""
    info = getattr(db, "sync_session", db).info
    pending = info.setdefault(_PENDING_ONCE, set())
    if key in _opened or key in pending:
        return False
    pending.add(key)
    return True


def _release(db, key) -> None:
    getattr(db, "sync_session", db).info.get(_PENDING_ONCE, set()).discard(key)


def claim_once(db, key) -> bool:
    """True the first time this process records the fact ``key``.

    With ``db`` the claim is kept when its outer transaction commits and
    dropped when it rolls back; without ``db`` it is kept at once.
    """
    if db is not None:
        return _claim(db, key)
    if key in _opened:
        return False
    _opened.add(key)
    return True


def release_claim(db, key) -> None:
    """The claimed fact was not enqueued, so a later use may record it."""
    if db is not None:
        _release(db, key)
    else:
        _opened.discard(key)


# Recording markers (SPEC §5.6) ------------------------------------------------

def _stored_epoch(saved) -> int | None:
    value = saved.get(EPOCH_KEY) if isinstance(saved, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _epoch(saved) -> int | None:
    """The stored epoch of a period after the first (never 0)."""
    return _stored_epoch(saved) or None


def _pause_epoch(saved) -> int:
    """One above the stored period epoch, or from the clock when a run start dropped it."""
    stored = _stored_epoch(saved)
    return stored + 1 if stored is not None else max(1, int(time.time() * 1000))


def _state(user_id: str, root_session_id: str, state: str, reason: str, epoch: int) -> dict:
    return {"type": "recording.state", "user_id": user_id, "session_id": root_session_id,
            "state": state, "epoch": epoch, "reason": reason, "at": iso(now())}


def _owns_root(base: dict, user_id: str, session_id: str) -> bool:
    return (base.get("user_id") == user_id and base.get("session_id") == session_id
            and base.get("source_session_id", session_id) == session_id)


async def _open_period(db, context: TraceContext, root_session_id: str, epoch: int, *, resumed: bool) -> None:
    """Resumed control and baseline candidate for one period, once per process."""
    key = (root_session_id, epoch)
    if not _claim(db, key):
        return
    if resumed:
        emit_control(_state(context.user_id, root_session_id, "resumed", "recording_reenabled", epoch), db=db)
    try:
        from db.models.session import Session
        from session.session import baseline_snapshot
        baseline = await baseline_snapshot(db, context, await db.get(Session, root_session_id))
    except Exception as exc:
        _release(db, key)
        log.warning("Trajectory baseline not captured error_type=%s", type(exc).__name__)
        return
    emit_after_commit(db, "baseline.captured", baseline, context=context,
                      event_id=f"evt_baseline_{root_session_id}_{epoch}")


async def mark_recording_in_tx(db, execution, *, user_id: str, session_id: str,
                               context: TraceContext | None) -> None:
    """§5.6 for a write that holds the session row lock and its execution row.

    ``context`` is the write's recorded identity, None while recording is off.
    Markers change in the caller's transaction; their facts wait for its commit.
    """
    if execution is None:
        return
    saved = execution.trace_context if isinstance(execution.trace_context, dict) else {}
    base = identity(saved)
    if context is not None:
        if context.user_id != user_id or context.session_id != session_id:
            return  # A child session's write: its root session owns the markers.
        if not base:
            await _open_period(db, context, session_id, 0, resumed=False)
            execution.trace_context = {**context.derive(**_PER_WRITE_IDS).to_dict(), EPOCH_KEY: 0}
        elif saved.get(PAUSED_KEY):
            epoch = _epoch(saved) or _pause_epoch(saved) + 1
            await _open_period(db, context, session_id, epoch, resumed=True)
            execution.trace_context = {**base, EPOCH_KEY: epoch}
        return
    if enabled(user_id):
        return  # Recording is on; this write's identity just could not be resolved.
    if base and not saved.get(PAUSED_KEY) and _owns_root(base, user_id, session_id):
        # The next period's epoch is fixed now, so every later resume of this
        # pause names the same baseline and control epoch; the pause is just below.
        epoch = _pause_epoch(saved)
        execution.trace_context = {**base, PAUSED_KEY: True, EPOCH_KEY: epoch + 1}
        emit_control(_state(user_id, session_id, "paused", "recording_disabled", epoch), db=db)


async def baseline_candidate_in_tx(db, *, user_id: str, session_id: str, context: TraceContext | None,
                                   pause: bool = False) -> None:
    """§5.6 for writers without the session row lock (fork, revert, cron); nothing is persisted.

    A root session never recorded gets a first-period baseline candidate (the
    worker keeps the first copy of an event id). ``pause`` also reports a pause
    while recording is off. Its epoch is the one a locked write gives that
    pause while the stored epoch is known; once a run start dropped it, the
    report carries 1, below every resume epoch. Resuming needs the lock: the
    next session write or run start of that session resumes.
    """
    if context is None and not pause:
        return
    if context is not None and (context.user_id != user_id or context.session_id != session_id):
        return
    from db.models.question import SessionExecution
    execution = await db.get(SessionExecution, session_id)
    saved = execution.trace_context if execution is not None and isinstance(execution.trace_context, dict) else {}
    base = identity(saved)
    if context is not None:
        if not base:
            await _open_period(db, context, session_id, 0, resumed=False)
        return
    if base and not saved.get(PAUSED_KEY) and _owns_root(base, user_id, session_id):
        emit_control(_state(user_id, session_id, "paused", "recording_disabled",
                            (_stored_epoch(saved) or 0) + 1), db=db)


async def paused_in_tx(db, context: TraceContext) -> bool:
    """True while the root session's markers report a pause that is not resumed yet (§5.6).

    Only a write that holds the session row lock resumes. Until one does, a
    writer without the lock records nothing; the resume baseline starts from
    the session as it is then. A period this process already reopened (a
    question adoption leaves the flag to the next session write) is resumed.
    """
    from db.models.question import SessionExecution
    root_session_id = context.session_id
    try:
        execution = await db.get(SessionExecution, root_session_id)
    except Exception:
        _not_recorded("recording markers are not readable")
        return True
    saved = execution.trace_context if execution is not None and isinstance(execution.trace_context, dict) else {}
    if not saved.get(PAUSED_KEY):
        return False
    epoch = _epoch(saved)
    return epoch is None or (root_session_id, epoch) not in _opened


async def _resume_saved(db, context: TraceContext, saved: dict) -> None:
    """A run or question adopting a paused identity resumes recording (§5.6).

    The run start rewrites the saved identity, which clears the flag; otherwise
    the next session write clears it, and the process memory keeps either from
    opening the same period twice.
    """
    root_session_id = context.session_id
    if db is None or context.source_session_id != root_session_id:
        return
    await _open_period(db, context, root_session_id, _epoch(saved) or _pause_epoch(saved) + 1, resumed=True)


_UNLOADED = object()


def _held_identity(db, session_id: str):
    """The saved identity of an execution row the caller already loaded, without SQL.

    ``_UNLOADED`` when this transaction does not hold the row (or its value).
    """
    try:
        from sqlalchemy import inspect as instance_state
        from sqlalchemy.orm.util import identity_key
        from db.models.question import SessionExecution
        session = getattr(db, "sync_session", db)
        execution = session.identity_map.get(identity_key(SessionExecution, session_id))
        # An expired value would need a load; a row this transaction inserted
        # without an identity reads as None without one.
        if execution is None or "trace_context" in instance_state(execution).expired_attributes:
            return _UNLOADED
        return identity(execution.trace_context)
    except Exception:
        return _UNLOADED


async def _open_first_period(db, context: TraceContext) -> None:
    """§5.6 first recorded activity outside a locked session write (a run start, a question adoption).

    Those callers hold the root session's execution row and save its identity
    right after; an execution without a saved identity opens period 0 here,
    with the history that precedes this activity. Other callers do not hold
    the row, so they add no query and leave the period to the session writes.
    """
    root_session_id = context.session_id
    if db is None or context.source_session_id != root_session_id:
        return
    if _held_identity(db, root_session_id) != {}:
        return
    await _open_period(db, context, root_session_id, 0, resumed=False)


@sa_event.listens_for(SyncSession, "after_commit")
def _remember_opened(session) -> None:
    if session.in_nested_transaction():
        return
    for key in session.info.pop(_PENDING_ONCE, ()):
        _opened.add(key)


@sa_event.listens_for(SyncSession, "after_soft_rollback")
def _forget_rolled_back(session, previous_transaction) -> None:
    # A rollback that ends at a SAVEPOINT keeps the outer transaction's claims.
    boundary = previous_transaction
    while boundary is not None and not boundary.nested and boundary.parent is not None:
        boundary = boundary.parent
    if boundary is not None and boundary.nested:
        return
    session.info.pop(_PENDING_ONCE, None)


@sa_event.listens_for(SyncSession, "after_transaction_end")
def _forget_uncommitted(session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_PENDING_ONCE, None)
