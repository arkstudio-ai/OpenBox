"""Spool file discovery and reading for ingest (SPEC §3.1, §8.2).

A data file is ready when it is closed (``.jsonl``) or is an open ``.part``
whose mtime is older than ``TRAJECTORY_SPOOL_ABANDON_SECONDS``. Within one
producer files are consumed strictly by counter: a lower counter that is not
ready blocks the producer. Across producers the oldest ready file goes first.
Offsets are tracked under the closed name, so a ``.part`` renamed while it was
being consumed keeps its progress.

Version 2 event lines reference values stored in ``blobs/<sha256>``.
``read_batch`` returns them with those values in place, so ingest sees the
line a writer without blobs would have written; a line whose blob is missing
or corrupt comes back as a ``gap`` control and the file goes on. ``scan_spool``
sweeps the blob files that no data file can still reference and keeps
``quarantine/`` within its age and size limits.

Everything here is blocking file I/O; the ingest service calls it through
``asyncio.to_thread``. Lines are read in chunks and a batch stops at its line
and byte limits (resolved bytes), so memory stays bounded by one batch plus
one line.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import orjson

from core.log import create_logger
from trajectory import spool

log = create_logger("trajectory.worker.spool_reader")

READ_CHUNK_BYTES = 1024 * 1024
#: Blob files are swept at most this often; the mtime of ``blobs/.swept`` records the last sweep.
#: The ``quarantine/`` limits are applied at the same cadence.
BLOB_SWEEP_SECONDS = 60.0
SWEEP_MARKER = ".swept"
SWEEP_SUFFIX = ".sweep"
#: Data files unmodified this long no longer hold back the blob sweep: the blobs they reference are read instead.
OLD_DATA_SECONDS = 15 * 60.0
#: Bytes of old data files one sweep reads at most; beyond that the sweep keeps every blob newer than the
#: oldest data file, as if none were old.
OLD_DATA_READ_BYTES = 256 * 1024 * 1024
#: Suffix of a file renamed aside by ``remove_if_unchanged``.
REMOVE_SUFFIX = ".remove"
#: ``gap`` reasons of event lines whose blob cannot be used.
BLOB_MISSING = "spool_blob_missing"
BLOB_CORRUPT = "spool_blob_corrupt"
IDENTITY_MAX_CHARS = 128
LOG_INTERVAL_SECONDS = 60.0
_LINE_COUNTER = re.compile(rb'^\{"v":\d+,"k":"(?:event|control)","n":(\d+),')
_SHA256 = re.compile(rb"[0-9a-f]{64}")
_BLOB_REFERENCE = re.compile(re.escape(spool.BLOB_REFERENCE) + rb'([0-9a-f]{64})"\}')
#: Gap reason -> monotonic time before which it is not logged again.
_logged: dict[str, float] = {}
#: Spool directory -> wall time of its last quarantine sweep.
_quarantine_swept: dict[str, float] = {}


@dataclass(frozen=True)
class SpoolFile:
    producer_id: str
    counter: int
    path: Path
    closed: bool
    size: int
    mtime: float

    @property
    def name(self) -> str:
        """The closed file name: the offset key in ``trajectory_ingest_files``."""
        return spool.file_name(self.counter)

    def ready(self, now: float, abandon_seconds: float) -> bool:
        return self.closed or now - self.mtime >= abandon_seconds


@dataclass
class ProducerDir:
    producer_id: str
    path: Path
    document: dict | None
    files: list[SpoolFile] = field(default_factory=list)


@dataclass
class SpoolScan:
    producers: dict[str, ProducerDir]
    files: int = 0
    bytes: int = 0
    oldest_mtime: float | None = None
    #: False when a producer directory or a data file could not be listed; blobs are not swept then.
    complete: bool = True


@dataclass(frozen=True)
class RawLine:
    #: The line without its newline; a version 2 event line comes with its blob values resolved.
    data: bytes
    start: int
    end: int

    @property
    def size(self) -> int:
        """Bytes of the line as ingested: ``end - start`` unless blob values were resolved into it."""
        return len(self.data) + 1


@dataclass
class BatchRead:
    lines: list[RawLine]
    #: Offset after the last complete line returned (the start offset when none).
    end_offset: int
    #: No complete line remains after ``end_offset`` at the time of reading.
    eof: bool
    #: Bytes after the last complete line at end of file (a torn last line).
    tail_bytes: int


def producers_dir(spool_dir: Path) -> Path:
    return Path(spool_dir) / spool.PRODUCERS_DIR


def read_producer_document(path: Path) -> dict | None:
    try:
        document = orjson.loads(Path(path).read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def scan_spool(spool_dir: Path, *, documents: dict[str, dict | None] | None = None, sweep: bool = True,
               quarantine_max_bytes: int | None = None,
               quarantine_max_age_seconds: float | None = None) -> SpoolScan:
    """Producers and their data files (sorted by counter), with size totals.

    ``documents`` caches parsed ``producer.json`` files between scans. With ``sweep`` a complete
    scan also removes the blob files no listed data file can reference (``sweep_blobs``), and any
    scan applies the quarantine limits given (``sweep_quarantine``); each at most once per
    ``BLOB_SWEEP_SECONDS``.
    """
    started = time.time()
    scan = SpoolScan(producers={})
    try:
        entries = list(os.scandir(producers_dir(spool_dir)))
    except FileNotFoundError:
        entries = []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            children = list(os.scandir(entry.path))
        except FileNotFoundError:
            continue  # a finished producer directory removed since the listing
        except OSError:
            scan.complete = False
            continue
        producer_id = entry.name
        if documents is not None and documents.get(producer_id) is not None:
            document = documents[producer_id]
        else:
            document = read_producer_document(Path(entry.path) / spool.PRODUCER_FILE)
            if documents is not None:
                documents[producer_id] = document
        producer = ProducerDir(producer_id, Path(entry.path), document)
        by_counter: dict[int, SpoolFile] = {}
        for child in children:
            parsed = spool.parse_file_name(child.name)
            if parsed is None:
                continue
            try:
                status = child.stat(follow_symlinks=False)
            except FileNotFoundError:
                # Consumed and deleted, or just renamed from .part: that file was written to moments ago.
                continue
            except OSError:
                scan.complete = False
                continue
            counter, closed = parsed
            item = SpoolFile(producer_id, counter, Path(child.path), closed, status.st_size, status.st_mtime)
            # A rename between listing and stat cannot produce both names, but a
            # closed file always wins over a stale .part entry.
            if counter not in by_counter or closed:
                by_counter[counter] = item
            scan.files += 1
            scan.bytes += status.st_size
            if scan.oldest_mtime is None or status.st_mtime < scan.oldest_mtime:
                scan.oldest_mtime = status.st_mtime
        producer.files = [by_counter[counter] for counter in sorted(by_counter)]
        scan.producers[producer_id] = producer
    if sweep and scan.complete:
        _sweep_when_due(spool_dir, [item for producer in scan.producers.values() for item in producer.files], started)
    if sweep and (quarantine_max_bytes is not None or quarantine_max_age_seconds is not None):
        _sweep_quarantine_when_due(spool_dir, quarantine_max_bytes, quarantine_max_age_seconds, started)
    return scan


def select_ready(scan: SpoolScan, done: set[tuple[str, str]], *, now: float,
                 abandon_seconds: float) -> list[SpoolFile]:
    """The next consumable file of every producer, oldest mtime first.

    ``done`` holds ``(producer_id, name)`` of fully consumed files that still
    exist on disk (their deletion failed or is pending); they never block.
    """
    ready = []
    for producer in scan.producers.values():
        for item in producer.files:
            if (producer.producer_id, item.name) in done:
                continue
            if item.ready(now, abandon_seconds):
                ready.append(item)
            break
    return sorted(ready, key=lambda item: (item.mtime, item.producer_id, item.counter))


def read_batch(path: Path, offset: int, *, max_lines: int, max_bytes: int,
               chunk_size: int = READ_CHUNK_BYTES, blob_dir: Path | None = None) -> BatchRead:
    """Complete lines from ``offset``: at most ``max_lines`` and, after the first, ``max_bytes``.

    Version 2 event lines are resolved (``resolve_line``) against ``blob_dir``, by default the
    ``blobs/`` directory of the spool holding ``path``; their resolved size counts against ``max_bytes``.
    """
    lines: list[RawLine] = []
    consumed = 0
    line_start = offset
    pieces: list[bytes] = []
    eof = False
    limited = False
    blobs: dict[bytes, bytes | str] = {}
    with open(path, "rb") as handle:
        handle.seek(offset)
        while not limited:
            chunk = handle.read(chunk_size)
            if not chunk:
                eof = True
                break
            index = 0
            while True:
                newline = chunk.find(b"\n", index)
                if newline < 0:
                    if index < len(chunk):
                        pieces.append(chunk[index:])
                    break
                body = chunk[index:newline]
                if pieces:
                    pieces.append(body)
                    body = b"".join(pieces)
                    pieces = []
                length = len(body) + 1
                if body.startswith(spool.BLOB_EVENT_PREFIX):
                    if blob_dir is None:
                        # <spool>/producers/<producer_id>/<file>
                        blob_dir = spool.blobs_dir(Path(path).parent.parent.parent)
                    body = resolve_line(body, blob_dir, blobs, source=path)
                if lines and consumed + len(body) + 1 > max_bytes:
                    limited = True
                    break
                lines.append(RawLine(body, line_start, line_start + length))
                consumed += len(body) + 1
                line_start += length
                index = newline + 1
                if len(lines) >= max_lines:
                    limited = True
                    break
        tail = handle.tell() - line_start if eof else 0
    return BatchRead(lines, line_start, eof, tail)


def resolve_line(body: bytes, blob_dir: Path, cache: dict[bytes, bytes | str] | None = None, *,
                 source=None) -> bytes:
    """A version 2 event line with every ``{"$blob": sha256}`` replaced by the content of that blob.

    A blob holds the compact JSON of the value it replaced, and a version 2 line contains those
    bytes only as references, so the result is the line with every value inline: its event hashes
    and addresses its content exactly like the inline line. A missing or corrupt blob turns the
    line into a ``gap`` control; any other line is returned unchanged. ``cache`` keeps the blobs
    read for one batch.
    """
    if not body.startswith(spool.BLOB_EVENT_PREFIX):
        return body
    cache = {} if cache is None else cache
    marker = spool.BLOB_REFERENCE
    parts: list[bytes] = []
    position = 0
    while True:
        found = body.find(marker, position)
        if found < 0:
            break
        start = found + len(marker)
        sha = body[start:start + 64]
        if body[start + 64:start + 66] != b'"}' or not _SHA256.fullmatch(sha):
            return _gap_line(body, BLOB_CORRUPT, source)
        content = _blob(blob_dir, sha, cache)
        if isinstance(content, str):
            return _gap_line(body, content, source)
        parts.append(body[position:found])
        parts.append(content)
        position = start + 66
    if not parts:
        return body
    parts.append(body[position:])
    return b"".join(parts)


def _blob(blob_dir: Path, sha: bytes, cache: dict[bytes, bytes | str]) -> bytes | str:
    """The content of a blob checked against its name, or the gap reason when it cannot be used."""
    found = cache.get(sha)
    if found is None:
        try:
            with open(Path(blob_dir) / sha.decode("ascii"), "rb") as handle:
                content = handle.read()
        except OSError:
            found = BLOB_MISSING
        else:
            found = content if hashlib.sha256(content).hexdigest().encode("ascii") == sha else BLOB_CORRUPT
        cache[sha] = found
    return found


def _listable(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= IDENTITY_MAX_CHARS


def _gap_line(body: bytes, reason: str, source) -> bytes:
    """The ``gap`` control, with the same ``n`` and ``t``, that stands for an event line whose blob is unusable."""
    try:
        record = orjson.loads(body)
    except orjson.JSONDecodeError:
        return body  # not a spool line at all: decode_line rejects it
    event = record.get("event") if isinstance(record, dict) else None
    if not isinstance(event, dict):
        return body
    n, t = record.get("n"), record.get("t")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1 or not isinstance(t, str):
        return body
    sessions = []
    if _listable(event.get("user_id")) and _listable(event.get("session_id")):
        sessions.append({"user_id": event["user_id"], "session_id": event["session_id"],
                         "run_ids": [event["run_id"]] if _listable(event.get("run_id")) else [],
                         "request_ids": [event["request_id"]] if _listable(event.get("request_id")) else []})
    moment = time.monotonic()
    if moment >= _logged.get(reason, 0.0):
        # One line per reason per minute: a lost blobs directory must not flood the log.
        _logged[reason] = moment + LOG_INTERVAL_SECONDS
        log.warning("Spool event line recorded as a gap reason=%s file=%s n=%s", reason, source, n)
    control = {"type": "gap", "reason": reason, "dropped_events": 1, "dropped_bytes": len(body) + 1,
               "first_dropped_at": t, "last_dropped_at": t, "sessions": sessions}
    return orjson.dumps({"v": spool.VERSION, "k": spool.KIND_CONTROL, "n": n, "t": t, "control": control})


def _sweep_when_due(spool_dir: Path, data_files: list[SpoolFile], now: float) -> None:
    marker = spool.blobs_dir(spool_dir) / SWEEP_MARKER
    try:
        if 0 <= now - os.stat(marker).st_mtime < BLOB_SWEEP_SECONDS:
            return
    except FileNotFoundError:
        pass
    except OSError:
        return
    try:
        # The marker first: a sweep that keeps failing is retried once per interval, not on every scan.
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), spool.FILE_MODE))
        os.utime(marker)
    except FileNotFoundError:
        return  # no blobs directory: no line was ever written with blobs
    except OSError as exc:
        log.warning("Spool blob sweep skipped error_type=%s", type(exc).__name__)
        return
    try:
        files, size = sweep_blobs(spool_dir, data_files=data_files, now=now)
    except OSError as exc:
        log.warning("Spool blob sweep failed error_type=%s", type(exc).__name__)
        return
    if files:
        log.info("Swept spool blobs files=%s bytes=%s", files, size)


def _sweep_quarantine_when_due(spool_dir: Path, max_bytes: int | None, max_age_seconds: float | None,
                               now: float) -> None:
    # In memory: a marker file in quarantine/ would count as a quarantined file, and blobs/ may not exist.
    key = os.fspath(spool_dir)
    if 0 <= now - _quarantine_swept.get(key, float("-inf")) < BLOB_SWEEP_SECONDS:
        return
    _quarantine_swept[key] = now
    try:
        files, size = sweep_quarantine(spool_dir, max_bytes=max_bytes, max_age_seconds=max_age_seconds, now=now)
    except OSError as exc:
        log.warning("Spool quarantine sweep failed error_type=%s", type(exc).__name__)
        return
    if files:
        log.info("Swept quarantined spool files files=%s bytes=%s", files, size)


def sweep_blobs(spool_dir: Path, *, data_files: list[SpoolFile], now: float | None = None,
                margin: float = spool.BLOB_MARGIN_SECONDS,
                max_read_bytes: int = OLD_DATA_READ_BYTES) -> tuple[int, int]:
    """Delete the blob files no data file can reference: ``(files, bytes)`` deleted.

    ``data_files`` are the ``SpoolFile``s of a complete scan: the data files that exist, consumed or
    not. A writer refreshes the mtime of every blob a line references when it writes the line, or
    at most ``spool.BLOB_REFRESH_SECONDS`` earlier, so no blob of an existing or later line is older
    than the oldest data file's mtime minus ``margin``. Data files unmodified for ``OLD_DATA_SECONDS``
    are read instead: the blobs they reference are kept whatever their age and the cutoff follows the
    other files, so one stuck file does not keep every later blob until the spool is full. When the
    old files hold more than ``max_read_bytes``, or one cannot be read, every file sets the cutoff.

    A candidate is renamed aside before its mtime is read again: a writer that refreshed it in
    between keeps it (it is put back), and a writer that looks for it afterwards finds it missing
    and stores it again. Temporary files of writers that died and entries left by an interrupted
    sweep are cleaned up too.
    """
    now = time.time() if now is None else now
    data_files = list(data_files)
    recent = [item for item in data_files if item.mtime >= now - OLD_DATA_SECONDS]
    referenced: set[str] | None = set()
    if len(recent) < len(data_files):
        referenced = _blob_references([item for item in data_files if item.mtime < now - OLD_DATA_SECONDS],
                                      max_read_bytes)
        if referenced is None:
            referenced, recent = set(), data_files
    cutoff = min(now, min((item.mtime for item in recent), default=now)) - margin
    directory = spool.blobs_dir(spool_dir)
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0, 0
    files = size = 0
    for entry in entries:
        name = entry.name
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            status = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if spool.is_blob_name(name):
            if status.st_mtime >= cutoff or name in referenced:
                continue
            aside = directory / f".{name}.{uuid.uuid4().hex[:8]}{SWEEP_SUFFIX}"
            try:
                os.rename(entry.path, aside)
            except OSError:
                continue
            deleted = _settle(aside, directory / name, cutoff)
        elif name.startswith(".") and name.endswith(SWEEP_SUFFIX) and spool.is_blob_name(name[1:65]):
            # Left by a sweep that stopped between its rename and the unlink.
            deleted = _settle(Path(entry.path), directory / name[1:65], cutoff, keep=name[1:65] in referenced)
        elif name.startswith(".") and name.endswith(".tmp") and status.st_mtime < now - margin:
            deleted = status.st_size if remove_file(entry.path) else None
        else:
            continue
        if deleted is not None:
            files += 1
            size += deleted
    return files, size


def _blob_references(files: list[SpoolFile], max_bytes: int) -> set[str] | None:
    """The blobs referenced in ``files``; ``None`` when that takes reading over ``max_bytes`` or a file fails."""
    if sum(item.size for item in files) > max_bytes:
        return None
    found: set[str] = set()
    # A reference cut by a chunk boundary is found in the next window: keep all but its last byte.
    overlap = len(spool.BLOB_REFERENCE) + 64 + 1
    budget = max_bytes
    for item in files:
        try:
            handle = _open_listed(item)
            if handle is None:
                continue  # consumed and deleted since the scan
            with handle:
                tail = b""
                while chunk := handle.read(READ_CHUNK_BYTES):
                    budget -= len(chunk)
                    if budget < 0:
                        return None
                    window = tail + chunk
                    found.update(sha.decode("ascii") for sha in _BLOB_REFERENCE.findall(window))
                    tail = window[-overlap:]
        except OSError:
            return None
    return found


def _open_listed(item: SpoolFile):
    """A listed data file opened under its current name (a ``.part`` may be closed since); ``None`` when gone."""
    names = [item.path] if item.closed else [item.path, item.path.with_name(spool.file_name(item.counter))]
    for path in names:
        try:
            return open(path, "rb")
        except FileNotFoundError:
            continue
    return None


def _settle(aside: Path, path: Path, cutoff: float, *, keep: bool = False) -> int | None:
    """Delete a blob renamed aside, or put it back when refreshed or ``keep``: bytes deleted, else ``None``."""
    try:
        status = os.stat(aside)
        if status.st_mtime < cutoff and not keep:
            os.unlink(aside)
            return status.st_size
        if os.path.exists(path):
            os.unlink(aside)  # the writer has stored it again
        else:
            os.replace(aside, path)
    except OSError:
        pass
    return None


def counter_range(path: Path, offset: int) -> tuple[int | None, int | None]:
    """Lowest and highest line counter recognizable after ``offset``, even in damaged content."""
    low = high = None
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            for line in handle:
                match = _LINE_COUNTER.match(line)
                if match is None:
                    continue
                value = int(match.group(1))
                low = value if low is None else min(low, value)
                high = value if high is None else max(high, value)
    except OSError:
        pass
    return low, high


def quarantine_file(spool_dir: Path, item: SpoolFile, *, reason: str, detail: dict,
                    max_bytes: int | None = None) -> Path | None:
    """Move ``item`` to ``quarantine/`` with a ``.reason`` sidecar; ``None`` when the move failed.

    With ``max_bytes`` the oldest quarantined files are then deleted until their data bytes fit,
    the file just moved too when it alone is larger.
    """
    directory = Path(spool_dir) / spool.QUARANTINE_DIR
    path = item.path if item.path.exists() else item.path.with_name(
        spool.file_name(item.counter, closed=not item.closed))
    try:
        spool.ensure_private_dir(directory)
        target = directory / f"{item.producer_id}__{path.name}"
        if target.exists():
            target = directory / f"{item.producer_id}__{uuid.uuid4().hex[:8]}__{path.name}"
        os.replace(path, target)
    except OSError:
        return None
    try:
        spool.write_json_atomic(target.with_name(target.name + spool.REASON_SUFFIX), {
            "version": spool.VERSION, "producer_id": item.producer_id, "file": path.name,
            "reason": reason, "quarantined_at": spool.timestamp(), **detail})
    except OSError:
        pass
    if max_bytes is not None:
        try:
            files, size = _prune_quarantine(_quarantined(directory)[0], max_bytes)
        except OSError as exc:
            log.warning("Spool quarantine limit not applied error_type=%s", type(exc).__name__)
            return target
        if files:
            log.info("Deleted the oldest quarantined spool files files=%s bytes=%s", files, size)
        if not target.exists():
            log.warning("Quarantined spool file deleted at once: larger than the quarantine limit "
                        "producer_id=%s file=%s max_bytes=%s", item.producer_id, path.name, max_bytes)
    return target


@dataclass(frozen=True)
class _Quarantined:
    path: Path
    size: int
    #: When it was quarantined: the mtime of its ``.reason`` sidecar, else its own.
    mtime: float
    reason: Path | None


def _quarantined(directory: Path) -> tuple[list[_Quarantined], list[tuple[Path, float]]]:
    """Quarantined data files, oldest first, and the ``.reason`` sidecars whose data file is gone."""
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return [], []
    statuses = {}
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                statuses[entry.name] = entry.stat(follow_symlinks=False)
        except OSError:
            continue
    files, orphans = [], []
    for name, status in statuses.items():
        if spool.is_quarantined_file(name):
            sidecar = statuses.get(name + spool.REASON_SUFFIX)
            files.append(_Quarantined(directory / name, status.st_size,
                                      status.st_mtime if sidecar is None else sidecar.st_mtime,
                                      None if sidecar is None else directory / (name + spool.REASON_SUFFIX)))
        elif (not name.startswith(".") and name.endswith(spool.REASON_SUFFIX)
              and name[:-len(spool.REASON_SUFFIX)] not in statuses):
            orphans.append((directory / name, status.st_mtime))
    files.sort(key=lambda quarantined: (quarantined.mtime, quarantined.path.name))
    return files, orphans


def _remove_quarantined(quarantined: _Quarantined) -> bool:
    # The data file first: a sidecar left behind is an orphan the age sweep removes later.
    if not remove_file(quarantined.path):
        return False
    if quarantined.reason is not None:
        remove_file(quarantined.reason)
    return True


def _prune_quarantine(files: list[_Quarantined], max_bytes: int) -> tuple[int, int]:
    """Delete quarantined files, oldest first, until at most ``max_bytes`` of data remain: ``(files, bytes)``."""
    total = sum(quarantined.size for quarantined in files)
    deleted = size = 0
    for quarantined in files:
        if total <= max_bytes:
            break
        if _remove_quarantined(quarantined):
            total -= quarantined.size
            deleted += 1
            size += quarantined.size
    return deleted, size


def sweep_quarantine(spool_dir: Path, *, max_bytes: int | None, max_age_seconds: float | None,
                     now: float | None = None) -> tuple[int, int]:
    """Delete quarantined files older than ``max_age_seconds``, then the oldest beyond ``max_bytes`` of data.

    Age counts from the ``.reason`` sidecar's mtime, else the data file's. A data file goes together
    with its sidecar; sidecars without a data file go once that old. ``None`` disables a limit.
    ``(files, bytes)`` of the data files deleted.
    """
    now = time.time() if now is None else now
    files, orphans = _quarantined(Path(spool_dir) / spool.QUARANTINE_DIR)
    deleted = size = 0
    if max_age_seconds is not None:
        cutoff = now - max_age_seconds
        kept = []
        for quarantined in files:
            if quarantined.mtime < cutoff and _remove_quarantined(quarantined):
                deleted += 1
                size += quarantined.size
            else:
                kept.append(quarantined)
        files = kept
        for path, mtime in orphans:
            if mtime < cutoff:
                remove_file(path)
    if max_bytes is not None:
        pruned, pruned_bytes = _prune_quarantine(files, max_bytes)
        deleted += pruned
        size += pruned_bytes
    return deleted, size


def locate(item: SpoolFile) -> Path | None:
    """The file's current path: it may have been renamed from ``.part`` since the scan."""
    closed = item.path.with_name(spool.file_name(item.counter))
    if closed.exists():
        return closed
    if item.path.exists():
        return item.path
    return None


