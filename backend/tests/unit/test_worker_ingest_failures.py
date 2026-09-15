"""Ingest failure handling (SPEC §8.2, §8.3): quarantine moves that fail, transient errors, failure counts, retries."""
import asyncio
import hashlib
import logging
import sqlite3
import time

import asyncpg
import pytest
from sqlalchemy import exc as sa_exc

from trajectory.store.models import TrajectoryWorkerState
from trajectory.types import canonical
from trajectory.worker import ingest as ingest_module
from trajectory.worker import spool_reader
from trajectory.worker.ingest import FAILURES_STATE_KEY, RetryBatch, _Transaction, _transient
from tests.unit.test_worker_ingest import event, events_of, harness, rows, settings, trace_db  # noqa: F401

DELETED = {"type": "session.deleted", "session_id": "ses_1", "user_id": "u1", "deleted_at": "2026-09-14T09:00:00.000Z"}


class _Messages(logging.Handler):
    def __init__(self, text: str):
        super().__init__(logging.WARNING)
        self.text, self.messages = text, []

    def emit(self, record):
        if self.text in record.getMessage():
            self.messages.append(record.getMessage())


@pytest.fixture
def warnings_with():
    """``warnings_with(text)``: the list of ingest warnings containing ``text`` from then on."""
    logger = logging.getLogger("openbox.trajectory.worker.ingest")
    handlers = []

    def attach(text: str) -> list[str]:
        handler = _Messages(text)
        logger.addHandler(handler)
        handlers.append(handler)
        return handler.messages

    yield attach
    for handler in handlers:
        logger.removeHandler(handler)


def _release(service):
    """Let every file and batch that backs off retry on the next pass."""
    for failure in service._failures.values():
        failure.next_at = 0.0
    for held in service._backoff.values():
        held.next_at = 0.0


def _wait(entry) -> float:
    return entry.next_at - time.monotonic()


async def test_a_file_that_cannot_be_moved_to_quarantine_blocks_its_producer_and_backs_off(harness, monkeypatch,
                                                                                         warnings_with):
    moves = []
    move = spool_reader.quarantine_file

    def failing_move(spool_dir, item, **kwargs):
        moves.append(item.name)
        return None

    monkeypatch.setattr(spool_reader, "quarantine_file", failing_move)
    warnings = warnings_with("Could not quarantine spool file")
    writer = harness.writer
    bad = writer.file([writer.line("event", event(event_id="ok")), b"garbage\n"])
    later = writer.events(event(event_id="later"))
    key = (writer.producer_id, bad.name)
    result = await harness.run()
    assert (result["events"], result["quarantined_files"]) == (1, 0) and bad.exists() and later.exists()
    failure = harness.service._failures[key]
    assert (failure.attempts, failure.failures) == (1, 0) and 0 < _wait(failure) <= 1
    # While it backs off, neither the file nor the producer's later file is read.
    result = await harness.run()
    assert result["deferred_batches"] == 1 and moves == [bad.name] and later.exists()
    _release(harness.service)
    await harness.run()
    failure = harness.service._failures[key]
    assert len(moves) == 2 and failure.attempts == 2 and 1 < _wait(failure) <= 2 and later.exists()
    assert len(warnings) == 2
    monkeypatch.setattr(spool_reader, "quarantine_file", move)
    _release(harness.service)
    result = await harness.run()
    assert result["quarantined_files"] == 1 and not bad.exists() and not later.exists()
    assert harness.service._failures == {} and len(warnings) == 2
    _, stored = await events_of("ses_1")
    assert stored[1].event_id == "ok" and stored[-1].event_id == "later"


class _PsycopgError(Exception):
    """A driver error that reports its SQLSTATE the way psycopg does."""

    def __init__(self, pgcode: str):
        super().__init__(pgcode)
        self.pgcode = pgcode


def _wrapped(orig: BaseException, **options) -> sa_exc.DBAPIError:
    return sa_exc.OperationalError("UPDATE session_trajectories SET next_seq = 2", {}, orig, **options)


