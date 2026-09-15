"""Fail-open trajectory emitter: bounded queue, writer thread, JSONL spool.

Business code only validates, serializes and enqueues (SPEC §5). One writer
thread per process owns line counters, file I/O, rotation, the spool budget,
blob files for large values (SPEC §3) and budget-file refreshes. Nothing here
raises into callers: every loss is counted and reported to the worker through
``gap`` control lines.
"""
from __future__ import annotations

import asyncio
import atexit
import collections
import concurrent.futures
from datetime import date, datetime, time as time_of_day
import errno
import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import orjson
from pydantic import BaseModel
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SyncSession

from trajectory import spool
from trajectory.budget import DROP_BUDGET, NORMAL, BudgetReader, filter_event
from trajectory.config import emitter_settings, enabled, integer, pipeline_off
from trajectory.context import TraceContext, current
from trajectory.types import TrajectoryError, prepare_fast

log = logging.getLogger(__name__)

PENDING_KEY = "trajectory_pending_emits"
EVENT, CONTROL = 0, 1
# Writer-owned lifecycle records are never accepted from callers.
CALLER_CONTROL_TYPES = frozenset(spool.CONTROL_TYPES) - {"gap", "producer.goodbye"}
_BINARY = {"availability": "not_recorded", "reason": "binary_value"}
_UNSUPPORTED = {"availability": "not_recorded", "reason": "unsupported_value"}


