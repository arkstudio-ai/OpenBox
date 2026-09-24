"""Ingest failure handling (SPEC §8.2, §8.3): quarantine moves that fail, transient errors, failure counts, retries."""
import asyncio
import hashlib
import logging
import sqlite3
import time

import asyncpg
import orjson
import pytest
from sqlalchemy import exc as sa_exc

from trajectory import spool
from trajectory.store.models import TrajectoryWorkerState
from trajectory.types import canonical
from trajectory.worker import ingest as ingest_module
from trajectory.worker import spool_reader
from trajectory.worker.ingest import (FAILURES_STATE_KEY, IngestService, RetryBatch, _failures_from_state,
    _failures_to_state, _Transaction, _transient)
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


async def _restart(harness, **settings):
    """A new service with the saved state loaded and its backoffs released."""
    harness.configure(**settings)
    await harness.service._load_state()
    _release(harness.service)


def _marker(harness):
    return harness.settings.spool_dir / spool.CONTROL_DIR / ingest_module.INFLIGHT_FILE


class WorkerKilled(BaseException):
    """Like a kill (out of memory, say), nothing inside the worker handles it: no failure is counted."""


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
    [[*entry, retry_at]] = state.value["files"]
    assert entry == [key[0], key[1], 1, 1, 0, 0] and retry_at <= time.time() + 1
    monkeypatch.setattr(ingest_module, "RECENT_PERSIST_SECONDS", 3600.0)
    await _restart(harness, ingest_max_batch_failures=3)
    assert (await harness.run())["quarantined_files"] == 0
    assert (harness.service._failures[key].attempts, harness.service._failures[key].failures) == (2, 2)
    # Held back by the throttle, saved at shutdown.
    await harness.service.flush_state()
    await _restart(harness, ingest_max_batch_failures=3)
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


async def test_a_restart_keeps_the_backoff_of_a_failing_file(harness, monkeypatch):
    harness.writer.events(event(event_id="first"))
    await harness.run()

    async def poisoned(db, trajectory, *, reason):
        raise RuntimeError("value out of range for type integer")

    monkeypatch.setattr(harness.retention, "tombstone", poisoned)
    poison = harness.writer.controls(DELETED, age=30)
    key = (harness.writer.producer_id, poison.name)
    for _ in range(6):
        _release(harness.service)
        assert (await harness.run())["failed_batches"] == 1
    failure = harness.service._failures[key]
    assert failure.attempts == 6 and 16 < _wait(failure) <= 32 and 16 < failure.retry_at - time.time() <= 32
    await harness.service.flush_state()
    harness.configure()  # the restart does not retry the file before its backoff ends
    result = await harness.run()
    assert (result["deferred_batches"], result["failed_batches"]) == (1, 0) and poison.exists()
    assert 16 < _wait(harness.service._failures[key]) <= 32


def test_saved_retry_times_resume_a_backoff_never_below_zero_nor_beyond_its_attempts():
    now = time.time()
    saved = {"files": [["p", "ends_later", 6, 6, 0, 0, now + 20.0], ["p", "ended", 6, 6, 0, 0, now - 100.0],
                       ["p", "clock_set_back", 1, 1, 0, 0, now + 3600.0], ["p", "broken", 1, 1, 0, 0, "soon"]]}
    failures = _failures_from_state(saved)
    assert sorted(name for _, name in failures) == ["clock_set_back", "ended", "ends_later"]
    assert 15 < _wait(failures[("p", "ends_later")]) <= 20
    assert -1 < _wait(failures[("p", "ended")]) <= 0
    assert 0 < _wait(failures[("p", "clock_set_back")]) <= ingest_module.backoff_seconds(1)
    assert _failures_to_state(failures) == {"files": sorted(saved["files"][:3])}