def _caused(error: Exception, cause: BaseException) -> Exception:
    error.__cause__ = cause
    return error


def _during(handled: BaseException, error: Exception) -> Exception:
    try:
        try:
            raise handled
        except BaseException:
            raise error
    except Exception as raised:
        return raised


def _chain(depth: int, last: BaseException) -> Exception:
    error = last
    for _ in range(depth):
        error = _caused(RuntimeError("wrapped"), error)
    return error


TRANSIENT = [
    asyncpg.exceptions.QueryCanceledError("canceling statement due to statement timeout"),
    asyncpg.exceptions.LockNotAvailableError("could not obtain lock on row in relation"),
    asyncpg.exceptions.DeadlockDetectedError("deadlock detected"),
    asyncpg.exceptions.SerializationError("could not serialize access due to concurrent update"),
    asyncpg.exceptions.DiskFullError("could not extend file: No space left on device"),
    asyncpg.exceptions.ReadOnlySQLTransactionError("cannot execute UPDATE in a read-only transaction"),
    asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already"),
    asyncpg.exceptions.AdminShutdownError("terminating connection due to administrator command"),
    asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
    _wrapped(_PsycopgError("55P03")),
    _wrapped(_PsycopgError("58030")),
    _wrapped(RuntimeError("the connection is closed"), connection_invalidated=True),
    _wrapped(sqlite3.OperationalError("database is locked")),
    sa_exc.TimeoutError("QueuePool limit of size 5 overflow 10 reached, connection timed out"),
    asyncio.TimeoutError(),
    ConnectionResetError("Connection reset by peer"),
    _caused(RuntimeError("the tombstone failed"), _wrapped(asyncpg.exceptions.QueryCanceledError("statement timeout"))),
    _during(asyncpg.exceptions.DeadlockDetectedError("deadlock detected"), ValueError("raised while rolling back")),
    _chain(ingest_module.TRANSIENT_DEPTH, asyncpg.exceptions.SerializationError("deep but within reach")),
]
PERMANENT = [
    RuntimeError("value out of range for type integer"),
    asyncpg.exceptions.NumericValueOutOfRangeError("value out of range for type integer"),
    _wrapped(_PsycopgError("22P02")),
    sa_exc.IntegrityError("INSERT INTO trajectory_events", {}, asyncpg.exceptions.UniqueViolationError("duplicate key")),
    _wrapped(sqlite3.OperationalError("no such table: session_trajectories")),
    _chain(ingest_module.TRANSIENT_DEPTH + 1, asyncpg.exceptions.SerializationError("too deep to be seen")),
]


@pytest.mark.parametrize("error", TRANSIENT, ids=lambda error: type(error).__name__)
def test_failures_that_pass_by_themselves_are_transient(error):
    assert _transient(error)


@pytest.mark.parametrize("error", PERMANENT, ids=lambda error: type(error).__name__)
def test_other_failures_are_not_transient(error):
    assert not _transient(error)


def test_a_cycle_of_exception_contexts_is_walked_once():
    first, second = RuntimeError("first"), RuntimeError("second")
    first.__context__, second.__context__ = second, first
    assert not _transient(first)


async def test_a_transient_failure_backs_off_without_counting_towards_quarantine(harness, monkeypatch):
    harness.configure(ingest_max_batch_failures=2)
    harness.writer.events(event(event_id="first"))
    await harness.run()

    async def locked(db, trajectory, *, reason):
        raise sa_exc.OperationalError("DELETE FROM trajectory_events", {}, sqlite3.OperationalError("database is locked"))

    async def not_probed(timeout=ingest_module.DB_PROBE_SECONDS):
        raise AssertionError("a transient failure needs no probe")

    monkeypatch.setattr(harness.retention, "tombstone", locked)
    monkeypatch.setattr(ingest_module, "trace_db_available", not_probed)
    failing = harness.writer.controls(DELETED, age=30)
    for attempt in range(1, 5):
        result = await harness.run()
        assert (result["failed_batches"], result["quarantined_files"]) == (1, 0)
        [failure] = harness.service._failures.values()
        assert (failure.attempts, failure.failures) == (attempt, 0)
        _release(harness.service)
    assert failing.exists()


