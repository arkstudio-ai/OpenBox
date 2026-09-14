"""Spool file discovery and reading for ingest (SPEC §3.1, §8.2).

A data file is ready when it is closed (``.jsonl``) or is an open ``.part``
whose mtime is older than ``TRAJECTORY_SPOOL_ABANDON_SECONDS``. Within one
producer files are consumed strictly by counter: a lower counter that is not
ready blocks the producer. Across producers the oldest ready file goes first.
Offsets are tracked under the closed name, so a ``.part`` renamed while it was
being consumed keeps its progress.

Everything here is blocking file I/O; the ingest service calls it through
``asyncio.to_thread``. Lines are read in chunks and a batch stops at its line
and byte limits, so memory stays bounded by one batch plus one line.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import orjson

from trajectory import spool

READ_CHUNK_BYTES = 1024 * 1024
_LINE_COUNTER = re.compile(rb'^\{"v":\d+,"k":"(?:event|control)","n":(\d+),')


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


@dataclass(frozen=True)
class RawLine:
    data: bytes
    start: int
    end: int


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


def scan_spool(spool_dir: Path, *, documents: dict[str, dict | None] | None = None) -> SpoolScan:
    """Producers and their data files (sorted by counter), with size totals.

    ``documents`` caches parsed ``producer.json`` files between scans.
    """
    scan = SpoolScan(producers={})
    try:
        entries = list(os.scandir(producers_dir(spool_dir)))
    except FileNotFoundError:
        return scan
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            children = list(os.scandir(entry.path))
        except OSError:
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
            except OSError:
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
               chunk_size: int = READ_CHUNK_BYTES) -> BatchRead:
    """Complete lines from ``offset``: at most ``max_lines`` and, after the first, ``max_bytes``."""
    lines: list[RawLine] = []
    consumed = 0
    line_start = offset
    pieces: list[bytes] = []
    eof = False
    limited = False
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
                if lines and consumed + length > max_bytes:
                    limited = True
                    break
                lines.append(RawLine(body, line_start, line_start + length))
                consumed += length
                line_start += length
                index = newline + 1
                if len(lines) >= max_lines:
                    limited = True
                    break
        tail = handle.tell() - line_start if eof else 0
    return BatchRead(lines, line_start, eof, tail)


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


def quarantine_file(spool_dir: Path, item: SpoolFile, *, reason: str, detail: dict) -> Path | None:
    """Move ``item`` to ``quarantine/`` with a ``.reason`` sidecar; ``None`` when the move failed."""
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
        spool.write_json_atomic(target.with_name(target.name + ".reason"), {
            "version": spool.VERSION, "producer_id": item.producer_id, "file": path.name,
            "reason": reason, "quarantined_at": spool.timestamp(), **detail})
    except OSError:
        pass
    return target


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
