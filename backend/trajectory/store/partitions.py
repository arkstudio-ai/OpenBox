"""Daily range partitions of ``trajectory_events`` (PostgreSQL only).

A partition named ``trajectory_events_pYYYYMMDD`` holds one UTC day of
``recorded_on``; rows without a matching partition land in
``trajectory_events_default``. SQLite has no partitioning, so every helper is
a no-op there and returns an empty result.

Partition DDL takes ACCESS EXCLUSIVE locks on the parent table and the default
partition (creating one also takes SHARE ROW EXCLUSIVE on
``session_trajectories``), while ingest and projection transactions lock those
tables in varying orders. Waiting for one lock while holding another could
deadlock with them, so the helpers first take every lock inside a savepoint
with a short ``lock_timeout``, release everything and retry when any lock is
busy, and run DDL only once all locks are held. Call them in a short
transaction of their own; the caller commits.
"""
import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.log import create_logger

log = create_logger("trajectory.store.partitions")

EVENTS_TABLE = "trajectory_events"
DEFAULT_PARTITION = "trajectory_events_default"
PARTITION_PREFIX = "trajectory_events_p"
_PARTITION_NAME = re.compile(r"^trajectory_events_p(\d{8})$")

#: Wait per lock, far below PostgreSQL's 1 s deadlock_timeout.
LOCK_TIMEOUT_MS = 100
LOCK_ATTEMPTS = 20
LOCK_RETRY_SECONDS = 0.1
_LOCK_NOT_AVAILABLE = "55P03"

T = TypeVar("T")


class PartitionLockUnavailable(RuntimeError):
    """The locks needed for partition DDL stayed busy for every attempt."""


def _utc_day(value: date) -> date:
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo is not None else value).date()
    return value


def partition_name_for(day: date) -> str:
    """``trajectory_events_pYYYYMMDD`` for a UTC day; a datetime is reduced to its UTC date."""
    return f"{PARTITION_PREFIX}{_utc_day(day):%Y%m%d}"


def partition_date(name: str) -> date | None:
    """The day covered by a daily partition name, or None for any other name."""
    match = _PARTITION_NAME.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _is_postgresql(conn) -> bool:
    """``conn`` is an ``AsyncConnection`` or an ``AsyncSession``."""
    dialect = getattr(conn, "dialect", None) or conn.get_bind().dialect
    return dialect.name == "postgresql"


async def list_partitions(conn) -> list[str]:
    """Names of the daily partitions attached to ``trajectory_events``, oldest first.

    The default partition is not listed. SQLite: ``[]``.
    """
    if not _is_postgresql(conn):
        return []
    rows = await conn.execute(text(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        f"WHERE i.inhparent = to_regclass('{EVENTS_TABLE}')"
    ))
    return sorted(name for (name,) in rows if partition_date(name) is not None)


async def ensure_partitions(conn, today: date, days_ahead: int) -> list[str]:
    """Create the missing daily partitions for ``today`` .. ``today + days_ahead`` (UTC days).

    Rows of a new partition's day that already sit in the default partition
    are moved into the new partition in the same transaction. Returns the
    created names, oldest first. SQLite: ``[]``.
    """
    if days_ahead < 0:
        raise ValueError("days_ahead must not be negative")
    if not _is_postgresql(conn):
        return []
    first = _utc_day(today)
    days = [first + timedelta(days=offset) for offset in range(days_ahead + 1)]
    existing = set(await list_partitions(conn))
    if all(partition_name_for(day) in existing for day in days):
        return []

    async def create_missing() -> list[str]:
        attached = set(await list_partitions(conn))
        created = []
        for day in days:
            name = partition_name_for(day)
            if name not in attached:
                await _create_partition(conn, name, day, day + timedelta(days=1))
                created.append(name)
        return created

    created = await _run_locked(conn, (
        "LOCK TABLE session_trajectories IN SHARE ROW EXCLUSIVE MODE",
        f"LOCK TABLE ONLY {EVENTS_TABLE} IN ACCESS EXCLUSIVE MODE",
        f"LOCK TABLE {DEFAULT_PARTITION} IN ACCESS EXCLUSIVE MODE",
    ), create_missing)
    if created:
        log.info("Created trajectory event partitions %s", ",".join(created))
    return created