def _json_default(value):
    if isinstance(value, BaseModel):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return _UNSUPPORTED
    if isinstance(value, (datetime, date, time_of_day)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _BINARY
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return _UNSUPPORTED


class _LogLimiter:
    def __init__(self, interval: float = 60.0):
        self.interval = interval
        self._next: dict = {}
        self._lock = threading.Lock()

    def allow(self, key) -> bool:
        now = time.monotonic()
        with self._lock:
            if now < self._next.get(key, float("-inf")):
                return False
            self._next[key] = now + self.interval
            return True


_log_limiter = _LogLimiter()


IDENTITY_MAX_CHARS = 128   # the identity limit enforced by types.prepare()


def _listable(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= IDENTITY_MAX_CHARS


class _DropWindow:
    """Drops of one reason since the last gap line written for that reason."""
    __slots__ = ("events", "bytes", "first", "last", "sessions")

    def __init__(self, at: float):
        self.events = 0
        self.bytes = 0
        self.first = at
        self.last = at
        self.sessions: dict[tuple, tuple[dict, dict]] = {}

    def add(self, size: int, at: float, user_id, session_id, run_id, request_id) -> None:
        self.events += 1
        self.bytes += size
        self.first = min(self.first, at)
        self.last = max(self.last, at)
        self._note(user_id, session_id, (run_id,), (request_id,))

    def _note(self, user_id, session_id, run_ids, request_ids) -> None:
        # Only valid identities are listed. Anything else from a malformed caller
        # (an int beyond 64 bits, an unhashable value) is counted but never
        # listed, so it cannot make the whole gap control unserializable.
        if not _listable(user_id) or not _listable(session_id):
            return
        entry = self.sessions.get((user_id, session_id))
        if entry is None:
            if len(self.sessions) >= spool.GAP_MAX_SESSIONS:
                return
            entry = self.sessions[(user_id, session_id)] = ({}, {})
        runs, requests = entry
        for run_id in run_ids:
            if _listable(run_id) and len(runs) < spool.GAP_MAX_RUN_IDS:
                runs[run_id] = None
        for request_id in request_ids:
            if _listable(request_id) and len(requests) < spool.GAP_MAX_REQUEST_IDS:
                requests[request_id] = None

    def merge(self, newer: "_DropWindow") -> None:
        self.events += newer.events
        self.bytes += newer.bytes
        self.first = min(self.first, newer.first)
        self.last = max(self.last, newer.last)
        for (user_id, session_id), (runs, requests) in newer.sessions.items():
            self._note(user_id, session_id, runs, requests)

    def control(self, reason: str) -> dict:
        return {"type": "gap", "reason": reason, "dropped_events": self.events,
                "dropped_bytes": self.bytes, "first_dropped_at": spool.timestamp(self.first),
                "last_dropped_at": spool.timestamp(self.last),
                "sessions": [{"user_id": user_id, "session_id": session_id,
                              "run_ids": list(runs), "request_ids": list(requests)}
                             for (user_id, session_id), (runs, requests) in self.sessions.items()]}


VALUE_MAX_DEPTH = 6
#: ``request.prepared`` inputs moved whole, and input lists whose items are moved one by one.
REQUEST_VALUES = ("system", "instructions", "tools")
REQUEST_ITEMS = ("messages", "input")
#: Blobs the writer stored or refreshed recently; their mtime is refreshed at most every BLOB_REFRESH_SECONDS.
BLOB_MEMORY = 8192


def externalize(payload: bytes, *, min_bytes: int,
                value_bytes: int = spool.BLOB_VALUE_BYTES) -> tuple[bytes, dict[str, bytes]] | None:
    """``(event JSON with blob references, {sha256: blob content})``, or ``None`` to write the event inline.

    Moved (SPEC §3.2): the ``input.system``, ``instructions`` and ``tools`` of ``request.prepared``
    and each item of its ``input.messages`` or list-valued ``input`` larger than ``min_bytes``; then
    any other value inside ``data`` larger than ``value_bytes``, leaves first and at most six levels
    deep. A value that holds a reference stays inline, so blobs never nest. A blob is the value's
    compact JSON, exactly as it appears inline. An event that contains ``"$blob"`` itself is never
    changed: in a version 2 line those bytes are references only.
    """
    size = len(payload)
    request = size > min_bytes and b'"request.prepared"' in payload
    if (not request and size <= value_bytes) or b'"$blob"' in payload:
        return None
    try:
        event = orjson.loads(payload)
    except orjson.JSONDecodeError:
        return None
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return None
    blobs: dict[str, bytes] = {}

    def reference(body: bytes) -> dict:
        sha = hashlib.sha256(body).hexdigest()
        blobs.setdefault(sha, body)
        return {spool.BLOB_KEY: sha}

    inputs = data.get("input")
    if request and event.get("type") == "request.prepared" and isinstance(inputs, dict):
        for key in REQUEST_VALUES:
            if inputs.get(key) is not None:
                body = orjson.dumps(inputs[key])
                if len(body) > min_bytes:
                    inputs[key] = reference(body)
        for key in REQUEST_ITEMS:
            items = inputs.get(key)
            if isinstance(items, list):
                for position, item in enumerate(items):
                    body = orjson.dumps(item)
                    if len(body) > min_bytes:
                        items[position] = reference(body)

    def shrink(value, depth: int):
        """``(value, holds a reference)`` with the parts over ``value_bytes`` moved out."""
        if isinstance(value, str):
            if len(value) * 6 + 2 <= value_bytes:  # at most six bytes per character
                return value, False
            body = orjson.dumps(value)
        elif isinstance(value, (dict, list)):
            body = orjson.dumps(value)
            if len(body) <= value_bytes:
                return value, spool.BLOB_REFERENCE in body
            if depth < VALUE_MAX_DEPTH:
                holds = False
                for key, child in list(value.items() if isinstance(value, dict) else enumerate(value)):
                    value[key], moved = shrink(child, depth + 1)
                    holds = holds or moved
                if holds:
                    return value, True
            elif spool.BLOB_REFERENCE in body:
                return value, True
        else:
            return value, False
        if depth == 0 or len(body) <= value_bytes:
            return value, False
        return reference(body), True

    if size > value_bytes:
        shrink(data, 0)
    if not blobs:
        return None
    return orjson.dumps(event), blobs


def _fsync_directory(path) -> bool:
    try:
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        return False
    return True


class Emitter:
    WAIT_SECONDS = 0.1
    GAP_INTERVAL_SECONDS = 0.1
    #: The writer rescans producers/ this often, and blobs/ plus quarantine/ (they can hold many files) only every
    #: SHARED_SAMPLE_SECONDS; its own blob writes are added in between.
    SPOOL_SAMPLE_SECONDS = 5.0
    SHARED_SAMPLE_SECONDS = 60.0
    #: Writer-owned lines (gap controls, producer.goodbye) may exceed spool_max_bytes by this much: without its
    #: goodbye, a producer restarted while the spool is full is reported as crashed for every recent session.
    CONTROL_RESERVE_BYTES = 1024 * 1024
    #: The writer refreshes the mtime of producer.json this often: the worker declares a producer whose
    #: producer.json stays unrefreshed for TRAJECTORY_SPOOL_ABANDON_SECONDS dead, on any host.
    HEARTBEAT_SECONDS = 5.0
    RETRY_SECONDS = 1.0
    ALIVE_CHECK_SECONDS = 1.0
    WRITE_CHUNK_BYTES = 1024 * 1024

    def __init__(self, spool_dir: Path, *, role: str = "backend", queue_bytes: int, max_event_bytes: int,
                 file_bytes: int, file_ms: int, spool_max_bytes: int, budget_refresh_ms: int = 5000,
                 blob_min_bytes: int = spool.BLOB_MIN_BYTES):
        self.spool_dir = Path(spool_dir)
        self.role = role
        self.queue_bytes = max(1, int(queue_bytes))
        self.max_event_bytes = max(1, int(max_event_bytes))
        self.file_bytes = max(1, int(file_bytes))
        self.file_seconds = max(1, int(file_ms)) / 1000
        self.spool_max_bytes = max(1, int(spool_max_bytes))
        self.blob_min_bytes = max(1, int(blob_min_bytes))
        self.blob_dir = spool.blobs_dir(self.spool_dir)
        self.started_at = time.time()
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.producer_id = spool.producer_id(self.started_at, self.hostname, self.pid, uuid.uuid4().hex)
        self.producer_dir = self.spool_dir / spool.PRODUCERS_DIR / self.producer_id
        self.budgets = BudgetReader(spool.budgets_path(self.spool_dir), budget_refresh_ms)
        # File-system primitives are attributes so tests can inject faults.
        self._write = os.write
        self._fsync = os.fsync
        self._rename = os.replace
        self._truncate = os.ftruncate

        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._done = threading.Condition(self._lock)
        self._queue: collections.deque = collections.deque()
        self._state = "new"
        self._thread: threading.Thread | None = None
        self._forked = False
        self._waiting = False
        self._wake_bytes = max(1, min(self.queue_bytes // 4, 4 * 1024 * 1024))
        self._next_alive_check = 0.0
        self._queued_bytes = 0
        self._queued_lines = 0
        self._flush_seq = 0
        self._flush_done = 0
        self._drops: dict[str, _DropWindow] = {}
        self._writer_drops = 0
        self._written_lines = 0
        self._dropped_events = 0
        self._dropped_bytes = 0
        self._dropped_by_reason: dict[str, int] = {}
        self._files_closed = 0
        #: Open files the worker consumed and deleted before this writer closed them.
        self._files_lost = 0
        #: Writer-owned lines refused at the spool cap plus reserve or without a file; a refused gap line is retried.
        self._writer_lines_skipped = 0
        self._gap_lines = 0
        self._gaps_in_flight = 0
        self._filtered_events = 0
        self._truncated_events = 0
        self._write_errors = 0
        self._rejected_after_close = 0
        self._restarts = 0
        self._last_error: str | None = None
        # Writer thread state; a restarted writer repairs it in _recover().
        self._ready = False
        self._n = 1
        self._counter = 0
        self._fd: int | None = None
        self._part_path: Path | None = None
        self._file_size = 0
        self._file_opened = 0.0
        self._retry_at = 0.0
        self._next_sample = 0.0
        self._next_shared_sample = 0.0
        self._next_heartbeat = 0.0
        self._spool_estimate = 0
        #: The blobs/ and quarantine/ part of _spool_estimate: the last rescan plus this writer's blob writes since.
        self._shared_estimate = 0
        self._last_gap = float("-inf")
        self._inflight: collections.deque = collections.deque()
        self._unreleased_bytes = 0
        self._unreleased_lines = 0
        self._buffer: list[bytes] = []
        self._meta: list[tuple] = []
        self._buffer_bytes = 0
        #: sha256 -> monotonic time this writer last stored or refreshed the blob.
        self._blobs_seen: collections.OrderedDict[str, float] = collections.OrderedDict()
        #: Blob files were renamed into place since the blobs directory was last synced.
        self._blobs_unsynced = False
        self._blobs_written = 0
        self._blob_bytes_written = 0
        self._blob_errors = 0

    # Caller side -----------------------------------------------------------

    def start(self) -> None:
        """Idempotent: producer directory, producer.json, writer thread."""
        try:
            with self._lock:
                if self._state != "new":
                    if self._state == "running":
                        self._restart_if_dead_locked()
                    return
            # The state leaves "new" only together with the thread start, so a
            # concurrent emit cannot start a second writer and a concurrent
            # close() cannot be followed by a writer on a closed emitter.
            self._prepare_directory()
            try:
                self.budgets.maybe_refresh(force=True)
            except Exception as exc:
                self._note_error(exc)
            with self._lock:
                if self._state != "new":
                    return
                self._state = "running"
                try:
                    self._start_thread_locked()
                except Exception as exc:
                    # Retried by the liveness checks in emit, flush and close.
                    self._note_error(exc)
            atexit.register(self._close_at_exit)
        except Exception as exc:
            self._note_error(exc)

    def emit_bytes(self, event_json: bytes, *, user_id: str, session_id: str,
                   run_id: str | None = None, request_id: str | None = None) -> bool:
        """Enqueue one serialized event; ``False`` when it was dropped."""
        try:
            return self._enqueue(EVENT, event_json, user_id, session_id, run_id, request_id)
        except Exception as exc:
            self._note_error(exc)
            return False

    def emit_control(self, control: dict) -> bool:
        try:
            payload = self.encode_control(control)
            return payload is not None and self._enqueue(CONTROL, payload, *control_routing(control))
        except Exception as exc:
            self._note_error(exc)
            return False

    def encode_control(self, control: dict) -> bytes | None:
        routing = control_routing(control)
        if not isinstance(control, dict) or control.get("type") not in CALLER_CONTROL_TYPES:
            self.drop("invalid_event", 0, *routing)
            return None
        try:
            return orjson.dumps(control, default=_json_default, option=orjson.OPT_NON_STR_KEYS)
        except Exception:
            self.drop("serialization_failed", 0, *routing)
            return None

    def enqueue_encoded(self, kind: int, payload: bytes, routing: tuple) -> bool:
        """Enqueue a line body serialized earlier (after-commit emits)."""
        try:
            return self._enqueue(kind, payload, *routing)
        except Exception as exc:
            self._note_error(exc)
            return False

    def _enqueue(self, kind, payload, user_id, session_id, run_id, request_id) -> bool:
        if not isinstance(payload, bytes):
            self.drop("invalid_event", 0, user_id, session_id, run_id, request_id)
            return False
        size = len(payload)
        if size > self.max_event_bytes:
            self.drop("event_too_large", size, user_id, session_id, run_id, request_id)
            return False
        # Serialized objects only: a newline would split the spool line.
        if not payload.startswith(b"{") or b"\n" in payload:
            self.drop("invalid_event", size, user_id, session_id, run_id, request_id)
            return False
        at = time.time()
        with self._lock:
            if self._state not in ("new", "running"):
                self._rejected_after_close += 1
                return False
            if at >= self._next_alive_check:
                self._next_alive_check = at + self.ALIVE_CHECK_SECONDS
                self._restart_if_dead_locked()
            if self._queued_bytes + size > self.queue_bytes:
                self._drop_locked("queue_overflow", size, at, user_id, session_id, run_id, request_id)
                return False
            self._queue.append((kind, payload, at, user_id, session_id, run_id, request_id))
            self._queued_bytes += size
            self._queued_lines += 1
            # The writer drains on its own timer; wake it early only under load.
            if self._waiting and self._queued_bytes >= self._wake_bytes:
                self._wake.notify()
        return True

    def drop(self, reason: str, size: int = 0, user_id=None, session_id=None, run_id=None, request_id=None) -> None:
        """Count a line that will not be written; the next gap control reports it."""
        try:
            with self._lock:
                self._drop_locked(reason, size, time.time(), user_id, session_id, run_id, request_id)
        except Exception:
            return

    def _drop_locked(self, reason, size, at, user_id, session_id, run_id, request_id) -> None:
        self._dropped_events += 1
        self._dropped_bytes += size
        self._dropped_by_reason[reason] = self._dropped_by_reason.get(reason, 0) + 1
        window = self._drops.get(reason)
        if window is None:
            window = self._drops[reason] = _DropWindow(at)
        window.add(size, at, user_id, session_id, run_id, request_id)

    def count_filtered(self, *, truncated: bool = False) -> None:
        with self._lock:
            if truncated:
                self._truncated_events += 1
            else:
                self._filtered_events += 1

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything enqueued so far is written and the file rotated."""
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._lock:
                if self._state == "new":
                    return False
                if self._state == "closed":
                    return not self._queue
                self._flush_seq += 1
                sequence = self._flush_seq
                drops = self._writer_drops
                self._restart_if_dead_locked()
                if self._waiting:
                    self._wake.notify()
                while self._flush_done < sequence:
                    remaining = deadline - time.monotonic()
                    thread = self._thread
                    if remaining <= 0 or thread is None or not thread.is_alive():
                        return False
                    self._done.wait(min(remaining, self.WAIT_SECONDS))
                return self._writer_drops == drops
        except Exception as exc:
            self._note_error(exc)
            return False

    def close(self, timeout: float = 5.0) -> None:
        """Flush, write producer.goodbye, rotate and stop the writer."""
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._lock:
                if self._forked:
                    return
                if self._state == "new":
                    self._state = "closed"
                    return
                if self._state == "running":
                    self._restart_if_dead_locked()
                    self._state = "closing"
                    self._wake.notify()
                thread = self._thread
                while self._state != "closed":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or thread is None or not thread.is_alive():
                        break
                    self._done.wait(min(remaining, self.WAIT_SECONDS))
                closed = self._state == "closed"
            if closed and thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))
            else:
                log.warning("Trajectory emitter close timed out producer_id=%s", self.producer_id)
        except Exception as exc:
            self._note_error(exc)
        finally:
            try:
                atexit.unregister(self._close_at_exit)
            except Exception:
                pass

    def _close_at_exit(self) -> None:
        if not self._forked:
            self.close(2.0)

    def stats(self) -> dict:
        with self._lock:
            thread = self._thread
            return {"producer_id": self.producer_id, "state": self._state,
                    "writer_alive": thread is not None and thread.is_alive(),
                    "queued_bytes": self._queued_bytes, "queued_lines": self._queued_lines,
                    "written_lines": self._written_lines, "dropped_events": self._dropped_events,
                    "dropped_bytes": self._dropped_bytes, "dropped_by_reason": dict(self._dropped_by_reason),
                    "files_closed": self._files_closed, "files_lost": self._files_lost,
                    "last_error": self._last_error,
                    "spool_bytes": self._spool_estimate, "gap_lines": self._gap_lines,
                    "pending_gap_reasons": list(self._drops),
                    "pending_gaps": len(self._drops) + self._gaps_in_flight,
                    "filtered_events": self._filtered_events,
                    "truncated_events": self._truncated_events, "write_errors": self._write_errors,
                    "writer_lines_skipped": self._writer_lines_skipped,
                    "rejected_after_close": self._rejected_after_close, "writer_restarts": self._restarts,
                    "blobs_written": self._blobs_written, "blob_bytes_written": self._blob_bytes_written,
                    "blob_errors": self._blob_errors}

    def _note_error(self, exc: BaseException) -> None:
        code = errno.errorcode.get(getattr(exc, "errno", None) or 0)
        self._last_error = f"{type(exc).__name__}:{code}" if code else type(exc).__name__
        if _log_limiter.allow(("emitter", self._last_error)):
            log.warning("Trajectory emitter error=%s producer_id=%s", self._last_error, self.producer_id)

    def _start_thread_locked(self) -> None:
        thread = threading.Thread(target=self._run, name=f"trajectory-emitter-{self.pid}", daemon=True)
        self._thread = thread
        thread.start()

    def _restart_if_dead_locked(self) -> None:
        thread = self._thread
        if self._state != "running" or self._forked or (thread is not None and thread.is_alive()):
            return
        try:
            self._restarts += 1
            self._start_thread_locked()
        except Exception as exc:
            self._note_error(exc)

    # Writer thread ---------------------------------------------------------

    def _run(self) -> None:
        self._recover()
        while True:
            try:
                if self._iterate():
                    return
            except Exception as exc:
                # A bug in one cycle must not stop recording for the process.
                self._note_error(exc)
                try:
                    self._recover()
                except Exception as nested:
                    self._note_error(nested)
                time.sleep(0.05)

    def _iterate(self) -> bool:
        """One drain cycle; True after the goodbye line was handled."""
        with self._lock:
            if self._state == "closed":
                # Nothing can be written after producer.goodbye; never spin here.
                return True
            if not self._queue and self._state == "running" and self._flush_seq == self._flush_done:
                self._waiting = True
                self._wake.wait(self._wait_timeout())
                self._waiting = False
            if self._queue:
                self._inflight, self._queue = self._queue, collections.deque()
            flush_seq = self._flush_seq
            closing = self._state == "closing"
        self._maintain(time.monotonic())
        self._write_batch()
        self._write_gaps(closing)
        self._write_buffer()
        if flush_seq != self._flush_done or closing:
            self._rotate()
        elif self._fd is not None and time.monotonic() - self._file_opened >= self.file_seconds:
            self._rotate()
        if flush_seq != self._flush_done:
            with self._lock:
                self._flush_done = flush_seq
                self._done.notify_all()
        if not closing:
            return False
        with self._lock:
            if self._queue:
                return False
        self._append(CONTROL, orjson.dumps({"type": "producer.goodbye", "last_n": self._n}), time.time(), None)
        self._rotate()
        with self._lock:
            self._state = "closed"
            self._flush_done = self._flush_seq
            self._done.notify_all()
        return True

    def _wait_timeout(self) -> float:
        now = time.monotonic()
        timeout = self.WAIT_SECONDS
        if self._fd is not None:
            timeout = min(timeout, self._file_opened + self.file_seconds - now)
        if self._drops:
            timeout = min(timeout, self._last_gap + self.GAP_INTERVAL_SECONDS - now)
        return max(0.001, timeout)

    def _maintain(self, now: float) -> None:
        try:
            self.budgets.maybe_refresh()
        except Exception as exc:
            self._note_error(exc)
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + self.HEARTBEAT_SECONDS
            self._heartbeat()
        if now >= self._next_sample:
            self._next_sample = now + self.SPOOL_SAMPLE_SECONDS
            if now >= self._next_shared_sample:
                self._next_shared_sample = now + self.SHARED_SAMPLE_SECONDS
                try:
                    self._shared_estimate = spool.shared_usage_bytes(self.spool_dir)
                except Exception as exc:
                    self._note_error(exc)
            try:
                self._spool_estimate = spool.producer_usage_bytes(self.spool_dir) + self._shared_estimate
            except Exception as exc:
                self._note_error(exc)

    def _heartbeat(self) -> None:
        """Refresh the mtime of producer.json (``HEARTBEAT_SECONDS``); never raises."""
        try:
            os.utime(self.producer_dir / spool.PRODUCER_FILE)
        except Exception:
            pass

    def _write_batch(self) -> None:
        batch = self._inflight
        while batch:
            kind, payload, at, user_id, session_id, run_id, request_id = batch[0]
            self._unreleased_bytes += len(payload)
            self._unreleased_lines += 1
            batch.popleft()
            self._append(kind, payload, at, (user_id, session_id, run_id, request_id))
            if self._buffer_bytes >= self.WRITE_CHUNK_BYTES:
                self._write_buffer()
        self._write_buffer()

    def _append(self, kind: int, payload: bytes, at: float, routing: tuple | None,
                window: tuple | None = None) -> bool:
        """Buffer one line under the next counter; ``routing=None`` marks writer-owned lines."""
        stamp = spool.timestamp_bytes(at)
        external = self._externalize(payload) if kind == EVENT else None
        if external is not None:
            pending, added = self._blob_plan(external[1])
            line = spool.encode_event_line(self._n, stamp, external[0], version=spool.BLOB_VERSION)
        else:
            encode = spool.encode_event_line if kind == EVENT else spool.encode_control_line
            line, pending, added = encode(self._n, stamp, payload), [], 0
        # New blob bytes count against the spool budget like the line itself.
        limit = self.spool_max_bytes + (0 if routing is not None else self.CONTROL_RESERVE_BYTES)
        if self._spool_estimate + self._buffer_bytes + len(line) + added > limit:
            self._refuse("spool_full", len(payload), at, routing)
            return False
        if not self._open_file():
            self._refuse("writer_error", len(payload), at, routing)
            return False
        if external is not None and not self._store_blobs(pending):
            # A blob that cannot be stored leaves the event inline.
            line = spool.encode_event_line(self._n, stamp, payload)
            if self._spool_estimate + self._buffer_bytes + len(line) > self.spool_max_bytes:
                self._writer_drop("spool_full", len(payload), at, routing)
                return False
        self._buffer.append(line)
        self._meta.append((self._n, len(line), len(payload), at, routing, window))
        self._buffer_bytes += len(line)
        self._n += 1
        if self._file_size + self._buffer_bytes >= self.file_bytes:
            self._rotate()
        return True

    def _writer_drop(self, reason: str, size: int, at: float, routing: tuple) -> None:
        with self._lock:
            self._writer_drops += 1
            self._drop_locked(reason, size, at, *routing)

    def _refuse(self, reason: str, size: int, at: float, routing: tuple | None) -> None:
        """A line that cannot be buffered: a drop, or a skip for a writer-owned line (a gap line stays pending)."""
        if routing is not None:
            self._writer_drop(reason, size, at, routing)
            return
        self._writer_lines_skipped += 1
        if _log_limiter.allow(("writer_line_skipped", reason)):
            log.warning("Trajectory writer line skipped reason=%s producer_id=%s", reason, self.producer_id)

    def _externalize(self, payload: bytes) -> tuple[bytes, dict[str, bytes]] | None:
        try:
            return externalize(payload, min_bytes=self.blob_min_bytes)
        except Exception as exc:
            self._note_error(exc)
            return None

    def _blob_plan(self, blobs: dict[str, bytes]) -> tuple[list[tuple], int]:
        """The blobs of a line that need storing or an mtime refresh, and the bytes the missing ones add."""
        now = time.monotonic()
        pending, added = [], 0
        for sha, content in blobs.items():
            seen = self._blobs_seen.get(sha)
            if seen is not None and now - seen < spool.BLOB_REFRESH_SECONDS:
                continue
            try:
                os.stat(self.blob_dir / sha)
                exists = True
            except OSError:
                exists = False
                added += len(content)
            pending.append((sha, content, exists))
        return pending, added

    def _store_blobs(self, pending: list[tuple]) -> bool:
        """Refresh the mtime of existing blobs and store missing ones, before the line referencing them."""
        for sha, content, exists in pending:
            path = self.blob_dir / sha
            if exists:
                try:
                    os.utime(path)
                except FileNotFoundError:
                    exists = False  # swept since the check: store it again
                except OSError as exc:
                    self._blob_failed(exc)
                    return False
            if not exists and not self._write_blob(path, content):
                return False
            self._blobs_seen[sha] = time.monotonic()
            self._blobs_seen.move_to_end(sha)
            while len(self._blobs_seen) > BLOB_MEMORY:
                self._blobs_seen.popitem(last=False)
        return True

    def _write_blob(self, path: Path, content: bytes) -> bool:
        """Temp file, fsync, rename: a blob is complete under its name or absent."""
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            try:
                descriptor = os.open(temporary, flags, spool.FILE_MODE)
            except FileNotFoundError:
                spool.ensure_private_dir(path.parent)
                descriptor = os.open(temporary, flags, spool.FILE_MODE)
        except OSError as exc:
            self._blob_failed(exc)
            return False
        try:
            os.fchmod(descriptor, spool.FILE_MODE)
        except OSError:
            pass
        try:
            try:
                view, written = memoryview(content), 0
                while written < len(content):
                    count = self._write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError(errno.EIO, "Spool blob write made no progress")
                    written += count
                self._fsync(descriptor)
            finally:
                os.close(descriptor)
            self._rename(temporary, path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            self._blob_failed(exc)
            return False
        self._blobs_written += 1
        self._blob_bytes_written += len(content)
        self._spool_estimate += len(content)
        self._shared_estimate += len(content)
        self._blobs_unsynced = True
        return True

    def _blob_failed(self, exc: OSError) -> None:
        self._blob_errors += 1
        self._note_error(exc)

    def _prepare_directory(self) -> bool:
        if self._ready:
            return True
        try:
            os.makedirs(self.spool_dir.parent, exist_ok=True)
            spool.ensure_private_dir(self.producer_dir)
            spool.write_json_atomic(self.producer_dir / spool.PRODUCER_FILE, spool.producer_document(
                self.producer_id, role=self.role, started=self.started_at, hostname=self.hostname, pid=self.pid))
        except OSError as exc:
            self._note_error(exc)
            return False
        self._ready = True
        return True

    def _open_file(self) -> bool:
        if self._fd is not None:
            return True
        now = time.monotonic()
        if now < self._retry_at:
            return False
        if not self._prepare_directory():
            self._retry_at = now + self.RETRY_SECONDS
            return False
        self._counter += 1
        path = self.producer_dir / spool.file_name(self._counter, closed=False)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                                 spool.FILE_MODE)
        except OSError as exc:
            if not isinstance(exc, FileExistsError):
                self._counter -= 1
            if isinstance(exc, FileNotFoundError):
                # The directory vanished underneath; recreate it on retry.
                self._ready = False
            self._note_error(exc)
            self._retry_at = now + self.RETRY_SECONDS
            return False
        try:
            os.fchmod(descriptor, spool.FILE_MODE)
        except OSError:
            pass
        self._fd, self._part_path, self._file_size, self._file_opened = descriptor, path, 0, now
        return True

    def _write_buffer(self) -> None:
        if self._buffer and self._file_removed() and not self._reopen():
            self._write_failed(OSError(errno.ENOENT, "Open spool file was removed"), 0)
        if self._buffer:
            data = self._buffer[0] if len(self._buffer) == 1 else b"".join(self._buffer)
            written = 0
            try:
                view = memoryview(data)
                while written < len(data):
                    count = self._write(self._fd, view[written:])
                    if count <= 0:
                        raise OSError(errno.EIO, "Spool write made no progress")
                    written += count
            except OSError as exc:
                self._write_failed(exc, written)
            else:
                self._file_size += written
                self._spool_estimate += written
                self._written_lines += len(self._meta)
                self._settle_gap_lines(self._meta)
                self._buffer, self._meta, self._buffer_bytes = [], [], 0
        self._release()

    def _file_removed(self) -> bool:
        """The open file is unlinked: the worker consumes and deletes a ``.part`` left unmodified for
        TRAJECTORY_SPOOL_ABANDON_SECONDS, so a writer stalled that long would write into a deleted file."""
        try:
            return self._fd is not None and os.fstat(self._fd).st_nlink == 0
        except OSError as exc:
            self._note_error(exc)
            return False

    def _reopen(self) -> bool:
        """Close the removed file without a rename and open the next one for the buffered lines."""
        descriptor, self._fd, self._part_path = self._fd, None, None
        self._files_lost += 1
        try:
            os.close(descriptor)
        except OSError:
            pass
        if self._open_file():
            return True
        # The worker also removes the directory of a producer left without data files and heartbeat.
        self._retry_at = 0.0
        return not self._ready and self._open_file()

    def _release(self) -> None:
        if self._unreleased_lines:
            with self._lock:
                self._queued_bytes -= self._unreleased_bytes
                self._queued_lines -= self._unreleased_lines
            self._unreleased_bytes = self._unreleased_lines = 0

    def _write_failed(self, exc: OSError, written: int) -> None:
        """ENOSPC and friends: keep complete lines, drop the rest, reopen later."""
        self._write_errors += 1
        self._note_error(exc)
        complete = kept = 0
        for meta in self._meta:
            if complete + meta[1] > written:
                break
            complete += meta[1]
            kept += 1
        self._file_size += complete
        self._spool_estimate += complete
        self._written_lines += kept
        self._settle_gap_lines(self._meta[:kept])
        lost = self._meta[kept:]
        self._buffer, self._meta, self._buffer_bytes = [], [], 0
        self._discard_file(lost)
        self._retry_at = time.monotonic() + self.RETRY_SECONDS

    def _discard_file(self, lost: list[tuple]) -> None:
        """Close the open file keeping only complete lines; lost lines become drops."""
        repaired = self._fd is None
        if self._fd is not None:
            try:
                self._truncate(self._fd, self._file_size)
                repaired = True
            except OSError as exc:
                self._note_error(exc)
            # A torn tail that cannot be cut stays in an abandoned .part file.
            self._close_file(rename=repaired)
        if not lost:
            return
        if repaired:
            # Nothing past the kept prefix is on disk, so the lost counters are
            # reused and n stays contiguous; the gap control reports the loss.
            self._n = lost[0][0]
        for _, _, size, at, routing, window in lost:
            if window is not None:
                self._restore_window(*window)
            elif routing is not None:
                self._writer_drop("writer_error", size, at, routing)

    def _close_file(self, *, rename: bool) -> None:
        descriptor, path, size = self._fd, self._part_path, self._file_size
        self._fd = self._part_path = None
        if rename and size:
            try:
                self._fsync(descriptor)
            except OSError as exc:
                self._note_error(exc)
        try:
            os.close(descriptor)
        except OSError:
            pass
        if not rename:
            return
        if not size:
            try:
                os.unlink(path)
                self._counter -= 1
            except OSError:
                pass
            return
        if self._blobs_unsynced:
            # A closed file must not outlive a crash without the blob entries its lines reference.
            self._blobs_unsynced = not _fsync_directory(self.blob_dir)
        try:
            self._rename(path, path.with_name(path.name[:-len(".part")]))
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                # Consumed as abandoned and deleted while this writer was stalled.
                self._files_lost += 1
            self._note_error(exc)
            return
        self._files_closed += 1
        _fsync_directory(self.producer_dir)

    def _rotate(self) -> None:
        self._write_buffer()
        if self._fd is not None:
            self._close_file(rename=True)

    def _write_gaps(self, closing: bool) -> None:
        # At most one gap line per interval. A graceful close writes the windows
        # pending when it starts; drops that keep arriving cannot delay goodbye.
        with self._lock:
            lines = len(self._drops) if closing else 1
        while lines > 0 and self._drops:
            now = time.monotonic()
            if not closing and now - self._last_gap < self.GAP_INTERVAL_SECONDS:
                return
            with self._lock:
                if not self._drops:
                    return
                reason = next(iter(self._drops))
                window = self._drops.pop(reason)
                # Stays pending until its line is written or the window is restored.
                self._gaps_in_flight += 1
            lines -= 1
            self._last_gap = now
            control = window.control(reason)
            try:
                payload = orjson.dumps(control, default=_json_default, option=orjson.OPT_NON_STR_KEYS)
            except Exception as exc:
                # An identity orjson rejects (a lone surrogate) must not lose the counts.
                self._note_error(exc)
                payload = orjson.dumps({**control, "sessions": []})
            if not self._append(CONTROL, payload, time.time(), None, (reason, window)):
                self._restore_window(reason, window)
                return

    def _settle_gap_lines(self, metas: list[tuple]) -> None:
        written = sum(1 for meta in metas if meta[5] is not None)
        if written:
            with self._lock:
                self._gap_lines += written
                self._gaps_in_flight -= written

    def _restore_window(self, reason: str, window: _DropWindow) -> None:
        with self._lock:
            self._gaps_in_flight -= 1
            newer = self._drops.pop(reason, None)
            if newer is not None:
                window.merge(newer)
            # Oldest first, so a restored window keeps its turn.
            self._drops = {reason: window, **self._drops}

    def _recover(self) -> None:
        """Account for work left by a failed cycle or a dead writer thread."""
        lost, self._meta = self._meta, []
        self._buffer, self._buffer_bytes = [], 0
        self._discard_file(lost)
        batch = self._inflight
        while batch:
            kind, payload, at, user_id, session_id, run_id, request_id = batch.popleft()
            self._unreleased_bytes += len(payload)
            self._unreleased_lines += 1
            self._writer_drop("writer_error", len(payload), at, (user_id, session_id, run_id, request_id))
        self._release()


def control_routing(control) -> tuple:
    if not isinstance(control, dict):
        return None, None, None, None
    return control.get("user_id"), control.get("session_id"), control.get("run_id"), control.get("request_id")


def _unexpected(where: str, exc: Exception) -> None:
    if _log_limiter.allow((where, type(exc).__name__)):
        log.warning("Trajectory %s failed error_type=%s", where, type(exc).__name__)


# Process emitter --------------------------------------------------------------

_emitter: Emitter | None = None
_emitter_lock = threading.Lock()


def get_emitter() -> Emitter | None:
    """The started process emitter; ``None`` when ``TRAJECTORY_WORKER_MODE=off``."""
    global _emitter
    try:
        if pipeline_off():
            return None
        emitter = _emitter
        if emitter is not None:
            return emitter
        with _emitter_lock:
            if _emitter is None:
                settings = emitter_settings()
                emitter = Emitter(settings.spool_dir, queue_bytes=settings.queue_bytes,
                                  max_event_bytes=settings.max_event_bytes, file_bytes=settings.file_bytes,
                                  file_ms=settings.file_ms, spool_max_bytes=settings.spool_max_bytes,
                                  budget_refresh_ms=settings.budget_refresh_ms,
                                  blob_min_bytes=integer("TRAJECTORY_SPOOL_BLOB_MIN_BYTES", spool.BLOB_MIN_BYTES))
                emitter.start()
                _emitter = emitter
            return _emitter
    except Exception as exc:
        _unexpected("emitter start", exc)
        return None


def reset_emitter_for_tests() -> None:
    global _emitter
    with _emitter_lock:
        emitter, _emitter = _emitter, None
    if emitter is not None:
        emitter.close(2.0)


def _after_fork_in_child() -> None:
    # Threads do not survive fork: the child must neither write under the
    # parent's producer id nor wait on the parent's locks at exit.
    global _emitter, _emitter_lock
    emitter, _emitter = _emitter, None
    _emitter_lock = threading.Lock()
    # A lock held by another parent thread at fork time would never be released.
    _log_limiter._lock = threading.Lock()
    if emitter is not None:
        emitter._forked = True
        try:
            atexit.unregister(emitter._close_at_exit)
        except Exception:
            pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


# Public producer API (SPEC §5.3); none of these raise --------------------------

def _event_dict(event_type, data, event_id, occurred_at, ids) -> dict:
    event = {"type": event_type, "data": data, **ids}
    if event_id is not None:
        event["event_id"] = event_id
    if occurred_at is not None:
        event["occurred_at"] = occurred_at
    return event


def _encode_event(emitter: Emitter, context: TraceContext, event: dict):
    """Budget filter, validation, serialization: ``(payload, routing, event_id)`` or ``None``."""
    user_id, session_id = context.user_id, context.session_id
    run_id = event["run_id"] if "run_id" in event else context.run_id
    request_id = event["request_id"] if "request_id" in event else context.request_id
    level = emitter.budgets.level(user_id, session_id)
    if level != NORMAL:
        original = event.get("data", {})
        verdict, data = filter_event(level, event.get("type"), original)
        if verdict == DROP_BUDGET:
            emitter.drop("budget", 0, user_id, session_id, run_id, request_id)
            return None
        if verdict is not None:
            emitter.count_filtered()
            return None
        if data is not original:
            event["data"] = data
            emitter.count_filtered(truncated=True)
    try:
        prepared = prepare_fast(context, event)
    except Exception as exc:
        emitter.drop("invalid_event", 0, user_id, session_id, run_id, request_id)
        event_type = event.get("type") if isinstance(event.get("type"), str) else None
        if _log_limiter.allow(("invalid_event", event_type)):
            detail = str(exc) if isinstance(exc, TrajectoryError) else type(exc).__name__
            log.warning("Trajectory event dropped as invalid type=%r detail=%s", event_type, detail)
        return None
    routing = (user_id, session_id, prepared.get("run_id"), prepared.get("request_id"))
    try:
        payload = orjson.dumps(prepared, default=_json_default, option=orjson.OPT_NON_STR_KEYS)
    except Exception as exc:
        emitter.drop("serialization_failed", 0, *routing)
        if _log_limiter.allow(("serialization_failed", prepared["type"])):
            log.warning("Trajectory event dropped: not serializable type=%s error_type=%s",
                        prepared["type"], type(exc).__name__)
        return None
    return payload, routing, prepared["event_id"]


def _defer(emitter: Emitter, db, kind: int, payload: bytes, routing: tuple) -> bool:
    session = getattr(db, "sync_session", db)
    if not isinstance(session, SyncSession):
        emitter.drop("invalid_event", len(payload), *routing)
        return False
    session.info.setdefault(PENDING_KEY, []).append((kind, payload, routing))
    return True


def emit(type: str, data: dict, *, context: TraceContext | None = None, event_id: str | None = None,
         occurred_at=None, **ids) -> str | None:
    """Validate, serialize and enqueue one event now; the event id when enqueued."""
    try:
        emitter = get_emitter()
        context = context or current()
        if emitter is None or context is None or not enabled(context.user_id):
            return None
        encoded = _encode_event(emitter, context, _event_dict(type, data, event_id, occurred_at, ids))
        if encoded is None:
            return None
        payload, (user_id, session_id, run_id, request_id), identifier = encoded
        if emitter.emit_bytes(payload, user_id=user_id, session_id=session_id, run_id=run_id, request_id=request_id):
            return identifier
        return None
    except Exception as exc:
        _unexpected("emit", exc)
        return None


def emit_after_commit(db, type: str, data: dict, *, context: TraceContext | None = None,
                      event_id: str | None = None, occurred_at=None, **ids) -> str | None:
    """Serialize now; enqueue when the outer transaction of ``db`` commits."""
    try:
        emitter = get_emitter()
        context = context or current()
        if emitter is None or context is None or not enabled(context.user_id):
            return None
        encoded = _encode_event(emitter, context, _event_dict(type, data, event_id, occurred_at, ids))
        if encoded is None:
            return None
        payload, routing, identifier = encoded
        return identifier if _defer(emitter, db, EVENT, payload, routing) else None
    except Exception as exc:
        _unexpected("emit_after_commit", exc)
        return None


def emit_control(control: dict, *, db=None) -> bool:
    """Content-free control record, written even while recording is disabled."""
    try:
        emitter = get_emitter()
        if emitter is None:
            return False
        if db is None:
            return emitter.emit_control(control)
        payload = emitter.encode_control(control)
        return payload is not None and _defer(emitter, db, CONTROL, payload, control_routing(control))
    except Exception as exc:
        _unexpected("emit_control", exc)
        return False


def emit_stream(context: TraceContext, event: dict) -> None:
    """``request.delta`` / ``tool.output`` chunks; the same path as ``emit``."""
    try:
        emitter = get_emitter()
        context = context or current()
        if emitter is None or context is None or not enabled(context.user_id):
            return None
        if not isinstance(event, dict):
            emitter.drop("invalid_event", 0, context.user_id, context.session_id, context.run_id, context.request_id)
            return None
        encoded = _encode_event(emitter, context, dict(event))
        if encoded is not None:
            payload, (user_id, session_id, run_id, request_id), _ = encoded
            emitter.emit_bytes(payload, user_id=user_id, session_id=session_id, run_id=run_id, request_id=request_id)
    except Exception as exc:
        _unexpected("emit_stream", exc)
    return None


def completed_receipt():
    """An already resolved stream receipt (result ``None``)."""
    try:
        future = asyncio.get_running_loop().create_future()
    except RuntimeError:
        future = concurrent.futures.Future()
    future.set_result(None)
    return future


async def flush_spool() -> str:
    """Spool-sink ``flush``: wait for the writer off the event loop."""
    try:
        emitter = get_emitter()
        if emitter is not None:
            await asyncio.to_thread(emitter.flush)
    except Exception as exc:
        _unexpected("flush", exc)
    return "0"


# Commit hooks: global listeners that only act on sessions holding pending emits.

@sa_event.listens_for(SyncSession, "after_commit")
def _enqueue_after_commit(session) -> None:
    # SAVEPOINT releases dispatch after_commit too; only the outer commit counts.
    if session.in_nested_transaction():
        return
    pending = session.info.pop(PENDING_KEY, None)
    if not pending:
        return
    try:
        emitter = get_emitter()
        if emitter is None:
            return
        for kind, payload, routing in pending:
            emitter.enqueue_encoded(kind, payload, routing)
    except Exception as exc:
        _unexpected("after_commit enqueue", exc)


@sa_event.listens_for(SyncSession, "after_soft_rollback")
def _discard_after_rollback(session, previous_transaction) -> None:
    # A rollback that ends at a SAVEPOINT keeps the outer transaction's emits.
    boundary = previous_transaction
    while boundary is not None and not boundary.nested and boundary.parent is not None:
        boundary = boundary.parent
    if boundary is not None and boundary.nested:
        return
    session.info.pop(PENDING_KEY, None)


@sa_event.listens_for(SyncSession, "after_transaction_end")
def _discard_without_commit(session, transaction) -> None:
    # An outer transaction closed without COMMIT (a cancelled request only
    # closes its session) never commits its facts. A committed transaction
    # already popped the list in after_commit.
    if transaction.parent is None:
        session.info.pop(PENDING_KEY, None)
