"""Cold event segments (SPEC 8.10): canonical JSONL lines, zstd, sha256 of the raw lines, verified loads."""
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import zstandard

from trajectory.segments import (SEGMENT_FIELDS, SegmentLines, decode_segment, encode_segment, load_segment,
    load_segment_lines)
from trajectory.storage import MemoryBlobStore
from trajectory.types import CorruptContent, canonical

AT = datetime(2026, 9, 14, 8, 0, 0, 123456, tzinfo=timezone.utc)
KEY = "trajectories/trj_a/segments/000000000005-000000000008.jsonl.zst"


def _row(seq, trajectory_id="trj_a", **overrides):
    row = {"event_id": f"evt_{seq}", "trajectory_id": trajectory_id, "seq": seq, "type": "input.accepted", "version": 1,
           "user_id": "user_a", "session_id": "session_a", "source_session_id": "session_a", "request_id": None,
           "call_id": None, "agent_id": None, "context": {"turn_id": "turn_a"}, "data": {"text": f"你好 {seq}"},
           "hints": {"preview": {"text": "你好"}} if seq % 2 else None, "content_hash": "a" * 64,
           "occurred_at": AT, "recorded_at": AT}
    row.update(overrides)
    return row


def _zstd(raw: bytes) -> bytes:
    return zstandard.ZstdCompressor().compress(raw)


def _manifest(meta, **overrides):
    row = {"trajectory_id": "trj_a", "storage_key": KEY, "compression": "zstd", **meta}
    row.update(overrides)
    return row


def test_round_trip_keeps_every_field_and_reports_its_metadata():
    rows = [_row(seq) for seq in range(5, 9)]
    stored, meta = encode_segment(rows)
    raw = zstandard.ZstdDecompressor().decompress(stored)
    assert meta == {"sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw), "stored_bytes": len(stored),
                    "event_count": 4, "from_seq": 5, "to_seq": 8}
    assert encode_segment(rows) == (stored, meta)
    lines = raw.split(b"\n")
    assert lines[-1] == b"" and len(lines) == 5
    # Compact UTF-8 lines in SEGMENT_FIELDS order.
    expected = {field: _row(5)[field] for field in SEGMENT_FIELDS}
    expected.update(occurred_at="2026-09-14T08:00:00.123456Z", recorded_at="2026-09-14T08:00:00.123456Z")
    assert lines[0] == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    decoded = decode_segment(stored, expected_sha256=meta["sha256"])
    assert [row["seq"] for row in decoded] == [5, 6, 7, 8]
    assert all(set(row) == set(SEGMENT_FIELDS) for row in decoded)
    assert decoded[1]["data"] == {"text": "你好 6"} and decoded[0]["hints"] == {"preview": {"text": "你好"}}


def test_nested_objects_keep_their_stored_key_order():
    # Reducers preview structured values as stored, so a segment must not sort them.
    stored, _ = encode_segment([_row(1, data={"zeta": {"b": 1, "a": 2}, "alpha": [{"y": 1, "x": 2}]})])
    [row] = decode_segment(stored)
    assert list(row["data"]) == ["zeta", "alpha"] and list(row["data"]["zeta"]) == ["b", "a"]
    assert list(row["data"]["alpha"][0]) == ["y", "x"]


def test_timestamps_strings_and_naive_datetimes_are_utc_instants():
    naive = datetime(2026, 9, 14, 8, 0)
    stored, _ = encode_segment([_row(1, occurred_at=naive, recorded_at="2026-09-14T08:00:01.5Z")])
    [row] = decode_segment(stored)
    assert row["occurred_at"] == "2026-09-14T08:00:00Z" and row["recorded_at"] == "2026-09-14T08:00:01.5Z"


@pytest.mark.parametrize("rows, message", [
    ([], "at least one"),
    ([_row(5), _row(7)], "contiguous"),
    ([_row(6), _row(5)], "contiguous"),
    ([_row(5), _row(6, trajectory_id="trj_b")], "one trajectory"),
    ([{**_row(5), "recorded_on": "2026-09-14"}], "Unknown segment row keys"),
    ([_row(5, data=None)], "misses"),
    ([{key: value for key, value in _row(5).items() if key != "content_hash"}], "misses"),
])
def test_invalid_batches_are_rejected_before_anything_is_written(rows, message):
    with pytest.raises(ValueError, match=message):
        encode_segment(rows)