async def test_the_in_flight_marker_names_the_batch_being_read_and_goes_afterwards(harness, monkeypatch):
    harness.configure(ingest_batch_lines=1)
    path = harness.writer.events(event(event_id="e1"), event(event_id="e2"))
    first, second = path.read_bytes().splitlines(keepends=True)
    marker = _marker(harness)
    named = []
    read_batch = spool_reader.read_batch

    def reading(file, offset, **options):
        named.append(orjson.loads(marker.read_bytes()))
        return read_batch(file, offset, **options)

    monkeypatch.setattr(spool_reader, "read_batch", reading)
    assert (await harness.run())["events"] == 2 and not marker.exists()
    assert named == [{"producer_id": harness.writer.producer_id, "file": path.name, "offset": offset}
                     for offset in (0, len(first), len(first) + len(second))]

    async def failing(self, *args, **options):
        raise RuntimeError("the batch failed")

    # A batch that raises is a failure, not a crash: its marker goes too.
    monkeypatch.setattr(IngestService, "_ingest_batch", failing)
    failed = harness.writer.events(event(event_id="e3"))
    assert (await harness.run())["failed_batches"] == 1 and not marker.exists()
    failure = harness.service._failures[(harness.writer.producer_id, failed.name)]
    assert (failure.failures, failure.crashes) == (1, 0)


async def test_a_marker_left_by_a_killed_process_counts_a_crash_until_its_batch_goes_through(harness):
    path = harness.writer.events(event(event_id="e1"))
    key = (harness.writer.producer_id, path.name)
    marker = _marker(harness)
    spool.ensure_private_dir(marker.parent)
    for offset, crashes in ((0, 1), (0, 2), (5, 1), (5, 2)):
        marker.write_bytes(orjson.dumps({"producer_id": key[0], "file": key[1], "offset": offset}))
        harness.configure()  # the killed process restarted
        await harness.service._load_state()
        failure = harness.service._failures[key]
        assert (failure.offset, failure.crashes) == (offset, crashes) and not marker.exists()
        # Saved at once: the batch may kill the new process before any throttled save.
        [state] = await rows(TrajectoryWorkerState, TrajectoryWorkerState.key == FAILURES_STATE_KEY)
        assert state.value["files"][0][:6] == [key[0], key[1], crashes, 0, offset, crashes]
        assert 0 < _wait(failure) <= ingest_module.backoff_seconds(crashes)
    marker.write_bytes(b'{"file": 3}')  # names no batch
    harness.configure()
    await harness.service._load_state()
    assert harness.service._failures[key].crashes == 2 and not marker.exists()
    _release(harness.service)
    assert (await harness.run())["events"] == 1 and harness.service._failures == {}
    await harness.service.flush_state()
    [state] = await rows(TrajectoryWorkerState, TrajectoryWorkerState.key == FAILURES_STATE_KEY)
    assert state.value == {"files": []}


async def test_shared_worker_crashes_preserve_the_batch_and_resume_without_a_gap(harness, monkeypatch):
    """A checkpoint/read OOM can kill a healthy in-flight ingest. Back off, retain ordering and replay it."""
    writer = harness.writer
    writer.events(event(event_id="first", run_id="run_c"))
    await harness.run()
    await harness.service.flush_state()  # the recent sessions a restarted worker reports losses for
    pending = writer.events(event(event_id="pending"), age=20)
    later = writer.events(event(event_id="later"), age=10)
    key = (writer.producer_id, pending.name)

    async def killed(self, *args, **options):
        raise WorkerKilled()

    monkeypatch.setattr(IngestService, "_commit", killed)
    # A killed process runs no finally block: the marker stays.
    monkeypatch.setattr(IngestService, "_clear_inflight", lambda self, **options: None)
    with pytest.raises(WorkerKilled):
        await harness.run()
    for crashes in (1, 2, 3, 4):
        harness.configure()  # the restart
        result = await harness.run()
        assert result["deferred_batches"] == 1 and result["quarantined_files"] == 0
        assert pending.exists() and later.exists()
        _release(harness.service)
        with pytest.raises(WorkerKilled):
            await harness.run()
        assert harness.service._failures[key].crashes == crashes and pending.exists()
    monkeypatch.undo()
    harness.configure()
    result = await harness.run()
    failure = harness.service._failures[key]
    assert (result["quarantined_files"], result["events"], failure.crashes, failure.attempts) == (0, 0, 5, 5)
    assert pending.exists() and later.exists() and 0 < _wait(failure) <= 16
    _release(harness.service)
    result = await harness.run()
    assert (result["quarantined_files"], result["events"]) == (0, 2) and not pending.exists() and not later.exists()
    _, stored = await events_of("ses_1")
    assert [row.event_id for row in stored[1:]] == ["first", "pending", "later"]
    assert harness.service._failures == {} and not _marker(harness).exists()
