"""Budget levels and the spool control files written by the worker (SPEC §5.7, §13).

Producers read ``control/budgets.json`` and filter events for degraded or
blocked root sessions and users. The worker derives the levels from the
trajectory counters maintained by ingest and from per-user bytes ingested in
the current UTC day, keeps ``session_trajectories.budget_level`` in step, and
rewrites the file atomically when a level changes (and at least every minute).
``control/worker.json`` is the worker heartbeat.
"""
from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select, update

from core.log import create_logger
from trajectory import spool
from trajectory.lifecycle import allow_long_statements
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryWorkerState

log = create_logger("trajectory.worker.budgets")

NORMAL, DEGRADED, BLOCKED = "normal", "degraded", "blocked"
REASON_EVENTS = "trajectory_events"
REASON_BYTES = "trajectory_bytes"
REASON_BLOCK_BYTES = "trajectory_block_bytes"
REASON_USER_DAILY = "user_daily_bytes"
USER_BYTES_STATE_KEY = "budget.user_daily_bytes"
REWRITE_SECONDS = 60.0


def utc_day(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def iso(moment: datetime) -> str:
    return spool.timestamp(moment.timestamp())


def trajectory_level(settings, *, event_count: int, stored_bytes: int) -> tuple[str, str | None]:
    """The level SPEC §13 assigns to one trajectory's counters."""
    if stored_bytes >= settings.budget_trajectory_block_bytes:
        return BLOCKED, REASON_BLOCK_BYTES
    if stored_bytes >= settings.budget_trajectory_bytes:
        return DEGRADED, REASON_BYTES
    if event_count >= settings.budget_trajectory_events:
        return DEGRADED, REASON_EVENTS
    return NORMAL, None


async def add_user_bytes(db, additions: dict[str, int], *, now: datetime) -> None:
    """Count ingested bytes per user for the UTC day, inside the ingest transaction."""
    additions = {user: size for user, size in additions.items() if size > 0}
    if not additions:
        return
    today = utc_day(now)
    # Row lock: the budget pass rewrites this row in its own transaction (BudgetService._user_levels).
    row = await db.get(TrajectoryWorkerState, USER_BYTES_STATE_KEY, with_for_update=True)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    users = dict(value.get("users") or {}) if value.get("day") == today else {}
    exceeded = dict(value.get("exceeded") or {}) if value.get("day") == today else {}
    for user, size in additions.items():
        users[user] = int(users.get(user, 0)) + size
    document = {"day": today, "users": users, "exceeded": exceeded}
    if row is None:
        db.add(TrajectoryWorkerState(key=USER_BYTES_STATE_KEY, value=document, updated_at=now))
    else:
        row.value, row.updated_at = document, now


class BudgetService:
    """Computes levels from the trace database and writes the two control files."""

    def __init__(self, settings, *, metrics, spool_dir: Path | None = None):
        self.settings = settings
        self.metrics = metrics
        self.spool_dir = Path(spool_dir or settings.spool_dir)
        self._last_document: dict | None = None
        self._last_written = 0.0
        self._since: dict[tuple[str, str], tuple[str, str | None, str]] | None = None

    async def run_once(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        if self._since is None:
            self._since = await asyncio.to_thread(self._read_existing_since)
        settings = self.settings
        async with trace_session() as db:
            # This scans the trajectory rows, longer than the trace role's request statement timeout allows.
            await allow_long_statements(db)
            rows = (await db.execute(select(
                SessionTrajectory.id, SessionTrajectory.session_id, SessionTrajectory.event_count,
                SessionTrajectory.stored_bytes, SessionTrajectory.budget_level, SessionTrajectory.budget_reason)
                .where(SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None), or_(
                    SessionTrajectory.event_count >= settings.budget_trajectory_events,
                    SessionTrajectory.stored_bytes >= settings.budget_trajectory_bytes,
                    SessionTrajectory.budget_level != NORMAL)))).all()
            users = await self._user_levels(db, now)
        sessions, changes = {}, []
        for trajectory_id, session_id, events, stored, current_level, current_reason in rows:
            level, reason = trajectory_level(settings, event_count=events, stored_bytes=stored)
            if (level, reason) != (current_level, current_reason):
                changes.append((trajectory_id, level, reason))
            if level != NORMAL:
                sessions[session_id] = self._entry(("session", session_id), level, reason, now)
        for trajectory_id, level, reason in changes:
            # One short transaction per row: the budget pass never holds several
            # trajectory row locks, so it cannot deadlock with an ingest batch.
            async with trace_session() as db:
                await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == trajectory_id)
                                 .values(budget_level=level, budget_reason=reason)
                                 .execution_options(synchronize_session=False))
        changed = len(changes)
        document = spool.budgets_document(sessions=sessions, users=users, generated_at=iso(now))
        self._since = {key: value for key, value in self._since.items()
                       if (key[0] == "session" and key[1] in sessions) or (key[0] == "user" and key[1] in users)}
        written = await self._write(document)
        self._gauge("trajectories_degraded", sum(1 for entry in sessions.values() if entry["level"] == DEGRADED))
        self._gauge("trajectories_blocked", sum(1 for entry in sessions.values() if entry["level"] == BLOCKED))
        return {"sessions": len(sessions), "users": len(users), "changed": changed, "written": written}

    async def _user_levels(self, db, now: datetime) -> dict:
        # Row lock: ingest adds bytes to this row in its batch transactions (add_user_bytes); without it
        # either side could overwrite the other's update of the shared JSON value on PostgreSQL.
        row = await db.get(TrajectoryWorkerState, USER_BYTES_STATE_KEY, with_for_update=True)
        value = row.value if row is not None and isinstance(row.value, dict) else {}
        if value.get("day") != utc_day(now):
            return {}
        limit = self.settings.budget_user_daily_bytes
        users = {}
        exceeded = dict(value.get("exceeded") or {})
        for user, size in (value.get("users") or {}).items():
            if isinstance(size, int) and size >= limit:
                exceeded.setdefault(user, iso(now))
        for user, since in exceeded.items():
            # Degraded for the rest of the UTC day once over the limit.
            users[user] = {"level": DEGRADED, "reason": REASON_USER_DAILY, "since": since}
            self._since[("user", user)] = (DEGRADED, REASON_USER_DAILY, since)
        if exceeded != (value.get("exceeded") or {}):
            row.value = {**value, "exceeded": exceeded}
            row.updated_at = now
        return users

    def _entry(self, key, level: str, reason: str | None, now: datetime) -> dict:
        previous = self._since.get(key)
        since = previous[2] if previous is not None and previous[:2] == (level, reason) else iso(now)
        self._since[key] = (level, reason, since)
        return {"level": level, "reason": reason, "since": since}

    def _read_existing_since(self) -> dict:
        try:
            import orjson
            document = orjson.loads(spool.budgets_path(self.spool_dir).read_bytes())
        except Exception:
            return {}
        since = {}
        for section, kind in (("sessions", "session"), ("users", "user")):
            entries = document.get(section) if isinstance(document, dict) else None
            for key, entry in (entries or {}).items() if isinstance(entries, dict) else ():
                if isinstance(entry, dict) and isinstance(entry.get("since"), str):
                    since[(kind, key)] = (entry.get("level"), entry.get("reason"), entry["since"])
        return since

    async def _write(self, document: dict) -> bool:
        comparable = {key: value for key, value in document.items() if key != "generated_at"}
        loop = asyncio.get_running_loop()
        path = spool.budgets_path(self.spool_dir)
        if (comparable == self._last_document and loop.time() - self._last_written < REWRITE_SECONDS
                and await asyncio.to_thread(path.exists)):
            return False
        try:
            await asyncio.to_thread(spool.write_budgets, self.spool_dir, document)
        except OSError as exc:
            log.warning("Budget file write failed error_type=%s", type(exc).__name__)
            return False
        self._last_document, self._last_written = comparable, loop.time()
        return True

    def _gauge(self, name: str, value) -> None:
        try:
            self.metrics.set_gauge(name, value)
        except Exception:
            pass


def write_heartbeat(spool_dir: Path, *, ingest_lag_seconds: float, now: datetime | None = None) -> None:
    """``control/worker.json``: ``{"version", "pid", "hostname", "updated_at", "ingest_lag_seconds"}``."""
    moment = now or datetime.now(timezone.utc)
    directory = Path(spool_dir) / spool.CONTROL_DIR
    spool.ensure_private_dir(directory)
    spool.write_json_atomic(directory / spool.WORKER_FILE, {
        "version": spool.VERSION, "pid": os.getpid(), "hostname": socket.gethostname(),
        "updated_at": iso(moment), "ingest_lag_seconds": round(max(0.0, ingest_lag_seconds), 3)})