def test_corrupt_bytes_digests_and_lines_are_corrupt_content():
    stored, meta = encode_segment([_row(seq) for seq in range(1, 4)])
    with pytest.raises(CorruptContent, match="digest"):
        decode_segment(stored, expected_sha256="0" * 64)
    with pytest.raises(CorruptContent):
        decode_segment(stored[:-4])
    with pytest.raises(CorruptContent):
        decode_segment(b"not zstd at all")
    good = [canonical(_row(1, occurred_at="t", recorded_at="t")), canonical(_row(2, occurred_at="t", recorded_at="t"))]
    for raw in (b"\n".join(good), b"\n".join([good[0], b"", good[1]]) + b"\n", good[0] + b"\n{not json\n",
                good[0] + b"\n" + canonical({"seq": 2}) + b"\n", good[1] + b"\n" + good[0] + b"\n", b"\n"):
        with pytest.raises(CorruptContent):
            decode_segment(_zstd(raw))


def test_line_ranges_parse_only_the_requested_rows():
    stored, meta = encode_segment([_row(seq) for seq in range(10, 20)])
    lines = SegmentLines(zstandard.ZstdDecompressor().decompress(stored), expected_sha256=meta["sha256"])
    assert (lines.from_seq, lines.to_seq, lines.size) == (10, 19, meta["raw_bytes"])
    assert [row["seq"] for row in lines.rows(12, 14)] == [12, 13, 14]
    assert [row["seq"] for row in lines.rows(0, 11)] == [10, 11]
    assert [row["seq"] for row in lines.rows(18, 99)] == [18, 19]
    assert lines.rows(20, 30) == [] and len(lines.rows()) == 10
    # Each call parses anew: callers never share mutable rows.
    first, second = lines.rows(10, 10), lines.rows(10, 10)
    first[0]["data"]["text"] = "changed"
    assert second[0]["data"]["text"] == "你好 10" and lines.rows(10, 10)[0]["data"]["text"] == "你好 10"


async def test_load_segment_verifies_the_object_against_its_manifest_row():
    store = MemoryBlobStore()
    stored, meta = encode_segment([_row(seq) for seq in range(5, 9)])
    await store.put(KEY, stored, content_type="application/zstd")
    rows = await load_segment(store, _manifest(meta))
    assert [row["event_id"] for row in rows] == ["evt_5", "evt_6", "evt_7", "evt_8"]
    assert [row["seq"] for row in await load_segment(store, SimpleNamespace(**_manifest(meta)))] == [5, 6, 7, 8]
    assert (await load_segment_lines(store, _manifest(meta, compression=None))).to_seq == 8
    for manifest, message in ((_manifest(meta, event_count=5), "manifest"), (_manifest(meta, from_seq=4), "manifest"),
                              (_manifest(meta, to_seq=9), "manifest"), (_manifest(meta, sha256="1" * 64), "digest"),
                              (_manifest(meta, compression="gzip"), "compression"),
                              (_manifest(meta, trajectory_id="trj_other"), "another trajectory"),
                              (_manifest(meta, storage_key=KEY + ".missing"), "missing")):
        with pytest.raises(CorruptContent, match=message):
            await load_segment(store, manifest)
    await store.put(KEY, stored[:-4], content_type="application/zstd", if_absent=False)
    with pytest.raises(CorruptContent):
        await load_segment(store, _manifest(meta))


@pytest.mark.parametrize("seq", ["1", True, 1.0, None])
def test_a_seq_that_is_not_an_integer_is_corrupt_content(seq):
    first = canonical({**_row(1, occurred_at="t", recorded_at="t"), "seq": seq})
    with pytest.raises(CorruptContent, match="sequence"):
        SegmentLines(first + b"\n")
    good = canonical(_row(1, occurred_at="t", recorded_at="t"))
    last = canonical({**_row(2, occurred_at="t", recorded_at="t"), "seq": seq})
    with pytest.raises(CorruptContent, match="sequence"):
        SegmentLines(good + b"\n" + last + b"\n")
