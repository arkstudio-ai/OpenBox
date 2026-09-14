"""Spool format v1 shared by the backend emitter and the worker reader.

Directory layout, line encoding, file naming, ``producer.json`` and the
``control/budgets.json`` schema (docs/trajectory-rearch/SPEC.md §3, §5.7).
Nothing here performs I/O at import time.
"""
from __future__ import annotations

import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import orjson

VERSION = 1
PRODUCERS_DIR = "producers"
CONTROL_DIR = "control"
QUARANTINE_DIR = "quarantine"
PRODUCER_FILE = "producer.json"
BUDGETS_FILE = "budgets.json"
WORKER_FILE = "worker.json"
CLOSED_SUFFIX = ".jsonl"
OPEN_SUFFIX = ".jsonl.part"
COUNTER_DIGITS = 20
DIR_MODE = 0o700
FILE_MODE = 0o600

KIND_EVENT = "event"
KIND_CONTROL = "control"

GAP_REASONS = ("queue_overflow", "spool_full", "serialization_failed", "invalid_event",
               "event_too_large", "writer_error", "budget")
CONTROL_TYPES = ("gap", "producer.goodbye", "session.meta", "user.meta", "workspace.meta",
                 "asset.meta", "session.deleted", "asset.deleted", "recording.state")
GAP_MAX_SESSIONS = 200
GAP_MAX_RUN_IDS = 20
GAP_MAX_REQUEST_IDS = 50

BUDGET_LEVELS = ("normal", "degraded", "blocked")

_FILE_NAME = re.compile(r"^(\d{20})\.jsonl(\.part)?$")
_HOST_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_EVENT_PREFIX = b'{"v":1,"k":"event","n":'
_CONTROL_PREFIX = b'{"v":1,"k":"control","n":'


class SpoolFormatError(ValueError):
    """A spool line or file that readers must not ingest."""


class UnsupportedSpoolVersion(SpoolFormatError):
    """A line written by a newer format; the worker quarantines the file."""


_second = (-1, b"")


def timestamp_bytes(epoch: float) -> bytes:
    """ISO-8601 UTC with milliseconds and ``Z``, truncated like ``types.iso``."""
    global _second
    # Whole microseconds first: 1789372800.123 is stored as ...122999 in binary.
    whole, micros = divmod(round(epoch * 1_000_000), 1_000_000)
    cached = _second
    if cached[0] != whole:
        cached = (whole, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)).encode())
        _second = cached
    return b"%s.%03dZ" % (cached[1], micros // 1000)


def timestamp(epoch: float | None = None) -> str:
    return timestamp_bytes(time.time() if epoch is None else epoch).decode()


def encode_event_line(n: int, t: bytes, event_json: bytes) -> bytes:
    return b"".join((_EVENT_PREFIX, b"%d" % n, b',"t":"', t, b'","event":', event_json, b"}\n"))


def encode_control_line(n: int, t: bytes, control_json: bytes) -> bytes:
    return b"".join((_CONTROL_PREFIX, b"%d" % n, b',"t":"', t, b'","control":', control_json, b"}\n"))


def decode_line(line: bytes) -> dict:
    """Validate one complete line; unknown top-level keys are kept and ignored."""
    try:
        record = orjson.loads(line)
    except orjson.JSONDecodeError as exc:
        raise SpoolFormatError("Spool line is not JSON") from exc
    if not isinstance(record, dict):
        raise SpoolFormatError("Spool line must be an object")
    if record.get("v") != VERSION:
        raise UnsupportedSpoolVersion("Unsupported spool format version")
    kind, n = record.get("k"), record.get("n")
    if kind not in (KIND_EVENT, KIND_CONTROL):
        raise SpoolFormatError("Unknown spool line kind")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise SpoolFormatError("Spool line counter must be a positive integer")
    if not isinstance(record.get("t"), str):
        raise SpoolFormatError("Spool line time must be a string")
    body = record.get(kind)
    if not isinstance(body, dict):
        raise SpoolFormatError(f"Spool {kind} must be an object")
    if kind == KIND_CONTROL and not isinstance(body.get("type"), str):
        raise SpoolFormatError("Spool control requires a type")
    return record


def file_name(counter: int, *, closed: bool = True) -> str:
    return f"{counter:0{COUNTER_DIGITS}d}{CLOSED_SUFFIX if closed else OPEN_SUFFIX}"


def parse_file_name(name: str) -> tuple[int, bool] | None:
    """``(counter, closed)`` for spool data files, ``None`` for anything else."""
    match = _FILE_NAME.match(name)
    if match is None:
        return None
    return int(match.group(1)), match.group(2) is None


def producer_id(started: float, hostname: str, pid: int, token: str) -> str:
    """``{UTC yyyymmddHHMMSS}-{hostname}-{pid}-{8 hex}``; lexical order is start order."""
    host = _HOST_UNSAFE.sub("_", hostname)[:64] or "host"
    return f"{time.strftime('%Y%m%d%H%M%S', time.gmtime(started))}-{host}-{pid}-{token[:8]}"


_process_boot = uuid.uuid4().hex


def boot_id() -> str:
    """Kernel boot id where available; otherwise a value unique to this interpreter."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            value = handle.read().strip()
        if value:
            return value
    except OSError:
        pass
    return _process_boot


def producer_document(identifier: str, *, role: str, started: float,
                      hostname: str | None = None, pid: int | None = None) -> dict:
    return {"version": VERSION, "producer_id": identifier, "boot_id": boot_id(),
            "hostname": hostname if hostname is not None else socket.gethostname(),
            "pid": pid if pid is not None else os.getpid(), "role": role,
            "started_at": datetime.fromtimestamp(started, timezone.utc)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")}


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` and missing parents below existing ones with mode 0700."""
    missing = []
    current = Path(path)
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, DIR_MODE)
        except FileExistsError:
            continue
        os.chmod(directory, DIR_MODE)


