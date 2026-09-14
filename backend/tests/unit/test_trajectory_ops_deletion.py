"""Deletion drill checks (trajectory.ops.deletion) against a SQLite trace database and a memory blob store."""
import asyncio
import io
import json
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, delete, insert, update
from sqlalchemy.ext.asyncio import create_async_engine

from trajectory.ops import deletion
from trajectory.storage import MemoryBlobStore

SESSION = "session_1"
TRAJECTORY = "trj_1"
PREFIX = "trajectories/trj_1/"

METADATA = MetaData()
TRAJECTORIES = Table(
    "session_trajectories", METADATA,
    Column("id", String(64), primary_key=True), Column("session_id", String(64)),
    Column("recording_status", String(32)), Column("deleted_at", DateTime(timezone=True)),
)
GC_QUEUE = Table(
    "trajectory_gc_queue", METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("kind", String(16)), Column("storage_key", Text),
)


async def execute(url: str, *statements) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            for statement in statements:
                await connection.execute(statement)
    finally:
        await engine.dispose()


def trace_database(tmp_path, *gc_keys: str) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}"
    rows = [insert(GC_QUEUE).values(kind="prefix", storage_key=key) for key in gc_keys]
    asyncio.run(execute(url, insert(TRAJECTORIES).values(id=TRAJECTORY, session_id=SESSION, recording_status="recording"), *rows))
    return url


def stored(*keys: str) -> MemoryBlobStore:
    store = MemoryBlobStore()
    for key in keys:
        store.objects[key] = b"x"
    return store


def run(url: str | None, store, *argv: str, **options) -> tuple[int, dict | None]:
    stdout = io.StringIO()
    code = deletion.main(list(argv), environ={} if url is None else {deletion.TRACE_ENV: url}, blob_store=store,
                         stdout=stdout, **options)
    lines = stdout.getvalue().splitlines()
    return code, json.loads(lines[-1]) if lines else None


def test_like_prefix_escapes_wildcards():
    assert deletion.like_prefix("trajectories/trj_1/") == "trajectories/trj\\_1/%"
    assert deletion.like_prefix("a%b\\") == "a\\%b\\\\%"


def test_precheck_passes_for_a_live_trajectory_with_stored_objects(tmp_path):
    url = trace_database(tmp_path)
    store = stored(f"{PREFIX}segments/000000000001-000000001000.jsonl.zst", f"{PREFIX}blobs/{'a' * 64}", "trajectories/trj_2/x")

    code, state = run(url, store, "precheck", "--session-id", SESSION)

    assert code == 0
    assert state == {
        "session_id": SESSION, "trajectory_id": TRAJECTORY, "recording_status": "recording", "tombstoned": False,
        "prefix": PREFIX, "gc_pending": 0, "objects": 2, "objects_truncated": False, "passed": True,
        "sample_keys": [f"{PREFIX}blobs/{'a' * 64}", f"{PREFIX}segments/000000000001-000000001000.jsonl.zst"],
    }


def test_precheck_refuses_a_trajectory_that_cannot_show_the_removal(tmp_path, capsys):
    url = trace_database(tmp_path)
    code, state = run(url, stored(), "precheck", "--session-id", SESSION)
    assert code == 1 and state["passed"] is False
    assert "nothing is stored under the trajectory prefix" in capsys.readouterr().err

    asyncio.run(execute(url, update(TRAJECTORIES).values(deleted_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
                                                         recording_status="deleted")))
    code, state = run(url, stored(f"{PREFIX}blobs/x"), "precheck", "--session-id", SESSION)
    assert code == 1 and state["tombstoned"] is True


def test_unknown_sessions_and_missing_configuration_exit_2(tmp_path, capsys):
    url = trace_database(tmp_path)
    assert run(url, stored(), "precheck", "--session-id", "session_2") == (2, None)
    assert "has no trajectory" in capsys.readouterr().err
    assert run(None, stored(), "precheck", "--session-id", SESSION) == (2, None)
    assert run(url, stored(), "verify", "--session-id", "session 1") == (2, None)
    missing_tables = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    assert run(missing_tables, stored(), "precheck", "--session-id", SESSION) == (2, None)
    assert "could not run" in capsys.readouterr().err


def test_verify_waits_for_the_tombstone_the_gc_queue_and_the_objects(tmp_path):
    # trjx1 and trj_10 must not count as entries of trj_1: "_" is a LIKE wildcard and the prefix ends in "/".
    url = trace_database(tmp_path, PREFIX, "trajectories/trjx1/", "trajectories/trj_10/")
    store = stored(f"{PREFIX}segments/000000000001-000000001000.jsonl.zst")
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        store.objects.clear()
        await execute(url, update(TRAJECTORIES).values(deleted_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
                                                       recording_status="deleted"),
                      delete(GC_QUEUE).where(GC_QUEUE.c.storage_key == PREFIX))

    code, state = run(url, store, "verify", "--session-id", SESSION, "--interval", "5", sleep=sleep)

    assert code == 0 and sleeps == [5.0]
    assert (state["tombstoned"], state["gc_pending"], state["objects"], state["passed"]) == (True, 0, 0, True)
    assert state["recording_status"] == "deleted"


def test_verify_fails_when_the_objects_outlive_the_timeout(tmp_path):
    url = trace_database(tmp_path)
    asyncio.run(execute(url, update(TRAJECTORIES).values(deleted_at=datetime(2026, 9, 15, tzinfo=timezone.utc))))
    moments = iter([0.0, 1000.0])

    async def never(seconds):
        raise AssertionError("no second check after the deadline")

    code, state = run(url, stored(f"{PREFIX}blobs/x"), "verify", "--session-id", SESSION, "--timeout", "60",
                      clock=lambda: next(moments), sleep=never)

    assert code == 1
    assert (state["tombstoned"], state["objects"], state["passed"]) == (True, 1, False)
