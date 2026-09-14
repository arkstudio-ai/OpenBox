"""Partition helpers: naming and SQLite no-ops. PostgreSQL behaviour: tests/integration/test_trace_pg_schema.py."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect

from trajectory.store import partitions
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
import trajectory.store.models  # noqa: F401


class Untouchable:
    """A connection stand-in that fails the test if any database work is attempted."""

    def __getattr__(self, name):
        raise AssertionError(f"database access attempted: {name}")


def test_partition_names_round_trip_on_utc_days():
    assert partitions.partition_name_for(date(2026, 9, 14)) == "trajectory_events_p20260914"
    assert partitions.partition_date("trajectory_events_p20260914") == date(2026, 9, 14)
    shanghai = timezone(timedelta(hours=8))
    assert partitions.partition_name_for(datetime(2026, 9, 15, 1, 30, tzinfo=shanghai)) == "trajectory_events_p20260914"
    assert partitions.partition_name_for(datetime(2026, 9, 14, 23, 59)) == "trajectory_events_p20260914"
    for name in ("trajectory_events_default", "trajectory_events_p2026091", "trajectory_events_p20261301",
                 "other_events_p20260914", "trajectory_events_p20260914; DROP TABLE trajectory_events",
                 "trajectory_events_p20260914\n", " trajectory_events_p20260914", "trajectory_events_p20260914x"):
        assert partitions.partition_date(name) is None, repr(name)


async def test_invalid_arguments_fail_before_any_database_work():
    with pytest.raises(ValueError):
        await partitions.drop_partition_if_empty(Untouchable(), partitions.DEFAULT_PARTITION)
    with pytest.raises(ValueError):
        await partitions.drop_partition_if_empty(Untouchable(), "trajectory_events")
    with pytest.raises(ValueError):
        await partitions.drop_partition_if_empty(Untouchable(), "trajectory_events_p20260914\n")
    with pytest.raises(ValueError):
        await partitions.ensure_partitions(Untouchable(), date(2026, 9, 14), -1)


async def test_helpers_are_noops_on_sqlite(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(TraceBase.metadata.create_all)
        async with engine.begin() as connection:
            before = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert await partitions.ensure_partitions(connection, date(2026, 9, 14), 7) == []
            assert await partitions.list_partitions(connection) == []
            assert await partitions.drop_partition_if_empty(connection, "trajectory_events_p20260914") is False
            assert await connection.run_sync(lambda sync: set(inspect(sync).get_table_names())) == before
        async with trace_session() as session:
            assert await partitions.list_partitions(session) == []
            assert await partitions.ensure_partitions(session, date(2026, 9, 14), 0) == []
    finally:
        await close_trace_engine()
