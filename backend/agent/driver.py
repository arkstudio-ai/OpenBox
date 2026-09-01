"""Single-flight agent driver ownership with a durable lease and fence.

The local asyncio registry provides prompt responsiveness; the database row is
the authority across API workers and process restarts.  Callers reserve before
accepting work, pass the returned :class:`RunLease` into ``run_loop``, and only
the matching ``(run_id, generation, owner_id)`` may renew or release it.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
import socket
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update

from bus import bus
from bus.events import SESSION_STATUS
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.session import Session as SessionRow
from session.status import clear_abort, register_run, trigger_abort

log = create_logger("agent.driver")

LEASE_SECONDS = 60.0
HEARTBEAT_SECONDS = 10.0
ABORT_POLL_SECONDS = 0.5

_PROCESS_NONCE = uuid.uuid4().hex[:12]
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{_PROCESS_NONCE}"


class DriverBusyError(RuntimeError):
    """Another non-expired generation currently owns this conversation."""

    def __init__(self, session_id: str, run_id: str | None, generation: int):
        super().__init__(f"session {session_id} is already running")
        self.session_id = session_id
        self.run_id = run_id
        self.generation = generation


class DriverQuotaExceededError(DriverBusyError):
    """A provider-capable generation would exceed the durable cluster quota."""

    def __init__(self, session_id: str, *, user_id: str, used: int, limit: int):
        super().__init__(session_id, None, 0)
        self.user_id = user_id
        self.used = used
        self.limit = limit
        self.args = (f"concurrent Agent quota exceeded for {user_id}: {used}/{limit}",)


class LeaseLostError(RuntimeError):
    """This worker is stale and must not make another external dispatch."""


class StaleRecoveryError(RuntimeError):
    """An expired marker changed before this recovery worker could claim it."""


@dataclass(frozen=True, slots=True)
class RecoveredDriver:
    """Durable wake metadata captured before an expired owner is fenced out."""

    session_id: str
    user_id: str
    run_id: str | None
    generation: int
    phase: str
    trigger_message_id: str | None


class DriverRecoveryRequiredError(DriverBusyError):
    """An expired non-idle generation must be repaired before replacement.

    Ordinary callers are deliberately not allowed to overwrite the durable
    marker.  It is the only identity recovery can use to close an open
    Assistant step and classify pending/running tool outcomes honestly.
    """

    def __init__(self, record: RecoveredDriver):
        super().__init__(record.session_id, record.run_id, record.generation)
        self.record = record


@dataclass(slots=True)
class _LocalActivity:
    run_id: str
    generation: int
    abort: asyncio.Event
    idle: asyncio.Event = field(default_factory=asyncio.Event)


_session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_agent_quota_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Lock,
] = weakref.WeakKeyDictionary()
_activities: dict[str, _LocalActivity] = {}
_current_lease: contextvars.ContextVar[RunLease | None] = contextvars.ContextVar(
    "openbox_agent_run_lease",
    default=None,
)


def bind_current_lease(lease: RunLease):
    """Bind one generation to sandbox calls spawned by this asyncio context."""
    return _current_lease.set(lease)


def reset_current_lease(token) -> None:
    _current_lease.reset(token)


def current_run_fence() -> tuple[str, str, int] | None:
    """Return ``(session_id, run_id, generation)`` for transport fencing."""
    lease = _current_lease.get()
    if lease is None or lease._closed:
        return None
    return lease.session_id, lease.run_id, lease.generation


def current_run_transport_lease() -> tuple[str, str, int, datetime] | None:
    """Return the exact live lease receipt carried to the execution plane.

    Unlike :func:`current_run_fence`, a bound-but-closed/lost/uninitialized
    lease is an error rather than a control-plane request.  Silently dropping
    the headers in that state would let stale Agent work bypass Action Server
    fencing by masquerading as an ordinary administrative call.
    """
    lease = _current_lease.get()
    if lease is None:
        return None
    if (
        lease._closed
        or lease._lost
        or lease._transport_revoked
        or lease._lease_expires_at is None
    ):
        raise LeaseLostError(
            f"agent transport lease unavailable for {lease.session_id} "
            f"generation {lease.generation}"
        )
    return (
        lease.session_id,
        lease.run_id,
        lease.generation,
        lease._lease_expires_at,
    )


def _session_lock(session_id: str) -> asyncio.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


def _agent_quota_lock() -> asyncio.Lock:
    """Return the process-local serializer bound to this event loop.

    SQLite's ``BEGIN IMMEDIATE`` remains the cross-loop/process authority. A
    loop-local lock prevents same-worker tasks from needlessly contending for
    it without leaking an asyncio primitive across test runners or embedders
    that create more than one loop over the process lifetime.
    """
    loop = asyncio.get_running_loop()
    lock = _agent_quota_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _agent_quota_locks[loop] = lock
    return lock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _is_live(state: AgentDriverState, now: datetime) -> bool:
    expiry = _aware(state.lease_expires_at)
    return bool(
        state.phase != "idle" and state.run_id and expiry is not None and expiry > now
    )


def _database_now(db):
    """Statement-time database clock used by strict ownership fences."""
    if db.get_bind().dialect.name == "postgresql":
        # PostgreSQL now() is transaction-start time and may be stale after a
        # row-lock wait. clock_timestamp() is evaluated at the statement.
        return func.clock_timestamp()
    return func.current_timestamp()


def _database_lease_expiry(db):
    """Database expression for a full lease from statement execution."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return func.clock_timestamp() + text(f"INTERVAL '{int(LEASE_SECONDS)} seconds'")
    if dialect == "sqlite":
        return func.datetime("now", f"+{int(LEASE_SECONDS)} seconds")
    return _database_now(db) + timedelta(seconds=LEASE_SECONDS)