async def drop_partition_if_empty(conn, name: str) -> bool:
    """Drop the attached daily partition ``name`` if it holds no rows.

    Returns False when it still has rows or is not an attached partition, and
    raises ValueError for anything but a daily partition name (the default
    partition is never dropped). SQLite: ``False``.
    """
    if partition_date(name) is None:
        raise ValueError(f"Not a daily trajectory event partition: {name!r}")
    if not _is_postgresql(conn):
        return False
    if name not in await list_partitions(conn) or await _has_rows(conn, name):
        return False

    async def drop() -> bool:
        if name not in await list_partitions(conn) or await _has_rows(conn, name):
            return False
        await conn.execute(text(f"DROP TABLE {name}"))
        return True

    dropped = await _run_locked(conn, (
        f"LOCK TABLE ONLY {EVENTS_TABLE} IN ACCESS EXCLUSIVE MODE",
        f"LOCK TABLE {DEFAULT_PARTITION} IN ACCESS EXCLUSIVE MODE",
        f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE",
    ), drop)
    if dropped:
        log.info("Dropped empty trajectory event partition %s", name)
    return dropped


async def _has_rows(conn, table: str) -> bool:
    return bool((await conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM {table})"))).scalar())


async def _create_partition(conn, name: str, lower: date, upper: date) -> None:
    bounds = f"FOR VALUES FROM ('{lower.isoformat()}') TO ('{upper.isoformat()}')"
    window = {"lower": lower, "upper": upper}
    stray = (await conn.execute(text(
        f"SELECT count(*) FROM {DEFAULT_PARTITION} WHERE recorded_on >= :lower AND recorded_on < :upper"
    ), window)).scalar_one()
    if not stray:
        await conn.execute(text(f"CREATE TABLE {name} PARTITION OF {EVENTS_TABLE} {bounds}"))
        return
    # PostgreSQL rejects a partition whose range still has rows in the default
    # partition: build the table, move those rows, then attach it.
    await conn.execute(text(f"CREATE TABLE {name} (LIKE {EVENTS_TABLE} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"))
    await conn.execute(text(
        f"WITH moved AS (DELETE FROM {DEFAULT_PARTITION} WHERE recorded_on >= :lower AND recorded_on < :upper "
        f"RETURNING *) INSERT INTO {name} SELECT * FROM moved"
    ), window)
    await conn.execute(text(f"ALTER TABLE {EVENTS_TABLE} ATTACH PARTITION {name} {bounds}"))
    log.warning("Moved %s rows from %s into %s", stray, DEFAULT_PARTITION, name)


async def _run_locked(conn, statements: tuple[str, ...], work: Callable[[], Awaitable[T]]) -> T:
    """Take every lock in ``statements`` without waiting on a busy one, then run ``work``."""
    previous = (await conn.execute(text("SELECT current_setting('lock_timeout')"))).scalar_one()
    for attempt in range(LOCK_ATTEMPTS):
        savepoint = await conn.begin_nested()
        try:
            await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))
            for statement in statements:
                await conn.execute(text(statement))
        except DBAPIError as exc:
            await savepoint.rollback()
            if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            await asyncio.sleep(LOCK_RETRY_SECONDS)
            continue
        try:
            await conn.execute(text("SELECT set_config('lock_timeout', :previous, true)"), {"previous": previous})
            result = await work()
        except Exception:
            await savepoint.rollback()
            raise
        await savepoint.commit()
        return result
    raise PartitionLockUnavailable(f"{EVENTS_TABLE} partition locks stayed busy for {LOCK_ATTEMPTS} attempts")
