"""Spool format shared by the backend emitter and the worker reader.

Directory layout, line encoding (versions 1 and 2), blob files, file naming,
``producer.json`` and the ``control/budgets.json`` schema
(docs/trajectory-rearch/SPEC.md §3, §5.7). Nothing here performs I/O at import time.
"""
from __future__ import annotations

import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import orjson

#: Version 1 lines and the JSON documents (``producer.json``, ``budgets.json``, ``worker.json``).
VERSION = 1
#: Event lines whose values include ``{"$blob": sha256}`` references; every other line stays version 1.
BLOB_VERSION = 2
LINE_VERSIONS = (VERSION, BLOB_VERSION)
PRODUCERS_DIR = "producers"
CONTROL_DIR = "control"
QUARANTINE_DIR = "quarantine"
#: Sidecar of a quarantined data file: ``quarantine/<name>.reason``.
REASON_SUFFIX = ".reason"
#: A blob a quarantined data file references, kept beside it: ``quarantine/<name>.blob-<sha256>``.
QUARANTINE_BLOB_INFIX = ".blob-"
BLOBS_DIR = "blobs"
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

BLOB_KEY = "$blob"
#: The serialized reference as the writer emits it; version 2 lines contain these bytes only as references.
BLOB_REFERENCE = b'{"$blob":"'
#: Any value inside event data whose compact JSON is larger than this moves to a blob.
BLOB_VALUE_BYTES = 16 * 1024
#: Default of ``TRAJECTORY_SPOOL_BLOB_MIN_BYTES``: ``request.prepared`` inputs larger than this move to blobs.
BLOB_MIN_BYTES = 1024
#: A writer refreshes the mtime of a blob it references at most this often.
BLOB_REFRESH_SECONDS = 60.0
#: The worker deletes a blob only when its mtime is older than every data file by this much. It must exceed
#: ``BLOB_REFRESH_SECONDS`` plus the longest time a writer keeps a data file open.
BLOB_MARGIN_SECONDS = 600.0

BUDGET_LEVELS = ("normal", "degraded", "blocked")

_FILE_NAME = re.compile(r"^(\d{20})\.jsonl(\.part)?$")
_BLOB_NAME = re.compile(r"[0-9a-f]{64}")
_HOST_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_EVENT_PREFIX = b'{"v":1,"k":"event","n":'
_CONTROL_PREFIX = b'{"v":1,"k":"control","n":'
BLOB_EVENT_PREFIX = b'{"v":2,"k":"event","n":'


def _is_version(value) -> bool:
    return type(value) is int and value == VERSION


def _is_line_version(value) -> bool:
    return type(value) is int and value in LINE_VERSIONS


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


def encode_event_line(n: int, t: bytes, event_json: bytes, *, version: int = VERSION) -> bytes:
    """A version 1 line, or version 2 (``BLOB_VERSION``) for an event holding blob references."""
    prefix = BLOB_EVENT_PREFIX if version == BLOB_VERSION else _EVENT_PREFIX
    return b"".join((prefix, b"%d" % n, b',"t":"', t, b'","event":', event_json, b"}\n"))


def encode_control_line(n: int, t: bytes, control_json: bytes) -> bytes:
    return b"".join((_CONTROL_PREFIX, b"%d" % n, b',"t":"', t, b'","control":', control_json, b"}\n"))


def decode_line(line: bytes) -> dict:
    """Validate one complete line; unknown top-level keys are kept and ignored.

    A version 2 event still holds its blob references; ``spool_reader.read_batch``
    returns lines with them resolved.
    """
    try:
        record = orjson.loads(line)
    except orjson.JSONDecodeError as exc:
        raise SpoolFormatError("Spool line is not JSON") from exc
    if not isinstance(record, dict):
        raise SpoolFormatError("Spool line must be an object")
    # Exact integers only: true and 1.0 compare equal to 1 but are not version 1.
    if not _is_line_version(record.get("v")):
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


def blobs_dir(spool_dir: Path) -> Path:
    return Path(spool_dir) / BLOBS_DIR


def is_blob_name(name: str) -> bool:
    """A blob file name: the sha256 of the file's content, 64 lowercase hex digits."""
    return _BLOB_NAME.fullmatch(name) is not None


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