# Every admission and renewal takes these PostgreSQL transaction locks in the
# same order. The global key closes cross-user/global-count races; the hashed
# user key scopes the configured per-user admission decision. SQLite uses the
# process lock plus BEGIN IMMEDIATE for the equivalent desktop invariant.
_AGENT_QUOTA_GLOBAL_LOCK_KEY = 0x4F42584147454E54


def _agent_quota_user_lock_key(user_id: str) -> int:
    raw = hashlib.blake2b(
        f"openbox:agent-quota:{user_id}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(raw, byteorder="big", signed=True)


@asynccontextmanager
async def _agent_quota_database(user_id: str):
    """Open the quota-serialized transaction after its Session lock is held."""
    async with _agent_quota_lock(), get_db_session() as db:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            result = await db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _AGENT_QUOTA_GLOBAL_LOCK_KEY},
            )
            result.close()
            result = await db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _agent_quota_user_lock_key(user_id)},
            )
            result.close()
        elif dialect == "sqlite":
            # This is the first statement and stays open through the
            # count/mutation and get_db_session's commit.
            result = await db.execute(text("BEGIN IMMEDIATE"))
            result.close()
        yield db


@asynccontextmanager
async def _agent_quota_transaction(
    user_id: str,
    *,
    session_id: str | None = None,
):
    """Yield one Session→quota/DB ownership transaction.

    Release/preserve already use the local Session lock before opening a
    database transaction. Admission, takeover and deadline extension must use
    the same order: otherwise one task can hold SQLite ``BEGIN IMMEDIATE``
    while waiting for a Session lock held by a task waiting for that writer.
    PostgreSQL still takes global→user advisory transaction locks before any
    Session/Driver database rows are read or mutated.
    """
    if session_id is None:
        # Kept for the direct advisory-order protocol test. Production Driver
        # lifecycle callers always provide their exact Session identity.
        async with _agent_quota_database(user_id) as db:
            yield db
        return
    async with _session_lock(session_id), _agent_quota_database(user_id) as db:
        yield db


async def _enforce_agent_quota_locked(db, *, session_id: str, user_id: str) -> None:
    """Keep every provider-capable live generation within the hard quota."""
    from core.config import get_config

    limit = int(get_config().max_concurrent_agents)
    used_result = await db.execute(
        select(func.count(AgentDriverState.session_id)).where(
            AgentDriverState.user_id == user_id,
            AgentDriverState.phase != "idle",
            AgentDriverState.run_id.is_not(None),
            AgentDriverState.lease_expires_at.is_not(None),
            AgentDriverState.lease_expires_at > _database_now(db),
        )
    )
    used = int(used_result.scalar_one())
    used_result.close()
    if used >= limit:
        raise DriverQuotaExceededError(
            session_id,
            user_id=user_id,
            used=used,
            limit=limit,
        )