def write_json_atomic(path: Path, value, *, mode: int = FILE_MODE) -> None:
    """Temp file in the same directory, fsync, then rename over ``path``."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.write(descriptor, orjson.dumps(value))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def spool_usage_bytes(spool_dir: Path) -> int:
    """Sum of file sizes under ``producers/`` (one directory per producer)."""
    total = 0
    try:
        producers = list(os.scandir(Path(spool_dir) / PRODUCERS_DIR))
    except FileNotFoundError:
        return 0
    for producer in producers:
        try:
            if not producer.is_dir(follow_symlinks=False):
                continue
            entries = list(os.scandir(producer.path))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def budgets_path(spool_dir: Path) -> Path:
    return Path(spool_dir) / CONTROL_DIR / BUDGETS_FILE


def budgets_document(*, sessions: dict | None = None, users: dict | None = None,
                     generated_at: str | None = None) -> dict:
    """``{"version", "generated_at", "sessions": {root: entry}, "users": {user: entry}}``.

    Entries are ``{"level": "degraded"|"blocked", "reason", "since"}``.
    """
    return {"version": VERSION, "generated_at": generated_at or timestamp(),
            "sessions": sessions or {}, "users": users or {}}


def write_budgets(spool_dir: Path, document: dict) -> None:
    ensure_private_dir(Path(spool_dir) / CONTROL_DIR)
    write_json_atomic(budgets_path(spool_dir), document)


def parse_budgets(raw: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """Non-normal levels by root session id and by user id.

    Raises ``SpoolFormatError`` for a file that is not a version 1 budget
    document; malformed individual entries are skipped.
    """
    try:
        document = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise SpoolFormatError("Budget file is not JSON") from exc
    if not isinstance(document, dict) or document.get("version") != VERSION:
        raise SpoolFormatError("Unsupported budget file")
    levels = []
    for section in ("sessions", "users"):
        entries = document.get(section) or {}
        if not isinstance(entries, dict):
            raise SpoolFormatError(f"Budget {section} must be an object")
        levels.append({key: entry["level"] for key, entry in entries.items()
                       if isinstance(entry, dict) and entry.get("level") in BUDGET_LEVELS[1:]})
    return levels[0], levels[1]