def write_json_atomic(path: Path, value, *, mode: int = FILE_MODE, fsync: bool = True) -> None:
    """Temp file in the same directory, fsync (unless ``fsync`` is False), then rename over ``path``."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.write(descriptor, orjson.dumps(value))
            if fsync:
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


def quarantine_blob_name(name: str, sha: str) -> str:
    return f"{name}{QUARANTINE_BLOB_INFIX}{sha}"


def quarantine_blob_owner(name: str) -> str | None:
    """The name of the quarantined data file a kept blob belongs to; ``None`` for any other file name."""
    start = len(name) - 64 - len(QUARANTINE_BLOB_INFIX)
    if start <= 0 or name.startswith(".") or not name.startswith(QUARANTINE_BLOB_INFIX, start):
        return None
    return name[:start] if is_blob_name(name[-64:]) else None


def is_quarantined_file(name: str) -> bool:
    """A quarantined data file: not its ``.reason`` sidecar or a blob kept for it, and not a temporary (dot) file."""
    return not name.startswith(".") and not name.endswith(REASON_SUFFIX) and quarantine_blob_owner(name) is None


@dataclass(frozen=True)
class SpoolUsage:
    """Allocated bytes of the files that count against ``TRAJECTORY_SPOOL_MAX_BYTES``."""
    producers_bytes: int = 0
    blob_bytes: int = 0
    #: Every file in ``quarantine/``, sidecars and kept blobs included.
    quarantine_bytes: int = 0
    #: Quarantined data files, without their ``.reason`` sidecars and kept blobs.
    quarantine_files: int = 0

    @property
    def total(self) -> int:
        return self.producers_bytes + self.blob_bytes + self.quarantine_bytes


def spool_usage(spool_dir: Path) -> SpoolUsage:
    """Usage of the producer directories, ``blobs/`` and ``quarantine/``; a missing directory counts as empty."""
    quarantine_bytes, quarantine_files = _directory_usage(Path(spool_dir) / QUARANTINE_DIR)
    return SpoolUsage(producer_usage_bytes(spool_dir), _directory_usage(blobs_dir(spool_dir))[0],
                      quarantine_bytes, quarantine_files)


def spool_usage_bytes(spool_dir: Path) -> int:
    return spool_usage(spool_dir).total


def producer_usage_bytes(spool_dir: Path) -> int:
    """Allocated bytes of the files directly inside each directory under ``producers/``."""
    try:
        producers = list(os.scandir(Path(spool_dir) / PRODUCERS_DIR))
    except FileNotFoundError:
        return 0
    total = 0
    for producer in producers:
        try:
            if producer.is_dir(follow_symlinks=False):
                total += _directory_usage(producer.path)[0]
        except OSError:
            continue
    return total


def shared_usage_bytes(spool_dir: Path) -> int:
    """Allocated bytes of ``blobs/`` and ``quarantine/``, the part of the usage no producer owns."""
    return _directory_usage(blobs_dir(spool_dir))[0] + _directory_usage(Path(spool_dir) / QUARANTINE_DIR)[0]


def _allocated(status: os.stat_result) -> int:
    # Blocks, not st_size: small files take a whole block and budgets are about disk space.
    blocks = getattr(status, "st_blocks", None)
    return status.st_size if blocks is None else max(status.st_size, blocks * 512)


def _directory_usage(directory) -> tuple[int, int]:
    """``(allocated bytes, quarantined data files)`` of the regular files directly inside ``directory``.

    ``(0, 0)`` when it does not exist; raises when it cannot be listed otherwise.
    """
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0, 0
    total = files = 0
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                total += _allocated(entry.stat(follow_symlinks=False))
                files += is_quarantined_file(entry.name)
        except OSError:
            continue
    return total, files


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
    if not isinstance(document, dict) or not _is_version(document.get("version")):
        raise SpoolFormatError("Unsupported budget file")
    levels = []
    for section in ("sessions", "users"):
        entries = document.get(section)
        entries = {} if entries is None else entries
        if not isinstance(entries, dict):
            raise SpoolFormatError(f"Budget {section} must be an object")
        levels.append({key: entry["level"] for key, entry in entries.items()
                       if isinstance(entry, dict) and entry.get("level") in BUDGET_LEVELS[1:]})
    return levels[0], levels[1]