async def test_the_failure_count_of_a_poison_batch_survives_a_restart(harness, monkeypatch):
    harness.configure(ingest_max_batch_failures=3)
    harness.writer.events(event(event_id="first"))
    await harness.run()

    async def poisoned(db, trajectory, *, reason):
        raise RuntimeError("value out of range for type integer")

    monkeypatch.setattr(harness.retention, "tombstone", poisoned)
    poison = harness.writer.controls(DELETED, age=30)
    key = (harness.writer.producer_id, poison.name)
    # A pass saves changed failures once RECENT_PERSIST_SECONDS passed since the last save.
    monkeypatch.setattr(ingest_module, "RECENT_PERSIST_SECONDS", 0.0)
    await harness.run()
    [state] = await rows(TrajectoryWorkerState, TrajectoryWorkerState.key == FAILURES_STATE_KEY)
    assert state.value == {"files": [[key[0], key[1], 1, 1, 0]]}
    monkeypatch.setattr(ingest_module, "RECENT_PERSIST_SECONDS", 3600.0)
    harness.configure(ingest_max_batch_failures=3)  # a restart
    assert (await harness.run())["quarantined_files"] == 0
    assert (harness.service._failures[key].attempts, harness.service._failures[key].failures) == (2, 2)
    # Held back by the throttle, saved at shutdown.
    await harness.service.flush_state()
    harness.configure(ingest_max_batch_failures=3)
    result = await harness.run()
    assert (result["failed_batches"], result["quarantined_files"]) == (1, 1) and not poison.exists()


async def test_a_batch_that_keeps_changing_backs_off_exponentially_and_warns_once_per_step(harness, monkeypatch,
                                                                                         warnings_with):
    async def changed(self, db, scan, **options):
        raise RetryBatch()

    monkeypatch.setattr(_Transaction, "begin", changed)
    warnings = warnings_with("keeps changing underneath")
    path = harness.writer.events(event(event_id="e1"))
    key = (harness.writer.producer_id, path.name, 0)
    result = await harness.run()
    held = harness.service._backoff[key]
    assert result["events"] == 0 and held.conflicts == 1 and 0 < _wait(held) <= 1 and len(warnings) == 1
    # While it backs off the batch is not prepared again, and nothing is logged.
    assert (await harness.run())["deferred_batches"] == 1 and len(warnings) == 1
    for conflicts, longest in ((2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (7, 60), (8, 60)):
        _release(harness.service)
        await harness.run()
        held = harness.service._backoff[key]
        assert held.conflicts == conflicts and longest / 2 < _wait(held) <= longest and len(warnings) == conflicts
    monkeypatch.undo()
    _release(harness.service)
    assert (await harness.run())["events"] == 1 and harness.service._backoff == {}


async def test_the_backoff_and_upload_record_of_a_batch_whose_file_is_gone_are_dropped(harness):
    tools = [{"name": "t", "description": "D" * 3000}]
    tools_sha = hashlib.sha256(canonical(tools)).hexdigest()

    def failing_tools(key):
        if key.endswith(tools_sha):
            raise ConnectionError("the tools upload fails")

    harness.store.faults["put"] = failing_tools
    path = harness.writer.events(event("request.prepared", request_id="r1", event_id="r1",
                                       data={"model": "m", "input": {"system": "S" * 3000, "tools": tools}}))
    await harness.run()
    key = (harness.writer.producer_id, path.name, 0)
    assert set(harness.service._backoff) == {key} and len(harness.service._stored[key]) == 1
    path.unlink()  # removed by hand: the batch never commits
    await harness.run()
    assert harness.service._backoff == {} and harness.service._stored == {}