def stat_signature(path: Path) -> tuple[int, float] | None:
    try:
        status = os.stat(path)
    except OSError:
        return None
    return status.st_size, status.st_mtime


def remove_if_unchanged(path: Path, signature: tuple[int, float] | None) -> bool:
    """Delete ``path`` if its ``(st_size, st_mtime)`` still equals ``signature``; False when missing or changed.

    For an abandoned ``.part`` consumed while its writer may have resumed: the file is renamed aside
    before it is checked, so lines written up to the check make it differ and it is put back.
    """
    path = Path(path)
    aside = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}{REMOVE_SUFFIX}")
    try:
        os.rename(path, aside)
    except OSError:
        return False
    try:
        status = os.stat(aside)
        if signature is not None and (status.st_size, status.st_mtime) == tuple(signature):
            os.unlink(aside)
            return True
    except OSError:
        pass
    try:
        os.replace(aside, path)
    except OSError as exc:
        log.warning("Could not put back a changed spool file path=%s error_type=%s", path, type(exc).__name__)
    return False


def remove_file(path: Path) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def remove_producer_dir(path: Path) -> bool:
    """Remove a finished producer directory (``producer.json`` and nothing else)."""
    try:
        for child in os.scandir(path):
            if child.name != spool.PRODUCER_FILE and not child.name.startswith("."):
                return False
        for child in os.scandir(path):
            os.unlink(child.path)
        os.rmdir(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def monotonic_age(mtime: float | None, now: float | None = None) -> float:
    if mtime is None:
        return 0.0
    return max(0.0, (time.time() if now is None else now) - mtime)
