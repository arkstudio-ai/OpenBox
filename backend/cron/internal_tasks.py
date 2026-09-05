"""Database-claimed periodic maintenance tasks shared by all backend replicas."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, text, update

from core.log import create_logger
from db.base import get_db_session
from db.models.internal_task import InternalTaskState


log = create_logger("cron.internal_tasks")
STUCK_SEC = 15 * 60
MAX_BACKOFF_SEC = 60 * 60


@dataclass(frozen=True)
class RegisteredTask:
    name: str
    interval_sec: int
    fn: Callable[[], Awaitable[None]]


_tasks: dict[str, RegisteredTask] = {}


def register(
    name: str,
    interval_sec: int,
    fn: Callable[[], Awaitable[None]],
) -> None:
    if not name or len(name) > 128:
        raise ValueError("Internal task name must be 1-128 characters")
    if interval_sec < 1:
        raise ValueError("Internal task interval must be positive")
    existing = _tasks.get(name)
    candidate = RegisteredTask(name=name, interval_sec=interval_sec, fn=fn)
    if existing and existing != candidate:
        raise ValueError(f"Internal task already registered: {name}")
    _tasks[name] = candidate


async def _ensure_state(task: RegisteredTask, now: datetime) -> None:
    async with get_db_session() as db:
        await db.execute(
            text(
                """INSERT INTO internal_task_state
                   (name, consecutive_failures, updated_at)
                   VALUES (:name, 0, :updated_at)
                   ON CONFLICT (name) DO NOTHING"""
            ),
            {"name": task.name, "updated_at": now},
        )


async def _claim(task: RegisteredTask, now: datetime) -> bool:
    due_before = now - timedelta(seconds=task.interval_sec)
    stuck_before = now - timedelta(seconds=STUCK_SEC)
    async with get_db_session() as db:
        result = await db.execute(
            update(InternalTaskState)
            .where(
                InternalTaskState.name == task.name,
                or_(
                    InternalTaskState.running_at.is_(None),
                    InternalTaskState.running_at < stuck_before,
                ),
                or_(
                    InternalTaskState.last_run_at.is_(None),
                    InternalTaskState.last_run_at <= due_before,
                ),
                or_(
                    InternalTaskState.backoff_until.is_(None),
                    InternalTaskState.backoff_until <= now,
                ),
            )
            .values(running_at=now, updated_at=now)
        )
        return result.rowcount == 1


async def _finish_ok(task: RegisteredTask, now: datetime) -> None:
    async with get_db_session() as db:
        await db.execute(
            update(InternalTaskState)
            .where(InternalTaskState.name == task.name)
            .values(
                running_at=None,
                last_run_at=now,
                last_status="ok",
                last_error=None,
                backoff_until=None,
                consecutive_failures=0,
                updated_at=now,
            )
        )


async def _finish_error(task: RegisteredTask, now: datetime, error: Exception) -> None:
    async with get_db_session() as db:
        failures = (
            await db.execute(
                select(InternalTaskState.consecutive_failures).where(
                    InternalTaskState.name == task.name
                )
            )
        ).scalar_one_or_none() or 0
        failures += 1
        delay = min(task.interval_sec * (2 ** failures), MAX_BACKOFF_SEC)
        await db.execute(
            update(InternalTaskState)
            .where(InternalTaskState.name == task.name)
            .values(
                running_at=None,
                last_run_at=now,
                last_status="error",
                last_error=str(error)[:4000],
                backoff_until=now + timedelta(seconds=delay),
                consecutive_failures=failures,
                updated_at=now,
            )
        )


async def tick() -> None:
    """Claim and execute every due registered task at most once per interval."""
    for task in tuple(_tasks.values()):
        now = datetime.now(timezone.utc)
        try:
            await _ensure_state(task, now)
            if not await _claim(task, now):
                continue
            try:
                await task.fn()
            except Exception as exc:
                await _finish_error(task, datetime.now(timezone.utc), exc)
                log.warning("Internal task failed name=%s error=%s", task.name, exc)
            else:
                await _finish_ok(task, datetime.now(timezone.utc))
        except Exception as exc:
            log.warning(
                "Internal task tick failed name=%s error_type=%s",
                task.name,
                type(exc).__name__,
            )


async def _noop() -> None:
    return None


def register_builtin_tasks() -> None:
    """Register the smoke-test task and future maintenance tasks centrally."""
    from core.config import get_config
    from sandbox.fleet import run_snapshot_task

    register("noop", 30, _noop)
    register(
        "fleet_snapshot",
        get_config().fleet_snapshot_interval_sec,
        run_snapshot_task,
    )
