"""Spool ingest (SPEC §8.2, §8.3, §8.5-§8.7).

A pass scans the spool, takes the next ready file of every producer (oldest
first) and ingests it in batches. A batch is prepared outside the database:
decoding, redaction and content preparation (``content.py``) run in a thread,
blob uploads are idempotent (content-addressed keys). The batch is then
applied in one trace transaction together with the file offset, so a crash
anywhere replays it from the committed offset, and keep-first event keys
drop whatever had already been committed. Consumed files are deleted and
``trajectory.available`` is published only after the commit.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from core.log import create_logger
from trajectory import spool
from trajectory.lifecycle import revoke_asset, worker_setting
from trajectory.storage import blob_key
from trajectory.store.database import trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryEventKey, TrajectoryGcQueue,
    TrajectoryIngestFile, TrajectoryIngestProducer, TrajectoryPayload, TrajectoryWorkerState)
from trajectory.types import ID_FIELDS
from trajectory.worker import content, meta, spool_reader
from trajectory.worker.budgets import add_user_bytes
from trajectory.worker.lock import ObjectGuard
from trajectory.worker.notify import publish_available

log = create_logger("trajectory.worker.ingest")

MAX_UPLOAD_ATTEMPTS = 10
BACKOFF_FIRST_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
#: After a failed file batch the trace database is probed this long: a failure while it does not answer is an
#: outage and never counts towards TRAJECTORY_INGEST_MAX_BATCH_FAILURES.
DB_PROBE_SECONDS = 5.0
#: Characters of the last error a quarantined batch records in its ``.reason`` file.
ERROR_TEXT_LIMIT = 1000
#: Immediate re-preparations of one batch when the transaction finds changed state.
MAX_PREPARE_RETRIES = 3
UPLOAD_CONCURRENCY = 8
QUERY_CHUNK = 500
RECENT_SESSION_SECONDS = 600.0
RECENT_PERSIST_SECONDS = 60.0
RECENT_STATE_KEY = "ingest.recent_sessions"
GAP_RUN_IDS = 10
GAP_REQUEST_IDS = 50
EVENT_ID_CHARS = 128
SESSION_ID_CHARS = 64
TYPE_CHARS = 64
REQUEST_ID_CHARS = 128
TRAJECTORY_SCHEMA_VERSION = 2
PROVISIONAL_IDS = 10000
LOGGED_CONFLICTS = 10000
INVALID_LOG_SECONDS = 60.0
#: Invalid event field -> monotonic time before which it is not logged again.
_invalid_logged: dict[str, float] = {}
COUNTERS = ("lines", "events", "controls", "duplicates", "idempotency_conflicts", "deleted_drops", "ownership_drops",
            "invalid_events", "gaps", "producer_losses", "quarantined_files", "files_done", "blob_puts",
            "blob_put_bytes", "blob_put_failures", "deferred_batches", "failed_batches")
#: Pass counters -> SPEC §8.13 metric names.
METRICS = {"lines": "ingest_lines", "events": "ingest_events", "duplicates": "duplicates",
           "idempotency_conflicts": "idempotency_conflicts", "deleted_drops": "deleted_drops",
           "ownership_drops": "ownership_drops", "gaps": "gaps_recorded", "producer_losses": "producer_loss_events",
           "quarantined_files": "quarantined_files"}


class RetryBatch(Exception):
    """The transaction found state that changed after the batch was prepared."""


class UploadsDeferred(Exception):
    """Blob uploads failed; the batch waits for its backoff."""


# -- Lines ----------------------------------------------------------------------

@dataclass(eq=False)
class Item:
    """One spool line of a batch, prepared up to content addressing."""
    n: int
    t: datetime
    size: int
    end: int
    kind: str
    control: dict | None = None
    event: dict | None = None
    invalid: str | None = None
    content_hash: str | None = None
    helpers: dict = field(default_factory=dict)
    media: list = field(default_factory=list)
    occurred_at: datetime | None = None
    #: ``baseline.captured`` with a non-empty history (``trajectory.started.existing_session``).
    history: bool = False
    plan: content.ContentPlan | None = None
    trajectory_id: str | None = None
    #: The content was not prepared: the event cannot be ingested (tombstone, known duplicate).
    skipped: bool = False


@dataclass
class ParsedBatch:
    items: list[Item]
    bad_offset: int | None = None
    bad_reason: str | None = None
    bad_error: str | None = None


def _identifier(value, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value


def validate_event(event: dict) -> str | None:
    """The first field the trace columns cannot store, or ``None``."""
    if not isinstance(event.get("data"), dict):
        return "data"
    if not _identifier(event.get("type"), TYPE_CHARS):
        return "type"
    version = event.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or not -2 ** 31 <= version < 2 ** 31:
        return "version"
    if not _identifier(event.get("event_id"), EVENT_ID_CHARS):
        return "event_id"
    for key in ("user_id", "session_id"):
        if not _identifier(event.get(key), SESSION_ID_CHARS):
            return key
    if event.get("source_session_id") is not None and not _identifier(event["source_session_id"], SESSION_ID_CHARS):
        return "source_session_id"
    for key in ("request_id", "call_id", "agent_id"):
        if event.get(key) is not None and not _identifier(event[key], REQUEST_ID_CHARS):
            return key
    for key in ID_FIELDS:
        if isinstance(event.get(key), str) and "\x00" in event[key]:
            return key
    if meta.parse_time(event.get("occurred_at")) is None:
        return "occurred_at"
    return None


def parse_batch(lines) -> ParsedBatch:
    """Decode the lines of one batch and run steps 1-4 of SPEC §8.4 up to media extraction.

    Stops at the first line that is not a supported spool line; the lines before it stay ingestible.
    """
    items: list[Item] = []
    now = datetime.now(timezone.utc)
    for line in lines:
        try:
            record = spool.decode_line(line.data)
        except spool.UnsupportedSpoolVersion as exc:
            return ParsedBatch(items, line.start, "unsupported_version", type(exc).__name__)
        except spool.SpoolFormatError as exc:
            return ParsedBatch(items, line.start, "unparsable_line", type(exc).__name__)
        item = Item(n=record["n"], t=meta.parse_time(record["t"]) or now, size=line.size,
                    end=line.end, kind=record["k"])
        if item.kind == spool.KIND_CONTROL:
            item.control = record["control"]
        else:
            event = item.event = record["event"]
            item.invalid = validate_event(event)
            if item.invalid is None:
                data = event["data"] = content.redact_data(event["data"], line.data)
                item.content_hash = content.hash_event(event)
                item.helpers = content.strip_helpers(data)
                item.occurred_at = meta.parse_time(event["occurred_at"])
                history = data.get("history")
                item.history = event["type"] == "baseline.captured" and isinstance(history, list) and bool(history)
                if content.has_media_markers(line.data):
                    item.media = content.extract_media(data)
        items.append(item)
    return ParsedBatch(items)


# -- Helpers ----------------------------------------------------------------------

def gap_event_id(producer_id: str, n_or_range: str, session_id: str, run_id: str | None = None) -> str:
    """``gap:{producer_id}:{n_or_range}:{session_id}[:{run_id}]``, hashed when longer than an event id may be."""
    value = f"gap:{producer_id}:{n_or_range}:{session_id}" + (f":{run_id}" if run_id else "")
    if len(value) <= EVENT_ID_CHARS:
        return value
    return f"gap:h:{hashlib.sha256(value.encode()).hexdigest()}"


def loss_range(first: int, last: int | None) -> str:
    return f"{first}-{last}" if last is not None else f"{first}-"


def iso(moment: datetime) -> str:
    return spool.timestamp(moment.timestamp())


def _chunks(values, size: int = QUERY_CHUNK):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


async def trace_db_available(timeout: float = DB_PROBE_SECONDS) -> bool:
    """Whether a trivial query succeeds in a fresh trace session within ``timeout`` seconds; never raises."""
    async def probe() -> None:
        async with trace_session() as db:
            await db.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(probe(), timeout)
    except Exception:
        return False
    return True


@dataclass(eq=False)
class TrajectoryState:
    """A trajectory row locked by the ingest transaction, with the counters it changes."""
    id: str
    user_id: str
    session_id: str
    workspace_id: str
    next_seq: int
    committed_seq: int
    event_count: int
    stored_bytes: int
    recording_status: str
    recording_epoch: int
    last_activity_at: datetime | None
    deleted: bool = False
    expired: bool = False
    created: bool = False
    tombstoned: bool = False
    initial_committed: int = 0
    initial: tuple = ()

    @classmethod
    def from_row(cls, row) -> TrajectoryState:
        state = cls(row.id, row.user_id, row.session_id, row.workspace_id, row.next_seq, row.committed_seq,
                    row.event_count, row.stored_bytes, row.recording_status, row.recording_epoch,
                    meta.parse_time(row.last_activity_at), deleted=row.deleted_at is not None,
                    expired=row.content_expired_at is not None, initial_committed=row.committed_seq)
        state.initial = state._counters()
        return state

    def _counters(self) -> tuple:
        return (self.next_seq, self.committed_seq, self.event_count, self.stored_bytes, self.recording_status,
                self.recording_epoch, self.last_activity_at)

    @property
    def live(self) -> bool:
        return not self.deleted and not self.expired

    @property
    def dirty(self) -> bool:
        return self._counters() != self.initial

    def mark_written(self) -> None:
        self.initial = self._counters()

    def touch(self, moment: datetime | None) -> None:
        if moment is not None and (self.last_activity_at is None or moment > self.last_activity_at):
            self.last_activity_at = moment


class RecentSessions:
    """Root sessions seen per producer in the last 10 minutes, with their last run id (SPEC §8.6)."""

    def __init__(self):
        self.producers: dict[str, dict[tuple[str, str], tuple[float, str | None]]] = {}
        self.dirty = False

    def note(self, producer_id: str, user_id: str, session_id: str, run_id, at: float) -> None:
        sessions = self.producers.setdefault(producer_id, {})
        previous = sessions.get((user_id, session_id))
        run = run_id if isinstance(run_id, str) else (previous[1] if previous else None)
        sessions[(user_id, session_id)] = (at, run)
        self.dirty = True

    def sessions(self, producer_id: str, now: float) -> list[tuple[str, str, str | None]]:
        horizon = now - RECENT_SESSION_SECONDS
        return sorted((user, session, run) for (user, session), (seen, run)
                      in self.producers.get(producer_id, {}).items() if seen >= horizon)

    def prune(self, now: float) -> None:
        horizon = now - RECENT_SESSION_SECONDS
        for producer_id in list(self.producers):
            kept = {key: value for key, value in self.producers[producer_id].items() if value[0] >= horizon}
            if kept:
                self.producers[producer_id] = kept
            else:
                del self.producers[producer_id]

    def to_state(self) -> dict:
        return {"producers": {producer: [[user, session, seen, run] for (user, session), (seen, run) in sessions.items()]
                              for producer, sessions in self.producers.items()}}

    @classmethod
    def from_state(cls, value) -> RecentSessions:
        recent = cls()
        producers = value.get("producers") if isinstance(value, dict) else None
        for producer, entries in (producers or {}).items():
            for entry in entries if isinstance(entries, list) else ():
                if isinstance(entry, list) and len(entry) == 4 and isinstance(entry[2], (int, float)):
                    recent.producers.setdefault(producer, {})[(entry[0], entry[1])] = (float(entry[2]), entry[3])
        return recent


@dataclass
class _Backoff:
    attempts: int = 0
    next_at: float = 0.0
    #: Line counters whose new content is replaced by a not-recorded marker.
    unavailable: set[int] = field(default_factory=set)
    #: Objects already stored by an earlier attempt of this batch.
    uploaded: set[str] = field(default_factory=set)
    #: Objects that had a queued GC entry in any attempt: stored again on every attempt, never reused.
    queued: set[str] = field(default_factory=set)


@dataclass
class _FileFailure:
    """A spool file whose batch failed: its backoff, and how often the batch at ``offset`` failed in a row."""
    #: Monotonic time before which the file is not retried.
    next_at: float
    #: Failed attempts in a row, trace database outages included: they set the backoff.
    attempts: int
    #: Committed offset of the batch that failed.
    offset: int
    #: Failures in a row while the trace database answered; TRAJECTORY_INGEST_MAX_BATCH_FAILURES quarantines the file.
    failures: int


@dataclass
class PreparedBatch:
    items: list[Item]
    planner: content.ContentPlanner | None
    cache: meta.MetaCache
    #: Blob keys of this batch that had queued GC entries when it entered the object guard.
    queued: set[str] = field(default_factory=set)


# -- Service ------------------------------------------------------------------------

class IngestService:
    """Spool reader and ingest transactions of the single writer."""

    def __init__(self, settings, *, blob_store, metrics, retention=None, object_guard: ObjectGuard | None = None):
        self.settings = settings
        self.spool_dir = settings.spool_dir
        self.blob_store = blob_store
        self.metrics = metrics
        #: RetentionService-compatible ``tombstone(db, trajectory, *, reason)``; built on first use when None.
        self.retention = retention
        #: Shared with the GC deletes of this process (WorkerServices): see ``_ingest_batch``.
        self.object_guard = object_guard if object_guard is not None else ObjectGuard()
        self.hostname = socket.gethostname()
        self.boot_id = spool.boot_id()
        self.last_lag_seconds = 0.0
        self._documents: dict[str, dict | None] = {}
        self._backoff: dict[tuple[str, str, int], _Backoff] = {}
        self._provisional: OrderedDict[str, str] = OrderedDict()
        self._logged_conflicts: OrderedDict[str, None] = OrderedDict()
        self._recent: RecentSessions | None = None
        self._recent_saved = 0.0
        #: (producer_id, file name) of files whose batch failed -> backoff and failures in a row.
        self._failures: dict[tuple[str, str], _FileFailure] = {}
        #: (producer_id, file name) -> committed offset of the batch being ingested (the one a failure is about).
        self._positions: dict[tuple[str, str], int] = {}
        self.max_batch_failures = worker_setting(settings, "ingest_max_batch_failures",
                                                 "TRAJECTORY_INGEST_MAX_BATCH_FAILURES", 10)

    async def _failed(self, spool_file, scan, exc: Exception, result: dict) -> bool:
        """Back off a file whose batch failed; True when the batch failed too often and the file was quarantined.

        Only failures while the trace database answers a probe (``trace_db_available``) count: an outage keeps
        backing off without bringing any file closer to quarantine. A batch that fails
        TRAJECTORY_INGEST_MAX_BATCH_FAILURES times in a row goes to ``quarantine/`` like a file with an
        unparsable line (reason ``batch_failed`` with the last error), so the producer's later files proceed.
        """
        key = (spool_file.producer_id, spool_file.name)
        result["failed_batches"] += 1
        self._inc("failed_batches")
        offset = self._positions.get(key, 0)
        previous = self._failures.get(key)
        if previous is not None and previous.offset != offset:
            previous = None  # an earlier batch of the file committed since: this one fails for the first time
        attempts = (previous.attempts if previous is not None else 0) + 1
        failures = previous.failures if previous is not None else 0
        available = await trace_db_available()
        if available:
            failures += 1
        # The error type only in the log: database messages can carry event content.
        error_type = type(exc).__name__
        if failures >= self.max_batch_failures:
            path = spool_reader.locate(spool_file)
            parsed = ParsedBatch([], offset, "batch_failed", f"{error_type}: {exc}"[:ERROR_TEXT_LIMIT])
            if path is not None and await self._quarantine(spool_file, scan, path, offset, parsed, result):
                self._failures.pop(key, None)
                self._positions.pop(key, None)
                return True
        delay = min(BACKOFF_MAX_SECONDS, BACKOFF_FIRST_SECONDS * 2 ** min(attempts - 1, 16))
        self._failures[key] = _FileFailure(time.monotonic() + delay, attempts, offset, failures)
        log.warning("Ingest of a spool file failed; retrying in %.0f s producer_id=%s file=%s attempts=%s "
                    "failures=%s trace_db_available=%s error_type=%s", delay, key[0], key[1], attempts, failures,
                    available, error_type)
        return False

    async def run_once(self, max_lines: int | None = None) -> dict:
        """One pass over the ready spool files; counters plus ``trajectories`` (ids whose committed seq advanced)."""
        result: dict = {name: 0 for name in COUNTERS}
        result["trajectories"] = set()
        result["deleted_trajectories"] = set()
        now = time.time()
        await self._load_recent()
        scan = await asyncio.to_thread(spool_reader.scan_spool, self.spool_dir, documents=self._documents)
        self._gauge("spool_bytes", scan.bytes)
        self._gauge("spool_files", scan.files)
        self._gauge("spool_oldest_age_seconds", spool_reader.monotonic_age(scan.oldest_mtime, now))
        files = await self._file_rows(scan)
        done = {key for key, row in files.items() if row["done"]}
        await self._delete_consumed(scan, done)
        remaining = max_lines
        consumed = set(done)
        waiting: dict[tuple[str, str], float] = {}
        progressed = True
        while progressed:
            # A finished file makes the producer's next listed file eligible in the same pass;
            # a file left unfinished blocks its producer until the next pass.
            progressed = False
            for spool_file in spool_reader.select_ready(scan, consumed, now=now,
                                                        abandon_seconds=self.settings.spool_abandon_seconds):
                key = (spool_file.producer_id, spool_file.name)
                if key in waiting:
                    continue
                if remaining is not None and remaining <= 0:
                    waiting[key] = spool_file.mtime
                    continue
                failure = self._failures.get(key)
                if failure is not None and time.monotonic() < failure.next_at:
                    result["deferred_batches"] += 1
                    waiting[key] = spool_file.mtime
                    continue
                try:
                    lines, finished = await self._consume_file(spool_file, scan, files, result, remaining)
                except Exception as exc:
                    # One file's failure (a purge that times out, a value the database rejects, a bug) must
                    # not stop the other producers: the file retries from its committed offset after a backoff,
                    # and a batch that keeps failing is quarantined so the producer's later files proceed.
                    if await self._failed(spool_file, scan, exc, result):
                        consumed.add(key)
                        progressed = True
                    else:
                        waiting[key] = spool_file.mtime
                    continue
                self._failures.pop(key, None)
                if remaining is not None:
                    remaining -= lines
                if finished:
                    consumed.add(key)
                    progressed = True
                else:
                    waiting[key] = spool_file.mtime
        listed = {(producer.producer_id, item.name) for producer in scan.producers.values() for item in producer.files}
        self._failures = {key: value for key, value in self._failures.items() if key in listed}
        self._positions = {key: value for key, value in self._positions.items() if key in listed}
        await self._finish_producers(scan, result)
        await self._save_recent()
        self.last_lag_seconds = max((now - mtime for mtime in waiting.values()), default=0.0)
        self._gauge("ingest_lag_seconds", self.last_lag_seconds)
        return result

    # Files ----------------------------------------------------------------------

    async def _consume_file(self, spool_file, scan, files, result, remaining) -> tuple[int, bool]:
        """Ingest one file batch by batch: ``(lines consumed, file finished)``."""
        key = (spool_file.producer_id, spool_file.name)
        row = files.get(key)
        offset = row["bytes_consumed"] if row else 0
        consumed = 0
        while True:
            # The committed offset of the batch that follows: a failure of this file is about that batch.
            self._positions[key] = offset
            held = self._backoff.get((spool_file.producer_id, spool_file.name, offset))
            if held is not None and time.monotonic() < held.next_at:
                # Waiting for the blob store: skip reading and preparing the batch again.
                result["deferred_batches"] += 1
                return consumed, False
            path = spool_reader.locate(spool_file)
            if path is None:
                return consumed, False
            closed = path.name.endswith(spool.CLOSED_SUFFIX)
            signature = spool_reader.stat_signature(path)
            limit = self.settings.ingest_batch_lines
            if remaining is not None:
                limit = min(limit, remaining - consumed)
                if limit <= 0:
                    return consumed, False
            batch = await asyncio.to_thread(spool_reader.read_batch, path, offset, max_lines=limit,
                                            max_bytes=self.settings.ingest_batch_bytes)
            parsed = await asyncio.to_thread(parse_batch, batch.lines)
            if parsed.bad_offset is None and batch.eof and batch.tail_bytes and closed:
                # A closed file ends with a complete line; anything else is damage.
                parsed.bad_offset, parsed.bad_reason, parsed.bad_error = batch.end_offset, "torn_closed_file", "tail"
            bad = parsed.bad_offset is not None
            finished = not bad and batch.eof and (closed or self._still_abandoned(path, signature))
            end_offset = parsed.items[-1].end if parsed.items else offset
            abandoned = finished and not closed and scan.producers[spool_file.producer_id].files[-1].counter == \
                spool_file.counter
            if parsed.items or finished:
                committed = await self._ingest_batch(
                    spool_file, scan, lines=batch.lines[:len(parsed.items)], parsed=parsed, offset=offset,
                    end_offset=end_offset, finished=finished, abandoned=abandoned, torn=batch.tail_bytes > 0,
                    result=result)
                if not committed:
                    return consumed, False
                consumed += len(parsed.items)
                offset = end_offset
            if bad:
                await self._quarantine(spool_file, scan, path, offset, parsed, result)
                return consumed, True
            if finished:
                if spool_reader.remove_file(path):
                    await self._forget_files([key])
                result["files_done"] += 1
                return consumed, True
            if batch.eof:
                return consumed, False

    def _still_abandoned(self, path, signature) -> bool:
        current = spool_reader.stat_signature(path)
        return (current is not None and current == signature
                and time.time() - current[1] >= self.settings.spool_abandon_seconds)

    async def _file_rows(self, scan) -> dict[tuple[str, str], dict]:
        if not scan.producers:
            return {}
        rows = {}
        async with trace_session() as db:
            for chunk in _chunks(scan.producers):
                for row in (await db.execute(select(
                        TrajectoryIngestFile.producer_id, TrajectoryIngestFile.file_name,
                        TrajectoryIngestFile.bytes_consumed, TrajectoryIngestFile.done)
                        .where(TrajectoryIngestFile.producer_id.in_(chunk)))).all():
                    rows[(row.producer_id, row.file_name)] = {"bytes_consumed": row.bytes_consumed, "done": row.done}
        return rows

    async def _delete_consumed(self, scan, done) -> None:
        """Clean up after a crash between a commit and the file deletion, or the row cleanup that follows it."""
        removed = []
        listed = set()
        for producer in scan.producers.values():
            for spool_file in producer.files:
                key = (producer.producer_id, spool_file.name)
                listed.add(key)
                if key in done and spool_reader.remove_file(spool_reader.locate(spool_file) or spool_file.path):
                    removed.append(key)
        # A consumed file that is already gone leaves only its row behind; a producer never reuses a counter.
        removed.extend(key for key in done if key not in listed)
        await self._forget_files(removed)

    async def _forget_files(self, keys) -> None:
        if not keys:
            return
        try:
            async with trace_session() as db:
                for producer_id, name in keys:
                    await db.execute(TrajectoryIngestFile.__table__.delete().where(
                        TrajectoryIngestFile.producer_id == producer_id, TrajectoryIngestFile.file_name == name,
                        TrajectoryIngestFile.done.is_(True)))
        except Exception as exc:
            # A done row left behind only makes the next pass retry the (idempotent) deletion.
            log.warning("Ingest file bookkeeping cleanup failed error_type=%s", type(exc).__name__)

    # Batches ----------------------------------------------------------------------

    async def _ingest_batch(self, spool_file, scan, *, lines, parsed, offset, end_offset, finished, abandoned, torn,
                            result) -> bool:
        """Prepare, upload and commit one batch; False when it waits for a blob store backoff."""
        key = (spool_file.producer_id, spool_file.name, offset)
        backoff = self._backoff.get(key)
        if backoff is not None and time.monotonic() < backoff.next_at:
            result["deferred_batches"] += 1
            return False
        retries = 0
        fresh = True
        while True:
            if not fresh:
                parsed = await asyncio.to_thread(parse_batch, lines)
            fresh = False
            backoff = self._backoff.get(key)
            try:
                prepared = await self._prepare(parsed.items, backoff)
                # A batch that stores or reuses blobs excludes the GC's blob deletes (lock.ObjectGuard,
                # services.GuardedGcBlobStore) from its look at the GC queue through its commit: keys with
                # queued entries are uploaded again, overwriting, and the transaction cancels those entries.
                guard = self.object_guard.shared() if prepared.planner.object_keys() else contextlib.nullcontext()
                async with guard:
                    await self._check_queued(prepared, key)
                    await self._upload(prepared, key, result)
                    await self._commit(spool_file, scan, prepared, offset=offset, end_offset=end_offset,
                                       finished=finished, abandoned=abandoned, torn=torn, result=result)
            except UploadsDeferred:
                result["deferred_batches"] += 1
                return False
            except RetryBatch as exc:
                if exc.args and exc.args[0] == "replan":
                    continue
                retries += 1
                if retries > MAX_PREPARE_RETRIES:
                    log.warning("Ingest batch keeps changing underneath; retrying later producer_id=%s file=%s",
                                spool_file.producer_id, spool_file.name)
                    held = self._backoff.setdefault(key, _Backoff())
                    held.next_at = time.monotonic() + BACKOFF_FIRST_SECONDS
                    return False
                continue
            self._backoff.pop(key, None)
            return True

    async def _prepare(self, items: list[Item], backoff: _Backoff | None) -> PreparedBatch:
        """Lookups, media binding, content addressing and payload ids (SPEC §8.4) outside the transaction."""
        planner = content.ContentPlanner(inline_bytes=self.settings.inline_bytes, blob_key=blob_key)
        cache = meta.MetaCache()
        events = [(index, item) for index, item in enumerate(items) if item.event is not None and item.invalid is None]
        if not events:
            return PreparedBatch(items, planner, cache)
        roots = {item.event["session_id"] for _, item in events}
        async with trace_session() as db:
            rows = await self._trajectory_rows(db, roots)
            keys = await self._event_keys(db, [item.event["event_id"] for _, item in events])
            await cache.load_sessions(db, roots)
            wanted: dict[str, tuple[set, set]] = {}
            asset_ids: set[str] = set()
            for _, item in events:
                event = item.event
                row = rows.get(event["session_id"])
                root = cache.sessions.get(event["session_id"])
                item.trajectory_id = row.id if row is not None else self._provisional_id(event["session_id"])
                item.skipped = (event["event_id"] in keys or (root is not None and root.deleted) or (
                    row is not None and (row.deleted_at is not None or row.content_expired_at is not None
                                         or row.user_id != event["user_id"])))
                if item.skipped:
                    continue
                if row is not None and item.media:
                    wanted.setdefault(row.id, (set(), set()))[1].update(content.media_digests(item.media))
                asset_ids.update(content.media_sources(item.helpers.get("media_sources")).values())
                asset_ref = item.helpers.get("asset_ref")
                if isinstance(asset_ref, dict) and isinstance(asset_ref.get("asset_id"), str):
                    asset_ids.add(asset_ref["asset_id"])
            found = await self._payload_rows(db, wanted)
            asset_ids.update(row.source_asset_id for rows_of in found.values() for row in rows_of if row.source_asset_id)
            await cache.load_assets(db, asset_ids)
        contents = {tid: content.TrajectoryContent.from_rows(value) for tid, value in found.items()}
        await asyncio.to_thread(self._bind_all, events, planner, contents, cache)
        wanted = {}
        existing = {row.id for row in rows.values()}
        for _, item in events:
            if item.plan is not None and item.plan.refs and item.trajectory_id in existing:
                keys_of, digests = content.reference_keys([item.plan])
                target = wanted.setdefault(item.trajectory_id, (set(), set()))
                target[0].update(keys_of)
                target[1].update(digests)
        if wanted:
            async with trace_session() as db:
                more = await self._payload_rows(db, wanted)
            for tid, extra in more.items():
                merged = {row.payload_id: row for row in found.get(tid, [])}
                merged.update({row.payload_id: row for row in extra})
                contents[tid] = content.TrajectoryContent.from_rows(merged.values())
        unavailable = backoff.unavailable if backoff is not None else set()
        queued = frozenset(backoff.queued) if backoff is not None else frozenset()
        await asyncio.to_thread(self._assign_all, events, planner, contents, unavailable, queued)
        return PreparedBatch(items, planner, cache)

    @staticmethod
    def _bind_all(events, planner, contents, cache) -> None:
        assets = cache.known_assets()
        for _, item in events:
            if item.skipped:
                continue
            event = item.event
            root = cache.sessions.get(event["session_id"])
            workspace = event.get("workspace_id") if isinstance(event.get("workspace_id"), str) else (
                root.workspace_id if root is not None else None)
            item.plan = planner.bind(
                trajectory_id=item.trajectory_id, event_type=event["type"], data=event["data"], media=item.media,
                helpers=item.helpers, lookup=contents.get(item.trajectory_id) or content.TrajectoryContent(),
                assets=assets, owner_user_id=event["user_id"], workspace_id=workspace)

    @staticmethod
    def _assign_all(events, planner, contents, unavailable, queued=frozenset()) -> None:
        for index, item in events:
            if item.plan is not None:
                planner.assign(item.plan, index=index, trajectory_id=item.trajectory_id,
                               lookup=contents.get(item.trajectory_id) or content.TrajectoryContent(),
                               unavailable=item.n in unavailable, size_hint=item.size, queued=queued)

    async def _trajectory_rows(self, db, sessions, *, lock: bool = False) -> dict[str, SessionTrajectory]:
        found = {}
        for chunk in _chunks(sorted(sessions)):
            statement = (select(SessionTrajectory).where(SessionTrajectory.session_id.in_(chunk))
                         .order_by(SessionTrajectory.id).execution_options(populate_existing=True))
            if lock:
                # FOR NO KEY UPDATE on PostgreSQL: seq allocation is serialized per
                # trajectory while child-row foreign keys (KEY SHARE) still pass.
                statement = statement.with_for_update(key_share=True)
            for row in (await db.scalars(statement)).all():
                found[row.session_id] = row
        return found

    @staticmethod
    async def _event_keys(db, event_ids) -> dict[str, tuple[str, str]]:
        found = {}
        for chunk in _chunks(dict.fromkeys(event_ids)):
            for row in (await db.execute(select(TrajectoryEventKey.event_id, TrajectoryEventKey.trajectory_id,
                                                TrajectoryEventKey.content_hash)
                                         .where(TrajectoryEventKey.event_id.in_(chunk)))).all():
                found[row.event_id] = (row.trajectory_id, row.content_hash)
        return found

    @staticmethod
    async def _payload_rows(db, wanted: dict[str, tuple[set, set]]) -> dict[str, list[content.ExistingPayload]]:
        columns = (TrajectoryPayload.payload_id, TrajectoryPayload.dedupe_key, TrajectoryPayload.sha256,
                   TrajectoryPayload.availability, TrajectoryPayload.storage_kind, TrajectoryPayload.encoding,
                   TrajectoryPayload.stored_bytes, TrajectoryPayload.source_asset_id, TrajectoryPayload.first_seq)
        found: dict[str, dict[str, content.ExistingPayload]] = {}
        for trajectory_id, (keys, digests) in wanted.items():
            rows = found.setdefault(trajectory_id, {})
            for column, values in ((TrajectoryPayload.dedupe_key, keys), (TrajectoryPayload.sha256, digests)):
                for chunk in _chunks(sorted(values)):
                    for row in (await db.execute(select(*columns).where(
                            TrajectoryPayload.trajectory_id == trajectory_id, column.in_(chunk)))).all():
                        rows[row.payload_id] = content.ExistingPayload(*row)
        return {trajectory_id: list(rows.values()) for trajectory_id, rows in found.items()}

    def _provisional_id(self, session_id: str) -> str:
        """Stable across retries: blobs of a new trajectory are uploaded under this id before it exists."""
        identifier = self._provisional.get(session_id)
        if identifier is None:
            identifier = self._provisional[session_id] = f"trj_{uuid.uuid4().hex}"
            while len(self._provisional) > PROVISIONAL_IDS:
                self._provisional.popitem(last=False)
        else:
            self._provisional.move_to_end(session_id)
        return identifier

    # Uploads ----------------------------------------------------------------------

    async def _check_queued(self, prepared: PreparedBatch, key) -> None:
        """Keys this batch stores or reuses that have queued GC entries are uploaded again, overwriting.

        Runs inside ``ObjectGuard.shared()``: no GC delete is in progress, but an earlier one may have
        removed an object whose entry is still queued (its bookkeeping failed, or it is still in its pass),
        while an available row says the object exists. Keys queued in an earlier attempt of this batch stay
        suspect even when their entries are gone. The transaction cancels the entries of the keys it
        references, and retries the batch when it finds an entry this check did not.
        """
        planner = prepared.planner
        keys = planner.object_keys()
        backoff = self._backoff.get(key)
        if keys:
            async with trace_session() as db:
                for chunk in _chunks(sorted(keys)):
                    prepared.queued.update((await db.scalars(select(TrajectoryGcQueue.storage_key).where(
                        TrajectoryGcQueue.kind == "key", TrajectoryGcQueue.storage_key.in_(chunk)))).all())
            if backoff is not None:
                backoff.queued |= prepared.queued
            for object_key in sorted(prepared.queued | (backoff.queued & keys if backoff is not None else set())):
                planner.store_again(object_key)
        planner.release_reused()

    async def _upload(self, prepared: PreparedBatch, key, result) -> None:
        uploads = prepared.planner.uploads
        if not uploads:
            return
        backoff = self._backoff.get(key)
        uploaded = backoff.uploaded if backoff is not None else set()
        failed: set[int] = set()
        semaphore = asyncio.Semaphore(UPLOAD_CONCURRENCY)

        async def put(upload: content.Upload) -> None:
            if upload.key in uploaded and upload.if_absent:
                return  # stored by an earlier attempt, and no GC entry has doubted it since
            async with semaphore:
                try:
                    await self.blob_store.put(upload.key, upload.data, content_type=upload.content_type,
                                              if_absent=upload.if_absent)
                except Exception as exc:
                    failed.update(upload.events)
                    result["blob_put_failures"] += 1
                    self._inc("blob_put_failures")
                    log.warning("Trajectory blob upload failed error_type=%s", type(exc).__name__)
                    return
            uploaded.add(upload.key)
            result["blob_puts"] += 1
            result["blob_put_bytes"] += len(upload.data)
            self._inc("blob_puts")
            self._inc("blob_put_bytes", len(upload.data))
            self._inc("blob_put_raw_bytes", upload.raw_bytes)

        await asyncio.gather(*(put(upload) for upload in uploads.values()))
        for upload in uploads.values():
            upload.data = b""
        if not failed:
            return
        backoff = self._backoff.setdefault(key, _Backoff())
        backoff.uploaded |= uploaded
        backoff.attempts += 1
        if backoff.attempts < MAX_UPLOAD_ATTEMPTS:
            backoff.next_at = time.monotonic() + min(BACKOFF_MAX_SECONDS,
                                                     BACKOFF_FIRST_SECONDS * 2 ** (backoff.attempts - 1))
            raise UploadsDeferred()
        # Out of attempts: the affected events keep a not-recorded marker instead of their content.
        backoff.unavailable |= {prepared.items[index].n for index in failed}
        raise RetryBatch("replan")

    # Metrics ------------------------------------------------------------------------

    def _inc(self, name: str, value: int = 1) -> None:
        try:
            self.metrics.inc(name, value)
        except Exception:
            pass

    def _gauge(self, name: str, value) -> None:
        try:
            self.metrics.set_gauge(name, value)
        except Exception:
            pass


    # Transactions -------------------------------------------------------------------

    async def _commit(self, spool_file, scan, prepared: PreparedBatch, *, offset, end_offset, finished, abandoned,
                      torn, result) -> None:
        tx = _Transaction(self, prepared, producer_id=spool_file.producer_id)
        try:
            async with trace_session() as db:
                await tx.begin(db, scan, file_name=spool_file.name, offset=offset)
                for item in prepared.items:
                    await tx.apply(db, item)
                if abandoned:
                    # All files consumed, no goodbye, and the newest file was an abandoned .part.
                    await tx.producer_crashed(db)
                await tx.finish(db, end_offset=end_offset, finished=finished)
        except IntegrityError as exc:
            log.warning("Ingest transaction conflict error_type=%s producer_id=%s", type(exc).__name__,
                        spool_file.producer_id)
            raise RetryBatch() from exc
        tx.after_commit(result)
        if torn and finished and not abandoned:
            log.info("Ignored the torn last line of abandoned spool file producer_id=%s file=%s",
                     spool_file.producer_id, spool_file.name)

    async def _quarantine(self, spool_file, scan, path, offset, parsed: ParsedBatch, result) -> bool:
        """Move a file aside and report the lines it held from ``offset`` as lost (SPEC §8.2).

        Used for a file with an unparsable line and for a batch that failed too often (``_failed``). False
        when the file could not be moved.
        """
        low, high = await asyncio.to_thread(spool_reader.counter_range, path, offset)
        moved = await asyncio.to_thread(
            spool_reader.quarantine_file, self.spool_dir, spool_reader.SpoolFile(
                spool_file.producer_id, spool_file.counter, path, path.name.endswith(spool.CLOSED_SUFFIX),
                spool_file.size, spool_file.mtime),
            reason=parsed.bad_reason or "unparsable_line",
            detail={"offset": offset, "error": parsed.bad_error, "first_n": low, "last_n": high})
        if moved is None:
            log.warning("Could not quarantine spool file producer_id=%s file=%s", spool_file.producer_id,
                        spool_file.name)
            return False
        log.warning("Quarantined spool file producer_id=%s file=%s reason=%s", spool_file.producer_id,
                    spool_file.name, parsed.bad_reason)
        tx = _Transaction(self, PreparedBatch([], None, meta.MetaCache()), producer_id=spool_file.producer_id)
        try:
            async with trace_session() as db:
                await tx.begin(db, scan, file_name=spool_file.name, offset=offset)
                first = tx.producer.last_n + 1
                last = high if high is not None and high >= first else None
                await tx.report_loss(db, first, last, reason="producer_lines_lost", occurred_at=tx.now)
                if last is not None:
                    tx.producer.last_n = last
                await tx.finish(db, end_offset=offset, finished=True)
        except Exception as exc:
            log.warning("Quarantine bookkeeping failed error_type=%s", type(exc).__name__)
            return True
        tx.counters["quarantined_files"] += 1
        tx.after_commit(result)
        await self._forget_files([(spool_file.producer_id, spool_file.name)])
        return True

    async def _finish_producers(self, scan, result) -> None:
        """Remove finished producer directories; declare dead producers without goodbye abandoned once."""
        idle = [producer for producer in scan.producers.values() if not producer.files]
        if not idle:
            return
        async with trace_session() as db:
            rows = {}
            for chunk in _chunks([producer.producer_id for producer in idle]):
                for row in (await db.scalars(select(TrajectoryIngestProducer).where(
                        TrajectoryIngestProducer.producer_id.in_(chunk)))).all():
                    rows[row.producer_id] = (row.goodbye, row.abandoned)
        for producer in idle:
            goodbye, abandoned = rows.get(producer.producer_id, (False, False))
            if goodbye or abandoned:
                if await asyncio.to_thread(spool_reader.remove_producer_dir, producer.path):
                    await self._forget_producer(producer.producer_id)
                continue
            if not self._dead(producer):
                continue
            tx = _Transaction(self, PreparedBatch([], None, meta.MetaCache()), producer_id=producer.producer_id)
            try:
                async with trace_session() as db:
                    await tx.begin(db, scan)
                    await tx.producer_crashed(db)
                    await tx.finish(db)
            except Exception as exc:
                log.warning("Producer abandonment bookkeeping failed error_type=%s", type(exc).__name__)
                continue
            tx.after_commit(result)
            log.warning("Spool producer ended without goodbye producer_id=%s", producer.producer_id)
            if await asyncio.to_thread(spool_reader.remove_producer_dir, producer.path):
                await self._forget_producer(producer.producer_id)

    async def _forget_producer(self, producer_id: str) -> None:
        """A finished producer's directory is gone, so none of its file rows can be needed again."""
        try:
            async with trace_session() as db:
                await db.execute(TrajectoryIngestFile.__table__.delete().where(
                    TrajectoryIngestFile.producer_id == producer_id))
        except Exception as exc:
            log.warning("Ingest producer bookkeeping cleanup failed error_type=%s", type(exc).__name__)

    def _dead(self, producer) -> bool:
        """Whether an idle producer without goodbye has ended (SPEC §8.2).

        A running emitter refreshes the mtime of its producer.json every few seconds
        (``Emitter.HEARTBEAT_SECONDS``), so one unrefreshed for TRAJECTORY_SPOOL_ABANDON_SECONDS belongs to a
        process that is gone, wherever it ran: a recreated container never comes back under its old hostname.
        A producer of this host and boot is dead as soon as its pid is gone, and never while it is this process.
        """
        try:
            age = time.time() - os.stat(producer.path / spool.PRODUCER_FILE).st_mtime
        except OSError:
            return False
        document = producer.document if isinstance(producer.document, dict) else {}
        pid = document.get("pid")
        if (document.get("hostname") == self.hostname and document.get("boot_id") == self.boot_id
                and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
            if pid == os.getpid():
                return False
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                pass  # alive, owned by another user
        return age >= self.settings.spool_abandon_seconds

    # Worker state ------------------------------------------------------------------

    async def _load_recent(self) -> None:
        if self._recent is not None:
            return
        try:
            async with trace_session() as db:
                row = await db.get(TrajectoryWorkerState, RECENT_STATE_KEY)
                value = row.value if row is not None else None
            self._recent = RecentSessions.from_state(value)
        except Exception as exc:
            log.warning("Recent producer sessions unavailable error_type=%s", type(exc).__name__)
            self._recent = RecentSessions()
        self._recent_saved = time.monotonic()

    async def _save_recent(self, *, force: bool = False) -> None:
        recent = self._recent
        if recent is None or not recent.dirty:
            return
        if not force and time.monotonic() - self._recent_saved < RECENT_PERSIST_SECONDS:
            return
        recent.prune(time.time())
        now = datetime.now(timezone.utc)
        try:
            async with trace_session() as db:
                row = await db.get(TrajectoryWorkerState, RECENT_STATE_KEY)
                if row is None:
                    db.add(TrajectoryWorkerState(key=RECENT_STATE_KEY, value=recent.to_state(), updated_at=now))
                else:
                    row.value, row.updated_at = recent.to_state(), now
        except Exception as exc:
            log.warning("Recent producer sessions not saved error_type=%s", type(exc).__name__)
            return
        recent.dirty = False
        self._recent_saved = time.monotonic()

    async def flush_state(self) -> None:
        """Persist in-memory producer bookkeeping (worker shutdown)."""
        await self._save_recent(force=True)

    def _tombstones_committed(self, count: int) -> None:
        """Committed ``session.deleted`` tombstones, for the retention service's daily report."""
        counted = getattr(self.retention, "tombstones_committed", None)
        if counted is not None:
            counted(count)

    def retention_service(self):
        if self.retention is None:
            from trajectory.worker.retention import RetentionService
            self.retention = RetentionService(self.settings, blob_store=self.blob_store, metrics=self.metrics)
        return self.retention

    def log_conflict(self, event_id: str) -> None:
        if event_id in self._logged_conflicts:
            return
        self._logged_conflicts[event_id] = None
        while len(self._logged_conflicts) > LOGGED_CONFLICTS:
            self._logged_conflicts.popitem(last=False)
        log.warning("Trajectory event id reused with different content or trajectory event_id=%s", event_id)


class _Transaction:
    """One ingest transaction: applies a batch's lines in order and writes the rows at the end."""

    def __init__(self, service: IngestService, prepared: PreparedBatch, *, producer_id: str):
        self.service = service
        self.prepared = prepared
        self.cache = prepared.cache
        self.producer_id = producer_id
        self.now = datetime.now(timezone.utc)
        self.states: dict[str, TrajectoryState] = {}
        self.missing: set[str] = set()
        self.keys: dict[str, tuple[str, str]] = {}
        self.payloads: dict[tuple[str, str], content.ExistingPayload] = {}
        self.blobs: dict[tuple[str, str], content.ExistingPayload] = {}
        self.blocked: dict[tuple[str, str], content.ExistingPayload] = {}
        self.inserted: dict[str, str] = {}
        #: payload_id -> the lowest first_seq this transaction knows (loaded, inserted or lowered).
        self.first_seqs: dict[str, int] = {}
        #: payload_id -> (trajectory_id, first_seq) still to write.
        self.lowered: dict[str, tuple[str, int]] = {}
        self.event_rows: list[dict] = []
        self.key_rows: list[dict] = []
        self.payload_rows: list[dict] = []
        self.gc_keys: list[str] = []
        #: Blob keys of available rows this transaction references (inserted or reused).
        self.referenced: set[str] = set()
        #: (id, storage_key) of GC key entries queued before this transaction for keys of the batch.
        self.claimable: list[tuple[int, str]] = []
        self.user_bytes: dict[str, int] = {}
        self.unavailable: dict[str, list[tuple]] = {}
        self.notes: list[tuple] = []
        self.created: list[str] = []
        self.counters = {name: 0 for name in COUNTERS}
        self.lines = 0
        self.producer: TrajectoryIngestProducer | None = None
        self.file: TrajectoryIngestFile | None = None

    # Setup and completion ---------------------------------------------------------

    async def begin(self, db, scan, *, file_name: str | None = None, offset: int = 0) -> None:
        self.producer = await db.get(TrajectoryIngestProducer, self.producer_id)
        if self.producer is None:
            directory = scan.producers.get(self.producer_id)
            document = directory.document if directory is not None and isinstance(directory.document, dict) else {}
            pid = document.get("pid")
            self.producer = TrajectoryIngestProducer(
                producer_id=self.producer_id, hostname=_text(document.get("hostname"), 64),
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) and 0 <= pid < 2 ** 31 else None,
                boot_id=_text(document.get("boot_id"), 64), role=_text(document.get("role"), 32),
                started_at=meta.parse_time(document.get("started_at")), last_n=0, last_seen_at=self.now,
                goodbye=False, abandoned=False)
            db.add(self.producer)
        if file_name is not None:
            self.file = await db.get(TrajectoryIngestFile, (self.producer_id, file_name))
            if self.file is None:
                self.file = TrajectoryIngestFile(producer_id=self.producer_id, file_name=file_name, bytes_consumed=0,
                                                 lines_consumed=0, done=False, updated_at=self.now)
                db.add(self.file)
            elif self.file.bytes_consumed != offset or self.file.done:
                raise RetryBatch()
        await db.flush()
        items = self.prepared.items
        sessions: set[str] = set()
        event_ids = []
        for item in items:
            if item.event is not None and item.invalid is None:
                sessions.add(item.event["session_id"])
                event_ids.append(item.event["event_id"])
                source_root = item.helpers.get("source_root_session_id")
                if isinstance(source_root, str) and _identifier(source_root, SESSION_ID_CHARS):
                    sessions.add(source_root)
            elif isinstance(item.control, dict):
                control = item.control
                if _identifier(control.get("session_id"), SESSION_ID_CHARS):
                    sessions.add(control["session_id"])
                for entry in control.get("sessions") or () if isinstance(control.get("sessions"), list) else ():
                    if isinstance(entry, dict) and _identifier(entry.get("session_id"), SESSION_ID_CHARS):
                        sessions.add(entry["session_id"])
        for session_id, row in (await self.service._trajectory_rows(db, sessions, lock=True)).items():
            self.states[session_id] = TrajectoryState.from_row(row)
        self.missing = sessions - set(self.states)
        self.keys = await self.service._event_keys(db, event_ids)
        existing = {state.id for state in self.states.values()}
        wanted: dict[str, tuple[set, set]] = {}
        for item in items:
            if item.plan is not None and item.plan.refs and item.trajectory_id in existing:
                keys, digests = content.reference_keys([item.plan])
                target = wanted.setdefault(item.trajectory_id, (set(), set()))
                target[0].update(keys)
                target[1].update(digests)
        for trajectory_id, rows in (await self.service._payload_rows(db, wanted)).items():
            for row in rows:
                self._remember(trajectory_id, row)
        planner = self.prepared.planner
        keys = planner.object_keys() if planner is not None else set()
        for chunk in _chunks(sorted(keys)):
            self.claimable.extend((entry_id, storage_key) for entry_id, storage_key in (await db.execute(
                select(TrajectoryGcQueue.id, TrajectoryGcQueue.storage_key).where(
                    TrajectoryGcQueue.kind == "key", TrajectoryGcQueue.storage_key.in_(chunk)))).all())
        if any(storage_key not in self.prepared.queued for _, storage_key in self.claimable):
            # Queued after the batch looked at the queue: prepare it again, so those objects are stored again.
            raise RetryBatch()

    async def finish(self, db, *, end_offset: int | None = None, finished: bool = False) -> None:
        for session_id, entries in self.unavailable.items():
            state = self.states.get(session_id)
            if state is None or not state.live:
                continue
            first, last = min(entry[0] for entry in entries), max(entry[0] for entry in entries)
            await self.append_worker_event(state, event_id=gap_event_id(self.producer_id, loss_range(first, last),
                                                                        session_id),
                                           event_type="recording.gap", occurred_at=self.now, gap=True, data={
                "phase": "dropped", "reason": "blob_store_unavailable", "dropped_events": 0,
                "dropped_bytes": sum(entry[3] for entry in entries), "producer_id": self.producer_id,
                "request_ids": list(dict.fromkeys(entry[2] for entry in entries if entry[2]))[:GAP_REQUEST_IDS],
                "event_ids": [entry[1] for entry in entries][:GAP_REQUEST_IDS]})
        await self.flush(db)
        # Objects this transaction references stay stored: their queued GC entries end here (lock.ObjectGuard).
        cancelled = [entry_id for entry_id, storage_key in self.claimable if storage_key in self.referenced]
        for chunk in _chunks(cancelled):
            await db.execute(TrajectoryGcQueue.__table__.delete().where(TrajectoryGcQueue.id.in_(chunk)))
        if self.file is not None and end_offset is not None:
            self.file.bytes_consumed = end_offset
            self.file.lines_consumed = (self.file.lines_consumed or 0) + self.lines
            self.file.done = finished
            self.file.updated_at = self.now
        self.producer.last_seen_at = self.now
        await add_user_bytes(db, self.user_bytes, now=self.now)

    def after_commit(self, result: dict) -> None:
        service = self.service
        for name, value in self.counters.items():
            if value:
                result[name] += value
                if name in METRICS:
                    service._inc(METRICS[name], value)
        tombstones = 0
        for state in self.states.values():
            if state.tombstoned:
                # The tombstone published the deleted notification as this transaction committed
                # (lifecycle.publish_after_commit); publishing it here as well announced it twice.
                result["deleted_trajectories"].add(state.id)
                tombstones += 1
            elif state.committed_seq > state.initial_committed:
                result["trajectories"].add(state.id)
                publish_available(state)
        if tombstones:
            service._tombstones_committed(tombstones)
        for session_id in self.created:
            service._provisional.pop(session_id, None)
        if service._recent is not None:
            for user_id, session_id, run_id, at in self.notes:
                service._recent.note(self.producer_id, user_id, session_id, run_id, at)

    async def flush(self, db) -> None:
        """Write pending rows and trajectory counters (before controls that read them, and at the end)."""
        if self.payload_rows:
            await db.execute(insert(TrajectoryPayload), self.payload_rows)
            self.payload_rows = []
        for payload_id, (trajectory_id, first_seq) in self.lowered.items():
            # Only ever lower, so a concurrent writer that lowered further (projection) keeps its value.
            await db.execute(update(TrajectoryPayload).where(
                TrajectoryPayload.trajectory_id == trajectory_id, TrajectoryPayload.payload_id == payload_id,
                TrajectoryPayload.first_seq > first_seq).values(first_seq=first_seq)
                .execution_options(synchronize_session=False))
        self.lowered = {}
        if self.event_rows:
            await db.execute(insert(TrajectoryEvent), self.event_rows)
            self.event_rows = []
        if self.key_rows:
            await db.execute(insert(TrajectoryEventKey), self.key_rows)
            self.key_rows = []
        # A key another reference of this transaction keeps available is not garbage.
        garbage = [key for key in dict.fromkeys(self.gc_keys) if key not in self.referenced]
        if garbage:
            await db.execute(insert(TrajectoryGcQueue), [
                {"kind": "key", "storage_key": key, "reason": "content_not_referenced", "attempts": 0,
                 "next_attempt_at": self.now, "created_at": self.now} for key in garbage])
        self.gc_keys = []
        for state in self.states.values():
            if state.dirty and not state.tombstoned:
                await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == state.id).values(
                    next_seq=state.next_seq, committed_seq=state.committed_seq, event_count=state.event_count,
                    stored_bytes=state.stored_bytes, recording_status=state.recording_status,
                    recording_epoch=state.recording_epoch, last_activity_at=state.last_activity_at,
                    updated_at=self.now).execution_options(synchronize_session=False))
                state.mark_written()

    # Lines ----------------------------------------------------------------------------

    async def apply(self, db, item: Item) -> None:
        self.lines += 1
        producer = self.producer
        expected = producer.last_n + 1
        if item.n > expected:
            await self.report_loss(db, expected, item.n - 1, reason="producer_lines_lost", occurred_at=item.t)
        if item.n >= expected:
            producer.last_n = item.n
            producer.abandoned = False
        self.counters["lines"] += 1
        if item.kind == spool.KIND_CONTROL:
            self.counters["controls"] += 1
            await self._control(db, item)
        elif item.invalid is not None:
            self.counters["invalid_events"] += 1
            moment = time.monotonic()
            if moment >= _invalid_logged.get(item.invalid, 0.0):
                # One line per invalid field per minute: a broken producer must not flood the log.
                _invalid_logged[item.invalid] = moment + INVALID_LOG_SECONDS
                log.warning("Ignored an invalid spool event field=%s producer_id=%s n=%s", item.invalid,
                            self.producer_id, item.n)
        else:
            await self._event(db, item)

    async def _event(self, db, item: Item) -> None:
        event = item.event
        root, user = event["session_id"], event["user_id"]
        state = self.states.get(root)
        if state is not None and not state.live:
            self.counters["deleted_drops"] += 1
            return
        if state is not None and state.user_id != user:
            self.counters["ownership_drops"] += 1
            return
        verdict = await meta.ownership_verdict(db, self.cache, user_id=user, root_session_id=root,
                                               source_session_id=event.get("source_session_id"))
        if verdict is not None:
            self.counters["deleted_drops" if verdict == meta.DELETED else "ownership_drops"] += 1
            return
        known = self.keys.get(event["event_id"])
        if known is not None:
            if known == ((state.id if state is not None else None), item.content_hash):
                self.counters["duplicates"] += 1
            else:
                self.counters["idempotency_conflicts"] += 1
                self.service.log_conflict(event["event_id"])
            return
        if item.plan is None:
            raise RetryBatch()  # prepared as not ingestible, but the state changed since
        if state is None:
            state = await self._create(db, item)
        elif item.trajectory_id != state.id:
            raise RetryBatch()
        await self._append_event(state, item)

    async def _create(self, db, item: Item) -> TrajectoryState:
        event = item.event
        root = event["session_id"]
        root_meta = self.cache.sessions.get(root)
        workspace = event.get("workspace_id")
        if not _identifier(workspace, SESSION_ID_CHARS):
            workspace = root_meta.workspace_id if root_meta is not None and root_meta.workspace_id else ""
        occurred = item.occurred_at
        await db.execute(insert(SessionTrajectory).values(
            id=item.trajectory_id, user_id=event["user_id"], session_id=root, workspace_id=workspace,
            started_at=self.now, updated_at=self.now, last_activity_at=occurred, next_seq=1, committed_seq=0,
            projected_seq=0, archived_seq=0, checkpoint_seq=0, schema_version=TRAJECTORY_SCHEMA_VERSION,
            recording_status="recording", recording_epoch=0, event_count=0, stored_bytes=0, budget_level="normal"))
        state = TrajectoryState(item.trajectory_id, event["user_id"], root, workspace, 1, 0, 0, 0, "recording", 0,
                                occurred, created=True)
        state.mark_written()
        self.states[root] = state
        self.missing.discard(root)
        self.created.append(root)
        await self.append_worker_event(state, event_id=f"evt_start_{state.id}", event_type="trajectory.started",
                                       occurred_at=occurred, data={"existing_session": item.history,
                                                                   "coverage_start": iso(occurred),
                                                                   "schema_version": TRAJECTORY_SCHEMA_VERSION})
        return state

    async def _append_event(self, state: TrajectoryState, item: Item) -> None:
        event, plan = item.event, item.plan
        seq = state.next_seq
        for ref in plan.refs:
            self._resolve(ref, state, seq)
            if ref.failed:
                self.unavailable.setdefault(state.session_id, []).append(
                    (item.n, event["event_id"], event.get("request_id"), ref.size_bytes))
            elif ref.storage_kind == "blob" and ref.availability == "available" and ref.storage_key:
                self.referenced.add(ref.storage_key)
        self._resolve_fork(item, state)
        data = plan.data
        self.event_rows.append({
            "trajectory_id": state.id, "seq": seq, "recorded_on": self.now.date(), "event_id": event["event_id"],
            "type": event["type"], "version": event["version"], "user_id": state.user_id,
            "session_id": state.session_id, "source_session_id": event.get("source_session_id") or state.session_id,
            "request_id": event.get("request_id"), "call_id": event.get("call_id"), "agent_id": event.get("agent_id"),
            "context": {key: event[key] for key in ID_FIELDS if event.get(key) is not None}, "data": data,
            "hints": content.final_hints(plan.previews, data), "content_hash": item.content_hash,
            "occurred_at": item.occurred_at, "recorded_at": self.now})
        self._registered(state, event["event_id"], seq, item.content_hash, item.occurred_at)
        self.counters["events"] += 1
        self.user_bytes[state.user_id] = self.user_bytes.get(state.user_id, 0) + item.size
        self.notes.append((state.user_id, state.session_id, event.get("run_id"), time.time()))

    def _resolve_fork(self, item: Item, state: TrajectoryState) -> None:
        source_root = item.helpers.get("source_root_session_id")
        data = item.plan.data
        if item.event["type"] != "history.forked" or not isinstance(source_root, str) or "$payload" in data:
            return
        source = self.states.get(source_root)
        valid = source is not None and source.live and source.user_id == state.user_id
        data["source_trajectory_id"] = source.id if valid else None
        data["source_through_seq"] = str(source.committed_seq) if valid else "0"

    async def append_worker_event(self, state: TrajectoryState, *, event_id: str, event_type: str, data: dict,
                                  occurred_at: datetime, run_id: str | None = None, gap: bool = False) -> bool:
        """Append an event the worker derives (start, gaps, removals); keep-first by its deterministic id."""
        identity = {"run_id": run_id} if run_id else {}
        content_hash = content.hash_event({"user_id": state.user_id, "session_id": state.session_id,
                                           "source_session_id": state.session_id, **identity, "type": event_type,
                                           "version": 1, "data": data})
        known = self.keys.get(event_id)
        if known is not None:
            if known != (state.id, content_hash):
                self.counters["idempotency_conflicts"] += 1
                self.service.log_conflict(event_id)
            return False
        seq = state.next_seq
        self.event_rows.append({
            "trajectory_id": state.id, "seq": seq, "recorded_on": self.now.date(), "event_id": event_id,
            "type": event_type, "version": 1, "user_id": state.user_id, "session_id": state.session_id,
            "source_session_id": state.session_id, "request_id": None, "call_id": None, "agent_id": None,
            "context": identity, "data": data, "hints": None, "content_hash": content_hash,
            "occurred_at": occurred_at, "recorded_at": self.now})
        self._registered(state, event_id, seq, content_hash, occurred_at)
        if gap:
            self.counters["gaps"] += 1
        return True

    def _registered(self, state: TrajectoryState, event_id: str, seq: int, content_hash: str,
                    occurred_at: datetime | None) -> None:
        self.key_rows.append({"event_id": event_id, "trajectory_id": state.id, "seq": seq,
                              "content_hash": content_hash, "recorded_at": self.now})
        self.keys[event_id] = (state.id, content_hash)
        state.next_seq = seq + 1
        state.committed_seq = seq
        state.event_count += 1
        state.touch(occurred_at)

    # Payload rows -------------------------------------------------------------------

    def _remember(self, trajectory_id: str, row: content.ExistingPayload) -> None:
        self.payloads[(trajectory_id, row.dedupe_key)] = row
        if row.first_seq is not None:
            self.first_seqs[row.payload_id] = min(row.first_seq, self.first_seqs.get(row.payload_id, row.first_seq))
        if row.sha256 is None:
            return
        if row.storage_kind == "blob" and row.availability == "available":
            self.blobs[(trajectory_id, row.sha256)] = row
        elif row.availability != "available":
            self.blocked.setdefault((trajectory_id, row.sha256), row)

    def _visible_from(self, trajectory_id: str, payload_id: str, seq: int) -> None:
        """A reference at ``seq`` makes its row visible from there (first_seq only moves down).

        Other writers can register a row with a later first_seq than an event that references it (the
        projection stores record values under the position of its batch); readers resolve a reference only
        when ``first_seq <= H``. Availability is untouched,
        so deleted content stays deleted. The projection's ``ensure_payload_rows`` applies the same rule.
        """
        current = self.first_seqs.get(payload_id)
        if current is not None and current > seq:
            self.first_seqs[payload_id] = seq
            self.lowered[payload_id] = (trajectory_id, seq)

    def _resolve(self, ref: content.PendingRef, state: TrajectoryState, seq: int) -> None:
        """Point ``ref`` at its payload row, inserting it with ``first_seq = seq`` when new."""
        if ref.failed or ref.blocked:
            return
        if ref.payload_id in self.inserted:
            ref.fill(ref.payload_id, self.inserted[ref.payload_id])
            self._visible_from(state.id, ref.payload_id, seq)
            return
        key = (state.id, ref.dedupe_key)
        row = self.payloads.get(key)
        if row is not None:
            if row.payload_id == ref.payload_id or not ref.nested:
                ref.fill(row.payload_id, row.availability)
                self._visible_from(state.id, row.payload_id, seq)
                return
            # The id is baked into a stored blob: keep it through a second row for the same content.
            alias = hashlib.sha256(f"{ref.dedupe_key}:{ref.payload_id}".encode()).hexdigest()
            self._insert(ref, state, seq, availability=row.availability, dedupe=alias)
            return
        if ref.existing:
            raise RetryBatch()
        digest_key = (state.id, ref.sha256)
        if ref.storage_kind == "blob" and digest_key in self.blocked and digest_key not in self.blobs:
            blocked = self.blocked[digest_key]
            if not ref.nested:
                ref.blocked = True
                ref.fill(blocked.payload_id, blocked.availability)
                self._visible_from(state.id, blocked.payload_id, seq)
                return
            self._insert(ref, state, seq, availability=blocked.availability)
            return
        availability = ref.availability
        asset = self.cache.assets.get(ref.source_asset_id) if ref.source_asset_id else None
        if asset is not None and asset.deleted:
            availability = "deleted"
        self._insert(ref, state, seq, availability=availability)

    def _insert(self, ref: content.PendingRef, state: TrajectoryState, seq: int, *, availability: str,
                dedupe: str | None = None) -> None:
        blob = ref.storage_kind == "blob"
        row = content.ExistingPayload(ref.payload_id, dedupe or ref.dedupe_key, ref.sha256, availability,
                                      ref.storage_kind, ref.encoding if blob else "identity",
                                      ref.stored_bytes if blob else 0, ref.source_asset_id, seq)
        self.first_seqs[row.payload_id] = seq
        self.payload_rows.append({
            "payload_id": row.payload_id, "trajectory_id": state.id, "dedupe_key": row.dedupe_key,
            "sha256": row.sha256, "size_bytes": ref.size_bytes, "stored_bytes": row.stored_bytes,
            "media_type": ref.media_type, "encoding": row.encoding, "storage_kind": row.storage_kind,
            "storage_key": ref.storage_key, "source_asset_id": row.source_asset_id, "availability": availability,
            "first_seq": seq, "created_at": self.now, "deleted_at": None if availability == "available" else self.now})
        if blob:
            digest_key = (state.id, ref.sha256)
            if availability == "available":
                if digest_key not in self.blobs:
                    state.stored_bytes += row.stored_bytes
                self.blobs[digest_key] = row
            elif digest_key not in self.blobs:
                # Uploaded for content that is already gone: nothing may keep the object.
                self.gc_keys.append(ref.storage_key)
        self.payloads.setdefault((state.id, row.dedupe_key), row)
        self.inserted[ref.payload_id] = availability
        ref.fill(ref.payload_id, availability)

    # Controls ---------------------------------------------------------------------------

    async def _control(self, db, item: Item) -> None:
        control = item.control
        control_type = control.get("type")
        if control_type in meta.META_CONTROLS:
            await meta.apply_meta(db, self.cache, control_type, control, line_time=item.t, now=self.now)
        elif control_type == "gap":
            await self._gap(db, item)
        elif control_type == "session.deleted":
            await self._session_deleted(db, item)
        elif control_type == "asset.deleted":
            await self._asset_deleted(db, item)
        elif control_type == "recording.state":
            await self._recording_state(db, item)
        elif control_type == "producer.goodbye":
            self.producer.goodbye = True
        # Unknown control types come from newer producers and are ignored.

    async def state(self, db, session_id: str) -> TrajectoryState | None:
        if session_id in self.states:
            return self.states[session_id]
        if session_id in self.missing:
            return None
        rows = await self.service._trajectory_rows(db, [session_id], lock=True)
        row = rows.get(session_id)
        if row is None:
            self.missing.add(session_id)
            return None
        self.states[session_id] = TrajectoryState.from_row(row)
        return self.states[session_id]

    async def _state_by_id(self, db, trajectory_id: str) -> TrajectoryState | None:
        for state in self.states.values():
            if state.id == trajectory_id:
                return state
        row = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.id == trajectory_id)
                              .with_for_update(key_share=True).execution_options(populate_existing=True))
        if row is None:
            return None
        self.states[row.session_id] = TrajectoryState.from_row(row)
        return self.states[row.session_id]

    async def _gap(self, db, item: Item) -> None:
        control = item.control
        sessions = control.get("sessions")
        if not isinstance(sessions, list):
            return
        occurred = meta.parse_time(control.get("last_dropped_at")) or item.t
        base = {"phase": "dropped", "reason": control.get("reason") if isinstance(control.get("reason"), str) else None,
                "dropped_events": _count(control.get("dropped_events")),
                "dropped_bytes": _count(control.get("dropped_bytes")), "producer_id": self.producer_id}
        for entry in sessions[:spool.GAP_MAX_SESSIONS]:
            if not isinstance(entry, dict) or not _identifier(entry.get("session_id"), SESSION_ID_CHARS):
                continue
            session_id = entry["session_id"]
            state = await self.state(db, session_id)
            if state is None or not state.live or state.user_id != entry.get("user_id"):
                continue
            request_ids = [value for value in entry.get("request_ids") or [] if _identifier(value, REQUEST_ID_CHARS)]
            data = {**base, "request_ids": request_ids[:GAP_REQUEST_IDS]}
            run_ids = [value for value in entry.get("run_ids") or [] if _identifier(value, REQUEST_ID_CHARS)]
            for run_id in list(dict.fromkeys(run_ids))[:GAP_RUN_IDS]:
                await self.append_worker_event(state, event_id=gap_event_id(self.producer_id, str(item.n), session_id,
                                                                            run_id),
                                               event_type="recording.gap", data=data, occurred_at=occurred,
                                               run_id=run_id, gap=True)
            await self.append_worker_event(state, event_id=gap_event_id(self.producer_id, str(item.n), session_id),
                                           event_type="recording.gap", data=data, occurred_at=occurred, gap=True)

    async def report_loss(self, db, first: int, last: int | None, *, reason: str, occurred_at: datetime) -> None:
        """``recording.gap {phase: lost}`` for the sessions this producer served in the last 10 minutes."""
        self.counters["producer_losses"] += 1
        sessions: dict[tuple[str, str], str | None] = {}
        recent = self.service._recent
        if recent is not None:
            for user_id, session_id, run_id in recent.sessions(self.producer_id, time.time()):
                sessions[(user_id, session_id)] = run_id
        for user_id, session_id, run_id, _ in self.notes:
            previous = sessions.get((user_id, session_id))
            sessions[(user_id, session_id)] = run_id if isinstance(run_id, str) else previous
        log.warning("Spool producer lost lines producer_id=%s from_n=%s to_n=%s reason=%s", self.producer_id, first,
                    last, reason)
        for (user_id, session_id), run_id in sorted(sessions.items(), key=lambda entry: entry[0]):
            state = await self.state(db, session_id)
            if state is None or not state.live or state.user_id != user_id:
                continue
            await self.append_worker_event(
                state, event_id=gap_event_id(self.producer_id, loss_range(first, last), session_id, run_id),
                event_type="recording.gap", occurred_at=occurred_at, run_id=run_id, gap=True,
                data={"phase": "lost", "reason": reason, "producer_id": self.producer_id, "from_n": first,
                      "to_n": last})

    async def producer_crashed(self, db) -> None:
        if self.producer.goodbye or self.producer.abandoned:
            return
        await self.report_loss(db, self.producer.last_n + 1, None, reason="producer_crashed", occurred_at=self.now)
        self.producer.abandoned = True

    async def _session_deleted(self, db, item: Item) -> None:
        control = item.control
        session_id, user_id = control.get("session_id"), control.get("user_id")
        if not _identifier(session_id, SESSION_ID_CHARS):
            return
        deleted_at = meta.parse_time(control.get("deleted_at")) or item.t
        await meta.mark_session_deleted(db, self.cache, session_id=session_id, user_id=user_id, deleted_at=deleted_at,
                                        now=self.now)
        state = await self.state(db, session_id)
        if state is None or state.deleted or (isinstance(user_id, str) and state.user_id != user_id):
            return
        # Rows written so far in this batch must go with the tombstone.
        await self.flush(db)
        row = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.id == state.id)
                              .execution_options(populate_existing=True))
        await self.service.retention_service().tombstone(db, row, reason="session_deleted")
        state.deleted = state.tombstoned = True
        state.recording_status = "deleted"
        state.mark_written()

    async def _asset_deleted(self, db, item: Item) -> None:
        control = item.control
        asset_id = control.get("asset_id")
        if not _identifier(asset_id, SESSION_ID_CHARS):
            return
        deleted_at = meta.parse_time(control.get("deleted_at")) or item.t
        await self.flush(db)
        await meta.mark_asset_deleted(db, self.cache, asset_id=asset_id, user_id=control.get("user_id"),
                                      deleted_at=deleted_at, now=self.now)
        revocations = await revoke_asset(db, asset_id, at=self.now)
        for key, row in list(self.payloads.items()):
            if row.source_asset_id == asset_id and row.availability != "deleted":
                gone = content.ExistingPayload(row.payload_id, row.dedupe_key, row.sha256, "deleted", row.storage_kind,
                                               row.encoding, row.stored_bytes, row.source_asset_id, row.first_seq)
                self.payloads[key] = gone
                if row.sha256 is not None:
                    digest_key = (key[0], row.sha256)
                    if self.blobs.get(digest_key) is row:
                        del self.blobs[digest_key]
                    self.blocked.setdefault(digest_key, gone)
        for revocation in revocations:
            state = await self._state_by_id(db, revocation.trajectory.id)
            if state is None or not state.live:
                continue
            await self.append_worker_event(state, event_id=revocation.event_id, event_type="artifact.removed",
                                           occurred_at=deleted_at, data=revocation.data)

    async def _recording_state(self, db, item: Item) -> None:
        control = item.control
        session_id, user_id, value = control.get("session_id"), control.get("user_id"), control.get("state")
        if not _identifier(session_id, SESSION_ID_CHARS) or value not in (meta.PAUSED, meta.RESUMED):
            return
        state = await self.state(db, session_id)
        if state is None or not state.live or (isinstance(user_id, str) and state.user_id != user_id):
            return
        # Producers suppress repeated pause and resume controls per process only, so several processes
        # report the same transition, and a control from a crashed producer's abandoned file can arrive
        # after a newer one. The control's epoch, or without one the trajectory's own state, makes them
        # idempotent (meta.recording_transition): a duplicate or stale control adds no gap and no epoch.
        epoch = meta.recording_transition(value, meta.control_epoch(control), status=state.recording_status,
                                          current_epoch=state.recording_epoch)
        if epoch is None:
            return
        if value == meta.PAUSED:
            data = {"phase": "paused", "reason": "recording_disabled", "last_recorded_seq": str(state.committed_seq)}
        else:
            data = {"phase": "resumed", "reason": "recording_reenabled",
                    "previous_committed_seq": str(state.committed_seq)}
        if await self.append_worker_event(state, event_id=gap_event_id(self.producer_id, str(item.n), session_id),
                                          event_type="recording.gap", data=data, gap=True,
                                          occurred_at=meta.parse_time(control.get("at")) or item.t):
            state.recording_status = "paused" if value == meta.PAUSED else "gap"
            state.recording_epoch = epoch


def _text(value, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
