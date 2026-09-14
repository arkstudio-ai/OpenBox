"""Cold event segments: the archived form of ``trajectory_events`` (SPEC 8.10).

A segment holds the stored event rows ``from_seq..to_seq`` as JSON lines, one
canonical object per line with exactly SEGMENT_FIELDS, compressed with zstd.
The ``trajectory_segments`` row keeps the sha256 of the uncompressed JSONL and
every load verifies it, so a damaged, truncated or swapped object reads as
CorruptContent (HTTP 409), never as a shorter history.
"""
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone

import zstandard

from trajectory.storage import ZSTD_LEVEL, decode_blob
from trajectory.types import CorruptContent

#: Keys of one segment line, in the order of SPEC 8.10.
SEGMENT_FIELDS = (
    "event_id", "trajectory_id", "seq", "type", "version", "user_id", "session_id", "source_session_id",
    "request_id", "call_id", "agent_id", "context", "data", "hints", "content_hash", "occurred_at", "recorded_at",
)
NULLABLE_FIELDS = frozenset({"request_id", "call_id", "agent_id", "hints"})
COMPRESSION = "zstd"


def _timestamp(value) -> str:
    # Full precision: rows rebuilt from a segment keep their recorded instants.
    if isinstance(value, datetime):
        value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"Invalid segment timestamp: {value!r}")


def _line(row: dict) -> bytes:
    # Compact JSON in SEGMENT_FIELDS order that keeps the stored key order of
    # context, data and hints: reducers preview structured values as stored.
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def segment_row(event) -> dict:
    """The segment line dict of a stored ``TrajectoryEvent`` row."""
    row = {field: getattr(event, field) for field in SEGMENT_FIELDS}
    row["occurred_at"], row["recorded_at"] = _timestamp(event.occurred_at), _timestamp(event.recorded_at)
    return row


def encode_segment(rows: list[dict]) -> tuple[bytes, dict]:
    """(zstd bytes, metadata) for contiguous event rows of one trajectory.

    Metadata: sha256 of the raw JSONL, raw_bytes, stored_bytes, event_count,
    from_seq and to_seq. Raises ValueError for an empty, non-contiguous or
    mixed batch and for rows with missing or unknown keys.
    """
    if not rows:
        raise ValueError("A segment needs at least one event")
    lines = []
    first = int(rows[0]["seq"])
    trajectory_id = rows[0].get("trajectory_id")
    for offset, row in enumerate(rows):
        unknown = set(row) - set(SEGMENT_FIELDS)
        if unknown:
            raise ValueError(f"Unknown segment row keys: {sorted(unknown)}")
        missing = [field for field in SEGMENT_FIELDS if row.get(field) is None and field not in NULLABLE_FIELDS]
        if missing:
            raise ValueError(f"Segment row misses {missing}")
        line = {field: row.get(field) for field in SEGMENT_FIELDS}
        line["seq"], line["version"] = int(row["seq"]), int(row["version"])
        if line["seq"] != first + offset:
            raise ValueError("Segment rows must be contiguous and ordered by seq")
        if row["trajectory_id"] != trajectory_id:
            raise ValueError("A segment holds events of one trajectory")
        line["occurred_at"], line["recorded_at"] = _timestamp(row["occurred_at"]), _timestamp(row["recorded_at"])
        lines.append(_line(line))
    raw = b"\n".join(lines) + b"\n"
    stored = zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(raw)
    return stored, {"sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw), "stored_bytes": len(stored),
                    "event_count": len(rows), "from_seq": first, "to_seq": first + len(rows) - 1}


def _parse(line: bytes, seq: int | None = None) -> dict:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CorruptContent("Invalid trajectory segment line") from exc
    if not isinstance(value, dict) or any(field not in value for field in SEGMENT_FIELDS):
        raise CorruptContent("Invalid trajectory segment row")
    if seq is not None and value["seq"] != seq:
        raise CorruptContent("Trajectory segment sequence is not contiguous")
    return value


class SegmentLines:
    """Verified JSONL of one segment; rows are parsed per request, so cached
    lines never hand one caller's mutable objects to another."""

    __slots__ = ("from_seq", "to_seq", "lines", "size")

    def __init__(self, raw: bytes, *, expected_sha256: str | None = None):
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise CorruptContent("Trajectory segment digest mismatch")
        lines = raw.split(b"\n")
        if len(lines) < 2 or lines[-1] != b"" or any(not line for line in lines[:-1]):
            raise CorruptContent("Truncated or empty trajectory segment")
        self.lines = lines[:-1]
        self.size = len(raw)
        self.from_seq = _parse(self.lines[0])["seq"]
        self.to_seq = self.from_seq + len(self.lines) - 1
        if not isinstance(self.from_seq, int) or _parse(self.lines[-1])["seq"] != self.to_seq:
            raise CorruptContent("Trajectory segment sequence is not contiguous")

    def rows(self, lo: int | None = None, hi: int | None = None) -> list[dict]:
        """Rows with lo <= seq <= hi (default: all), each checked against its position."""
        start = max(self.from_seq, self.from_seq if lo is None else lo)
        end = min(self.to_seq, self.to_seq if hi is None else hi)
        return [_parse(self.lines[seq - self.from_seq], seq) for seq in range(start, end + 1)]


def decode_segment(data: bytes, *, expected_sha256: str | None = None) -> list[dict]:
    """Rows of zstd segment bytes; CorruptContent on bad compression, digest or rows."""
    return SegmentLines(decode_blob(data, COMPRESSION), expected_sha256=expected_sha256).rows()


def _field(row, name: str):
    return row[name] if isinstance(row, Mapping) else getattr(row, name)


async def load_segment_lines(blob_store, segment_row) -> SegmentLines:
    """Download, decompress and verify the object of a ``trajectory_segments`` row (ORM row or dict)."""
    compression = _field(segment_row, "compression") or COMPRESSION
    if compression != COMPRESSION:
        raise CorruptContent(f"Unsupported trajectory segment compression: {compression!r}")
    try:
        stored = await blob_store.get(_field(segment_row, "storage_key"))
    except FileNotFoundError as exc:
        raise CorruptContent("Archived trajectory segment is missing") from exc
    lines = SegmentLines(decode_blob(stored, COMPRESSION), expected_sha256=_field(segment_row, "sha256"))
    if (lines.from_seq, lines.to_seq) != (int(_field(segment_row, "from_seq")), int(_field(segment_row, "to_seq"))) \
            or len(lines.lines) != int(_field(segment_row, "event_count")):
        raise CorruptContent("Trajectory segment does not match its manifest row")
    return lines


async def load_segment(blob_store, segment_row) -> list[dict]:
    """Verified rows of one archived segment (see load_segment_lines)."""
    rows = (await load_segment_lines(blob_store, segment_row)).rows()
    trajectory_id = _field(segment_row, "trajectory_id")
    if any(row["trajectory_id"] != trajectory_id for row in rows):
        raise CorruptContent("Trajectory segment holds events of another trajectory")
    return rows