async def assert_run_fence_locked(
    db,
    *,
    session_id: str,
    user_id: str,
    run_id: str,
    generation: int,
) -> None:
    """Fence an Agent transcript write inside its database transaction.

    Session then Driver are locked in the same global order as reserve/release.
    Both rows remain locked until the caller commits its write. A takeover
    therefore happens wholly before this check (and fails it) or wholly after
    the old write commits; it cannot interleave between a normal
    ``assert_current`` transaction and the subsequent Part/Message commit.
    """
    owned_session = (
        await db.execute(
            select(SessionRow.id)
            .where(
                SessionRow.id == session_id,
                SessionRow.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if owned_session is None:
        raise LeaseLostError(f"agent transcript session fence lost for {session_id}")
    database_now = _database_now(db)
    conditions = (
        AgentDriverState.session_id == session_id,
        AgentDriverState.user_id == user_id,
        AgentDriverState.run_id == run_id,
        AgentDriverState.generation == generation,
        AgentDriverState.phase != "idle",
        AgentDriverState.lease_expires_at.is_not(None),
        AgentDriverState.lease_expires_at > database_now,
    )
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores SELECT ... FOR UPDATE. A conditional no-op UPDATE is
        # the portable write-fence: it takes the database write lock and keeps
        # it through the caller's Part/Message commit. A concurrent takeover
        # waits, then re-evaluates its own predicate after this transaction.
        matched = (
            await db.execute(
                update(AgentDriverState)
                .where(*conditions)
                .values(generation=AgentDriverState.generation)
            )
        ).rowcount == 1
    else:
        matched = (
            await db.execute(
                select(AgentDriverState.session_id).where(*conditions).with_for_update()
            )
        ).scalar_one_or_none() is not None
    if not matched:
        raise LeaseLostError(
            f"agent transcript fence lost for {session_id} generation {generation}"
        )


async def bind_trigger_message_locked(
    db,
    *,
    session_id: str,
    user_id: str,
    run_id: str,
    generation: int,
    message_id: str,
) -> None:
    """Bind an accepted User Message in its own transcript transaction.

    The caller must already have acquired :func:`assert_run_fence_locked` in
    this transaction.  Updating the same exact generation here makes the
    Message/Event append and durable wake pointer commit or roll back together.
    """
    database_now = _database_now(db)
    result = await db.execute(
        update(AgentDriverState)
        .where(
            AgentDriverState.session_id == session_id,
            AgentDriverState.user_id == user_id,
            AgentDriverState.run_id == run_id,
            AgentDriverState.generation == generation,
            AgentDriverState.phase != "idle",
            AgentDriverState.lease_expires_at.is_not(None),
            AgentDriverState.lease_expires_at > database_now,
        )
        .values(trigger_message_id=message_id, updated_at=database_now)
        .execution_options(synchronize_session=False)
    )
    matched = bool(result.rowcount)
    result.close()
    if not matched:
        raise LeaseLostError(f"agent lease lost before accepting message {message_id}")


@dataclass(slots=True)
class RunLease:
    session_id: str
    user_id: str
    run_id: str
    generation: int
    owner_id: str
    abort: asyncio.Event
    _monitor: asyncio.Task | None = field(default=None, repr=False)
    _monitor_stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _closed: bool = field(default=False, repr=False)
    _lost: bool = field(default=False, repr=False)
    _transport_revoked: bool = field(default=False, repr=False)
    _lease_expires_at: datetime | None = field(default=None, repr=False)
    _settle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
    )

    def start_monitor(self) -> None:
        """Start lease renewal before slow attachment/sandbox preflight work."""
        if self._monitor is None and not self._closed:
            self._monitor_stop.clear()
            self._monitor = asyncio.create_task(
                self._monitor_loop(),
                name=f"agent-lease:{self.session_id}:{self.generation}",
            )

    async def _monitor_loop(self) -> None:
        # ``reserve_*`` has just committed a full lease.  Renewing again on
        # the very first scheduler tick needlessly contends with the caller's
        # immediate Message/Part transaction (and a StaticPool SQLite backend
        # can only service one transaction at a time).  Start at the ordinary
        # heartbeat boundary; the initial TTL already covers this interval.
        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
        try:
            while not self._closed and not self._monitor_stop.is_set():
                if time.monotonic() >= next_heartbeat:
                    if not await self.renew():
                        self._lost = True
                        self.abort.set()
                        return
                    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                if await self.abort_was_requested():
                    self.abort.set()
                try:
                    await asyncio.wait_for(
                        self._monitor_stop.wait(),
                        timeout=ABORT_POLL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            # Losing the ownership monitor is unsafe.  Stop at the next loop
            # boundary rather than continuing an un-fenced external action.
            log.exception(
                "Agent lease monitor failed session=%s generation=%s",
                self.session_id,
                self.generation,
            )
            self._lost = True
            self.abort.set()

    async def stop_monitor(self) -> None:
        """Quiesce renewal without cancelling an in-flight database call."""
        monitor = self._monitor
        self._monitor = None
        if monitor is None or monitor is asyncio.current_task():
            return
        self._monitor_stop.set()
        try:
            await monitor
        except asyncio.CancelledError:
            pass

    async def renew(self) -> bool:
        """Extend this live generation only; an expired lease stays expired."""
        # Renewal participates in the same global→user quota serialization as
        # admission. Otherwise a claim can count this row expired while an
        # uncommitted renewal later makes it live again, transiently exceeding
        # the cluster quota.
        async with _agent_quota_transaction(
            self.user_id,
            session_id=self.session_id,
        ) as db:
            session_result = await db.execute(
                select(SessionRow.id)
                .where(
                    SessionRow.id == self.session_id,
                    SessionRow.user_id == self.user_id,
                )
                .with_for_update()
            )
            session_exists = session_result.scalar_one_or_none() is not None
            session_result.close()
            if not session_exists:
                return False
            database_now = _database_now(db)
            result = await db.execute(
                update(AgentDriverState)
                .where(
                    AgentDriverState.session_id == self.session_id,
                    AgentDriverState.user_id == self.user_id,
                    AgentDriverState.run_id == self.run_id,
                    AgentDriverState.generation == self.generation,
                    AgentDriverState.owner_id == self.owner_id,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > database_now,
                )
                .values(
                    lease_expires_at=_database_lease_expiry(db),
                    updated_at=database_now,
                )
                .returning(AgentDriverState.lease_expires_at)
                .execution_options(synchronize_session=False)
            )
            renewed_until = result.scalar_one_or_none()
            result.close()
            if renewed_until is None:
                return False
            self._lease_expires_at = _aware(renewed_until)
            return True

    async def abort_was_requested(self) -> bool:
        async with get_db_session() as db:
            database_now = _database_now(db)
            result = await db.execute(
                select(
                    AgentDriverState.run_id,
                    AgentDriverState.generation,
                    AgentDriverState.owner_id,
                    AgentDriverState.abort_requested_at,
                    AgentDriverState.phase,
                ).where(
                    AgentDriverState.session_id == self.session_id,
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > database_now,
                )
            )
            row = result.one_or_none()
            result.close()
        if row is None:
            self._lost = True
            return True
        if (
            row.run_id != self.run_id
            or row.generation != self.generation
            or row.owner_id != self.owner_id
            or row.phase == "idle"
        ):
            self._lost = True
            return True
        return row.abort_requested_at is not None

    async def assert_current(self) -> None:
        """Fence an external side-effect boundary."""
        if self._lost:
            raise LeaseLostError(
                f"agent lease lost for {self.session_id} generation {self.generation}"
            )
        if not await self.renew():
            self._lost = True
            self.abort.set()
            raise LeaseLostError(
                f"agent lease lost for {self.session_id} generation {self.generation}"
            )

    async def set_phase(self, phase: str) -> None:
        if phase not in {"reserved", "running", "finalizing"}:
            raise ValueError(f"invalid active agent phase: {phase}")
        # Phase advancement also refreshes the deadline, so it participates in
        # the same quota serialization as heartbeat renewal. Otherwise an
        # uncommitted phase UPDATE could resurrect a row after an admission
        # counted its old deadline as expired.
        async with _agent_quota_transaction(
            self.user_id,
            session_id=self.session_id,
        ) as db:
            session_result = await db.execute(
                select(SessionRow.id)
                .where(
                    SessionRow.id == self.session_id,
                    SessionRow.user_id == self.user_id,
                )
                .with_for_update()
            )
            session_exists = session_result.scalar_one_or_none() is not None
            session_result.close()
            if not session_exists:
                matched = False
            else:
                database_now = _database_now(db)
                result = await db.execute(
                    update(AgentDriverState)
                    .where(
                        AgentDriverState.session_id == self.session_id,
                        AgentDriverState.user_id == self.user_id,
                        AgentDriverState.run_id == self.run_id,
                        AgentDriverState.generation == self.generation,
                        AgentDriverState.owner_id == self.owner_id,
                        AgentDriverState.phase != "idle",
                        AgentDriverState.lease_expires_at.is_not(None),
                        AgentDriverState.lease_expires_at > database_now,
                    )
                    .values(
                        phase=phase,
                        lease_expires_at=_database_lease_expiry(db),
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                matched = bool(result.rowcount)
                result.close()
        if not matched:
            self._lost = True
            self.abort.set()
            raise LeaseLostError(
                f"agent lease lost for {self.session_id} generation {self.generation}"
            )

    async def bind_trigger_message(self, message_id: str) -> None:
        """Attach the accepted user message to this durable generation."""
        async with get_db_session() as db:
            database_now = _database_now(db)
            result = await db.execute(
                update(AgentDriverState)
                .where(
                    AgentDriverState.session_id == self.session_id,
                    AgentDriverState.user_id == self.user_id,
                    AgentDriverState.run_id == self.run_id,
                    AgentDriverState.generation == self.generation,
                    AgentDriverState.owner_id == self.owner_id,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > database_now,
                )
                .values(trigger_message_id=message_id, updated_at=database_now)
                .execution_options(synchronize_session=False)
            )
            matched = bool(result.rowcount)
            result.close()
        if not matched:
            self._lost = True
            self.abort.set()
            raise LeaseLostError(
                f"agent lease lost before accepting message {message_id}"
            )

    async def release(self, *, session_status: str | None = None) -> bool:
        """Release exactly this still-live generation.

        Once the database lease expires, its identity becomes a durable recovery
        marker.  The old owner must not erase that marker while a reaper is
        snapshotting or repairing it.  A failed transaction deliberately leaves
        the local lease open (with its monitor stopped) so the caller's finalizer
        can retry or convert the identity into an expired recovery marker.
        """
        async with self._settle_lock:
            if self._closed:
                return False
            # Revoke execution-plane receipts before the database settlement.
            # Child tasks inherit ContextVars, so one may otherwise issue a
            # request after the row commits idle but before ``_closed`` is set.
            # A failed commit deliberately keeps this fail-closed; callers may
            # retry settlement or preserve the durable marker, but must never
            # resume external effects from a quiescing generation.
            self._transport_revoked = True
            # Do not cancel while SQLAlchemy/aiosqlite is inside a connection
            # operation.  Cancellation invalidates an in-memory SQLite
            # connection (and in production can discard a pooled PostgreSQL
            # connection).
            await self.stop_monitor()

            # SQLite ignores SELECT ... FOR UPDATE. The in-process lock gives
            # desktop mode the same release/reserve serialization PostgreSQL
            # gets from row locks, while the database fence remains authority
            # across API workers.
            async with _session_lock(self.session_id):
                async with get_db_session() as db:
                    # All paths that may touch both ownership rows use Session
                    # -> Driver order. Otherwise a release holding Driver while
                    # a prompt reservation holds Session can deadlock on
                    # PostgreSQL.
                    session = (
                        await db.execute(
                            select(SessionRow)
                            .where(
                                SessionRow.id == self.session_id,
                                SessionRow.user_id == self.user_id,
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    database_now = _database_now(db)
                    result = await db.execute(
                        update(AgentDriverState)
                        .where(
                            AgentDriverState.session_id == self.session_id,
                            AgentDriverState.user_id == self.user_id,
                            AgentDriverState.run_id == self.run_id,
                            AgentDriverState.generation == self.generation,
                            AgentDriverState.owner_id == self.owner_id,
                            AgentDriverState.phase != "idle",
                            AgentDriverState.lease_expires_at.is_not(None),
                            AgentDriverState.lease_expires_at > database_now,
                        )
                        .values(
                            run_id=None,
                            owner_id=None,
                            phase="idle",
                            trigger_message_id=None,
                            lease_expires_at=None,
                            abort_requested_at=None,
                            updated_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    matched = bool(result.rowcount)
                    result.close()
                    if session_status is not None and matched and session is not None:
                        session.status = session_status
                        session.updated_at = database_now

            # Marking the object closed before get_db_session exits used to
            # strand a live finalizing row when its commit failed.  Only a
            # successful context exit makes this local terminal state durable.
            self._closed = True
            self._retire_local_activity()
            return matched

    async def preserve_for_recovery(
        self,
        *,
        session_status: str | None = "error",
    ) -> bool:
        """Stop locally and make this identity an immediately expired marker.

        Maintenance repair uses this on exceptions and cancellation. Clearing
        the row would lose the only durable pointer to an unfinished repair;
        preserving it lets the next periodic pass retry safely.
        """
        async with self._settle_lock:
            if self._closed:
                return False
            self._transport_revoked = True
            await self.stop_monitor()
            try:
                async with _session_lock(self.session_id):
                    async with get_db_session() as db:
                        session = (
                            await db.execute(
                                select(SessionRow)
                                .where(
                                    SessionRow.id == self.session_id,
                                    SessionRow.user_id == self.user_id,
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()
                        database_now = _database_now(db)
                        result = await db.execute(
                            update(AgentDriverState)
                            .where(
                                AgentDriverState.session_id == self.session_id,
                                AgentDriverState.user_id == self.user_id,
                                AgentDriverState.run_id == self.run_id,
                                AgentDriverState.generation == self.generation,
                                AgentDriverState.owner_id == self.owner_id,
                                AgentDriverState.phase != "idle",
                            )
                            .values(
                                lease_expires_at=database_now,
                                updated_at=database_now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        matched = bool(result.rowcount)
                        result.close()
                        if (
                            session_status is not None
                            and matched
                            and session is not None
                        ):
                            session.status = session_status
                            session.updated_at = database_now
            finally:
                # If the database is unavailable the existing deadline still
                # turns this exact identity into a recovery marker. Retire the
                # local fast path so waiters consult that durable state rather
                # than an unmonitored in-process activity forever.
                self._closed = True
                self._retire_local_activity()
            return matched

    def _retire_local_activity(self) -> None:
        clear_abort(self.session_id, self.abort)
        activity = _activities.get(self.session_id)
        if (
            activity is not None
            and activity.run_id == self.run_id
            and activity.generation == self.generation
        ):
            activity.idle.set()
            _activities.pop(self.session_id, None)


def _activate_local_lease(
    *,
    session_id: str,
    user_id: str,
    run_id: str,
    generation: int,
    lease_expires_at: datetime,
) -> RunLease:
    """Publish a committed database claim into this process's fast path."""
    previous = _activities.get(session_id)
    if previous is not None:
        # Its database writes are fenced out. Nudge the stale local coroutine
        # so it converges without waiting for another heartbeat boundary.
        previous.abort.set()
    abort = register_run(session_id)
    _activities[session_id] = _LocalActivity(run_id, generation, abort)
    lease = RunLease(
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        generation=generation,
        owner_id=WORKER_ID,
        abort=abort,
        _lease_expires_at=_aware(lease_expires_at),
    )
    lease.start_monitor()
    return lease


def _publish_reserved_status(session_id: str, user_id: str, generation: int) -> None:
    """Announce the busy revision immediately after its reservation commits."""
    bus.publish(
        SESSION_STATUS,
        {
            "userId": user_id,
            "sessionId": session_id,
            "status": "busy",
            "generation": generation,
        },
    )


async def reserve_run(
    session_id: str,
    user_id: str,
    *,
    run_id: str | None = None,
    trigger_message_id: str | None = None,
    initial_phase: str = "reserved",
) -> RunLease:
    """Synchronously reserve the running phase before any background wake."""
    if initial_phase not in {"reserved", "running", "finalizing"}:
        raise ValueError(f"invalid initial agent phase: {initial_phase}")
    async with _agent_quota_transaction(
        user_id,
        session_id=session_id,
    ) as db:
        session_result = await db.execute(
            select(SessionRow)
            .where(
                SessionRow.id == session_id,
                SessionRow.user_id == user_id,
                SessionRow.is_deleted.is_(False),
            )
            .with_for_update()
        )
        session = session_result.scalar_one_or_none()
        session_result.close()
        if session is None:
            raise LookupError(f"session {session_id} not found")

        state_result = await db.execute(
            select(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .with_for_update()
        )
        state = state_result.scalar_one_or_none()
        state_result.close()
        # Read the database clock only after both ownership rows are
        # locked. A worker that waited behind another transaction must not
        # decide liveness using a timestamp captured before that wait.
        clock_result = await db.execute(select(_database_now(db)))
        now = _aware(clock_result.scalar_one())
        clock_result.close()
        assert now is not None
        if state is not None and _is_live(state, now):
            raise DriverBusyError(session_id, state.run_id, state.generation)
        if state is not None and state.phase != "idle":
            # Never let a user prompt (or another generic caller) erase an
            # expired run identity.  Recovery must first claim this exact
            # marker and repair/replay it according to its durable phase.
            raise DriverRecoveryRequiredError(
                RecoveredDriver(
                    session_id=state.session_id,
                    user_id=state.user_id,
                    run_id=state.run_id,
                    generation=state.generation,
                    phase=state.phase,
                    trigger_message_id=state.trigger_message_id,
                )
            )

        await _enforce_agent_quota_locked(
            db,
            session_id=session_id,
            user_id=user_id,
        )

        generation = (state.generation if state is not None else 0) + 1
        actual_run_id = run_id or uuid.uuid4().hex
        expiry = now + timedelta(seconds=LEASE_SECONDS)
        if state is None:
            state = AgentDriverState(
                session_id=session_id,
                user_id=user_id,
                generation=generation,
                run_id=actual_run_id,
                owner_id=WORKER_ID,
                phase=initial_phase,
                trigger_message_id=trigger_message_id,
                lease_expires_at=expiry,
                abort_requested_at=None,
                started_at=now,
                updated_at=now,
            )
            db.add(state)
        else:
            state.user_id = user_id
            state.generation = generation
            state.run_id = actual_run_id
            state.owner_id = WORKER_ID
            state.phase = initial_phase
            state.trigger_message_id = trigger_message_id
            state.lease_expires_at = expiry
            state.abort_requested_at = None
            state.started_at = now
            state.updated_at = now
        # Reserve the public busy read model in the same transaction.  The
        # loop publishes the WebSocket event after it takes over.
        session.status = "busy"
        session.updated_at = now
        await db.flush()

    _publish_reserved_status(session_id, user_id, generation)
    return _activate_local_lease(
        session_id=session_id,
        user_id=user_id,
        run_id=actual_run_id,
        generation=generation,
        lease_expires_at=expiry,
    )


async def reserve_recovered_run(
    record: RecoveredDriver,
    *,
    initial_phase: str,
) -> RunLease:
    """Take over exactly one still-expired durable recovery marker.

    Unlike :func:`reserve_run`, this is not allowed to claim whatever state is
    current.  A user prompt or another reaper may have advanced the Session
    after the snapshot; in that case the old recovery work is stale and must
    not replay or repair beneath the newer generation.
    """
    if initial_phase not in {"reserved", "finalizing"}:
        raise ValueError(f"invalid recovery phase: {initial_phase}")
    # Exact recovery preserves the prior logical work identity, but turning an
    # expired marker live again still consumes a provider-capable slot. It uses
    # the same quota transaction as ordinary admission so a full cluster leaves
    # the marker untouched for the next recovery pass.
    async with _agent_quota_transaction(
        record.user_id,
        session_id=record.session_id,
    ) as db:
        session_result = await db.execute(
            select(SessionRow)
            .where(
                SessionRow.id == record.session_id,
                SessionRow.user_id == record.user_id,
                SessionRow.is_deleted.is_(False),
            )
            .with_for_update()
        )
        session = session_result.scalar_one_or_none()
        session_result.close()
        if session is None:
            raise LookupError(f"session {record.session_id} not found")

        state_result = await db.execute(
            select(AgentDriverState)
            .where(AgentDriverState.session_id == record.session_id)
            .with_for_update()
        )
        state = state_result.scalar_one_or_none()
        state_result.close()
        clock_result = await db.execute(select(_database_now(db)))
        now = _aware(clock_result.scalar_one())
        clock_result.close()
        assert now is not None
        expiry = _aware(state.lease_expires_at) if state is not None else None
        exact_expired_marker = bool(
            state is not None
            and state.user_id == record.user_id
            and state.run_id == record.run_id
            and state.generation == record.generation
            and state.phase == record.phase
            and state.trigger_message_id == record.trigger_message_id
            and state.phase != "idle"
            and expiry is not None
            and expiry <= now
        )
        if not exact_expired_marker:
            raise StaleRecoveryError(
                f"recovery marker changed for session {record.session_id}"
            )

        await _enforce_agent_quota_locked(
            db,
            session_id=record.session_id,
            user_id=record.user_id,
        )

        generation = record.generation + 1
        actual_run_id = uuid.uuid4().hex
        state.generation = generation
        state.run_id = actual_run_id
        state.owner_id = WORKER_ID
        state.phase = initial_phase
        state.trigger_message_id = record.trigger_message_id
        new_expiry = now + timedelta(seconds=LEASE_SECONDS)
        state.lease_expires_at = new_expiry
        state.abort_requested_at = None
        state.started_at = now
        state.updated_at = now
        session.status = "busy"
        session.updated_at = now
        await db.flush()

    _publish_reserved_status(record.session_id, record.user_id, generation)
    return _activate_local_lease(
        session_id=record.session_id,
        user_id=record.user_id,
        run_id=actual_run_id,
        generation=generation,
        lease_expires_at=new_expiry,
    )


async def reserve_aborted_run_settlement(
    session_id: str,
    user_id: str,
    *,
    settled_run_id: str,
    settled_generation: int,
) -> RunLease:
    """Claim maintenance immediately after one exact run released.

    Interruption bookkeeping is part of the run it describes.  It must not
    append a synthetic marker or rewrite the todo store after a replacement
    prompt has already reserved a newer generation.  The old owner clears its
    ``run_id`` when it releases, so the exact hand-off boundary is the idle row
    at ``settled_generation``.  Advancing that row to a short-lived
    ``finalizing`` generation serializes the bookkeeping with all prompt and
    recovery claims.

    ``settled_run_id`` is intentionally required even though a normal release
    clears it from the row.  Callers may only obtain it from the live target
    they actually aborted; accepting only a generation would make accidental
    cross-run settlement too easy at the API boundary.
    """
    if not settled_run_id:
        raise ValueError("aborted-run settlement requires a run_id")
    # This exact idle→finalizing handoff completes an already admitted run. It
    # is quota-continuation (not user work), but participates in the same locks
    # and becomes visible to subsequent admission counts while live.
    async with _agent_quota_transaction(
        user_id,
        session_id=session_id,
    ) as db:
        session_result = await db.execute(
            select(SessionRow)
            .where(
                SessionRow.id == session_id,
                SessionRow.user_id == user_id,
                SessionRow.is_deleted.is_(False),
            )
            .with_for_update()
        )
        session = session_result.scalar_one_or_none()
        session_result.close()
        if session is None:
            raise LookupError(f"session {session_id} not found")

        state_result = await db.execute(
            select(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .with_for_update()
        )
        state = state_result.scalar_one_or_none()
        state_result.close()
        exact_settled_generation = bool(
            state is not None
            and state.user_id == user_id
            and state.generation == settled_generation
            and state.phase == "idle"
            and state.run_id is None
            and state.owner_id is None
            and state.trigger_message_id is None
            and state.lease_expires_at is None
        )
        if not exact_settled_generation:
            raise StaleRecoveryError(
                f"aborted run changed before settlement for session {session_id}"
            )

        clock_result = await db.execute(select(_database_now(db)))
        now = _aware(clock_result.scalar_one())
        clock_result.close()
        assert now is not None
        generation = settled_generation + 1
        actual_run_id = uuid.uuid4().hex
        state.generation = generation
        state.run_id = actual_run_id
        state.owner_id = WORKER_ID
        state.phase = "finalizing"
        state.trigger_message_id = None
        expiry = now + timedelta(seconds=LEASE_SECONDS)
        state.lease_expires_at = expiry
        state.abort_requested_at = None
        state.started_at = now
        state.updated_at = now
        session.status = "busy"
        session.updated_at = now
        await db.flush()

    _publish_reserved_status(session_id, user_id, generation)
    return _activate_local_lease(
        session_id=session_id,
        user_id=user_id,
        run_id=actual_run_id,
        generation=generation,
        lease_expires_at=expiry,
    )


async def request_abort(
    session_id: str,
    user_id: str,
    *,
    expected_run_id: str | None = None,
    expected_generation: int | None = None,
) -> bool:
    """Persist a stop request, optionally fenced to one exact generation.

    Interactive stop keeps its historical "current or imminently starting"
    semantics. Durable child ownership always supplies both expected fields:
    an old activation must never abort a replacement generation that reused
    the same Session.
    """
    exact = expected_run_id is not None or expected_generation is not None
    if exact and (expected_run_id is None or expected_generation is None):
        raise ValueError("exact abort requires run_id and generation")
    if not exact:
        trigger_abort(session_id)
    async with get_db_session() as db:
        database_now = _database_now(db)
        conditions = [
            AgentDriverState.session_id == session_id,
            AgentDriverState.user_id == user_id,
            AgentDriverState.phase != "idle",
            AgentDriverState.lease_expires_at.is_not(None),
            AgentDriverState.lease_expires_at > database_now,
        ]
        if exact:
            conditions.extend(
                (
                    AgentDriverState.run_id == expected_run_id,
                    AgentDriverState.generation == expected_generation,
                )
            )
        result = await db.execute(
            update(AgentDriverState)
            .where(*conditions)
            .values(abort_requested_at=database_now, updated_at=database_now)
            .execution_options(synchronize_session=False)
        )
        matched = bool(result.rowcount)
        result.close()
    if exact and matched:
        # Do not use trigger_abort(): between the committed CAS and this local
        # nudge, a replacement generation may have registered a new signal.
        # Remote owners observe abort_requested_at; the local fast path is set
        # only when it still represents the exact row updated above.
        activity = _activities.get(session_id)
        if (
            activity is not None
            and activity.run_id == expected_run_id
            and activity.generation == expected_generation
        ):
            activity.abort.set()
    return matched


async def wait_for_idle(session_id: str, *, timeout: float = 15.0) -> bool:
    """Wait for the current activity generation, including any replacement."""
    deadline = time.monotonic() + timeout
    while True:
        activity = _activities.get(session_id)
        if activity is not None and not activity.idle.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(
                    activity.idle.wait(), timeout=min(remaining, 0.25)
                )
            except asyncio.TimeoutError:
                pass
            # Loop rather than returning: a new generation may have replaced
            # the one whose idle event just fired.
            continue

        async with get_db_session() as db:
            database_now = _database_now(db)
            result = await db.execute(
                select(AgentDriverState.session_id)
                .where(
                    AgentDriverState.session_id == session_id,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.run_id.is_not(None),
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > database_now,
                )
                .limit(1)
            )
            is_live = result.scalar_one_or_none() is not None
            result.close()
        if not is_live:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(ABORT_POLL_SECONDS, remaining))


async def wait_for_generation_end(
    session_id: str,
    *,
    run_id: str,
    generation: int,
    timeout: float = 15.0,
) -> bool:
    """Wait only for the named generation, never for its replacement.

    Prompt preemptors deliberately race.  A waiter for generation N must be
    released when N ends even if another request has already claimed N+1;
    waiting for the whole Session to become idle lets stale cleanup drift into
    that replacement turn.
    """
    deadline = time.monotonic() + timeout
    while True:
        activity = _activities.get(session_id)
        if (
            activity is not None
            and activity.run_id == run_id
            and activity.generation == generation
            and not activity.idle.is_set()
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(
                    activity.idle.wait(),
                    timeout=min(remaining, 0.25),
                )
            except asyncio.TimeoutError:
                pass
            continue

        async with get_db_session() as db:
            database_now = _database_now(db)
            result = await db.execute(
                select(AgentDriverState.session_id)
                .where(
                    AgentDriverState.session_id == session_id,
                    AgentDriverState.run_id == run_id,
                    AgentDriverState.generation == generation,
                    AgentDriverState.phase != "idle",
                    AgentDriverState.lease_expires_at.is_not(None),
                    AgentDriverState.lease_expires_at > database_now,
                )
                .limit(1)
            )
            is_live = result.scalar_one_or_none() is not None
            result.close()
        if not is_live:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(ABORT_POLL_SECONDS, remaining))


async def get_driver_state(session_id: str) -> AgentDriverState | None:
    async with get_db_session() as db:
        result = await db.execute(
            select(AgentDriverState).where(AgentDriverState.session_id == session_id)
        )
        state = result.scalar_one_or_none()
        result.close()
        return state


async def recover_expired_driver_records() -> list[RecoveredDriver]:
    """Snapshot expired owners and preserve their identity as recovery markers."""
    async with get_db_session() as db:
        # Reservation consistently locks Session before Driver. Periodic
        # recovery follows the same order to avoid a PostgreSQL deadlock with a
        # user prompt that is taking over an expired generation.
        candidate_result = await db.execute(
            select(AgentDriverState.session_id)
            .where(
                AgentDriverState.phase != "idle",
                AgentDriverState.lease_expires_at.is_not(None),
                AgentDriverState.lease_expires_at <= _database_now(db),
            )
            .order_by(AgentDriverState.session_id)
        )
        candidate_ids = list(candidate_result.scalars().all())
        candidate_result.close()
        if not candidate_ids:
            return []

        sessions = list(
            (
                await db.execute(
                    select(SessionRow)
                    .where(SessionRow.id.in_(candidate_ids))
                    .order_by(SessionRow.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        states = list(
            (
                await db.execute(
                    select(AgentDriverState)
                    .where(
                        AgentDriverState.session_id.in_(candidate_ids),
                        AgentDriverState.phase != "idle",
                        AgentDriverState.lease_expires_at.is_not(None),
                        AgentDriverState.lease_expires_at <= _database_now(db),
                    )
                    .order_by(AgentDriverState.session_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        clock_result = await db.execute(select(_database_now(db)))
        now = _aware(clock_result.scalar_one())
        clock_result.close()
        assert now is not None
        recovered = [
            RecoveredDriver(
                session_id=state.session_id,
                user_id=state.user_id,
                run_id=state.run_id,
                generation=state.generation,
                phase=state.phase,
                trigger_message_id=state.trigger_message_id,
            )
            for state in states
        ]
        recovered_ids = {record.session_id for record in recovered}
        for state in states:
            # Keep run/generation/phase/trigger/expiry intact. If this process
            # dies before repair, the next sweep can rediscover the same marker.
            state.updated_at = now
        for session in sessions:
            if session.id in recovered_ids:
                session.status = "error"
                session.updated_at = now
        return recovered


async def recover_expired_driver_sessions() -> list[str]:
    """Compatibility facade returning expired session ids."""
    return [record.session_id for record in await recover_expired_driver_records()]


async def recover_expired_drivers() -> int:
    """Compatibility/count facade for startup and focused tests."""
    return len(await recover_expired_driver_sessions())
