"""Spool format v1 helpers shared by the emitter and the worker reader."""
from datetime import datetime, timezone
import os
import stat

import orjson
import pytest

from trajectory import spool
from trajectory.types import iso


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_file_names_are_twenty_digit_counters_and_parse_back():
    assert spool.file_name(1) == "00000000000000000001.jsonl"
    assert spool.file_name(42, closed=False) == "00000000000000000042.jsonl.part"
    assert spool.parse_file_name("00000000000000000042.jsonl.part") == (42, False)
    assert spool.parse_file_name(spool.file_name(7)) == (7, True)
    for other in ("producer.json", "1.jsonl", "00000000000000000001.jsonl.tmp", ".budgets.json.1a2b.tmp"):
        assert spool.parse_file_name(other) is None
    assert sorted([spool.file_name(10), spool.file_name(9)]) == [spool.file_name(9), spool.file_name(10)]


def test_producer_id_orders_by_start_and_keeps_path_safe():
    started = datetime(2026, 9, 14, 8, 0, 1, tzinfo=timezone.utc).timestamp()
    identifier = spool.producer_id(started, "api host/01", 42, "abcdef0123456789")
    assert identifier == "20260914080001-api_host_01-42-abcdef01"
    assert spool.producer_id(started - 1, "zzz", 99999, "ffffffff") < identifier
    assert len(spool.producer_id(started, "h" * 500, 4194304, "0" * 32)) <= 128
    assert spool.producer_id(started, "", 1, "00000000").split("-")[1] == "host"


def test_timestamps_are_utc_milliseconds_and_match_event_iso():
    assert spool.timestamp(0.5) == "1970-01-01T00:00:00.500Z"
    epoch = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc).timestamp() + 0.123
    assert spool.timestamp(epoch) == "2026-09-14T08:00:00.123Z"
    assert spool.timestamp_bytes(epoch + 0.9999999) == b"2026-09-14T08:00:01.123Z"
    for sample in (epoch, epoch + 0.0005, epoch + 59.9994):
        assert spool.timestamp(sample) == iso(datetime.fromtimestamp(sample, timezone.utc))


def test_event_and_control_lines_have_the_exact_v1_shape():
    t = b"2026-09-14T08:00:00.123Z"
    event = orjson.dumps({"type": "request.prepared", "data": {"text": "a\nb"}})
    line = spool.encode_event_line(42, t, event)
    assert line == b'{"v":1,"k":"event","n":42,"t":"2026-09-14T08:00:00.123Z","event":' + event + b"}\n"
    assert line.count(b"\n") == 1
    assert spool.decode_line(line) == {"v": 1, "k": "event", "n": 42, "t": t.decode(),
                                        "event": {"type": "request.prepared", "data": {"text": "a\nb"}}}
    control = spool.encode_control_line(43, t, b'{"type":"producer.goodbye","last_n":43}')
    assert control == (b'{"v":1,"k":"control","n":43,"t":"2026-09-14T08:00:00.123Z",'
                       b'"control":{"type":"producer.goodbye","last_n":43}}\n')
    assert spool.decode_line(control)["control"]["last_n"] == 43


def test_readers_reject_unknown_versions_and_malformed_lines_but_ignore_unknown_keys():
    good = {"v": 1, "k": "control", "n": 1, "t": "2026-09-14T08:00:00.000Z", "control": {"type": "gap"}}
    assert spool.decode_line(orjson.dumps({**good, "future": {"x": 1}}))["future"] == {"x": 1}
    # true and 1.0 compare equal to 1 in Python but are not format version 1.
    for version in (2, True, 1.0, "1", None):
        with pytest.raises(spool.UnsupportedSpoolVersion):
            spool.decode_line(orjson.dumps({**good, "v": version}))
    malformed = [{**good, "k": "other"}, {**good, "n": 0}, {**good, "n": True}, {**good, "n": "1"},
                 {key: value for key, value in good.items() if key != "t"}, {**good, "control": []},
                 {**good, "control": {"reason": "no type"}}, {"v": 1, "k": "event", "n": 2, "t": "x", "event": 5}]
    for record in malformed:
        with pytest.raises(spool.SpoolFormatError):
            spool.decode_line(orjson.dumps(record))
    for raw in (b"[1]", b"not json", orjson.dumps(good)[:-3]):
        with pytest.raises(spool.SpoolFormatError):
            spool.decode_line(raw)


def test_producer_document_and_atomic_json_files_are_private(tmp_path):
    started = datetime(2026, 9, 14, 8, 0, 0, 123000, tzinfo=timezone.utc).timestamp()
    document = spool.producer_document("p1", role="backend", started=started, hostname="host", pid=7)
    assert set(document) == {"version", "producer_id", "boot_id", "hostname", "pid", "role", "started_at"}
    assert document["started_at"] == "2026-09-14T08:00:00.123Z" and document["version"] == 1
    assert isinstance(spool.boot_id(), str) and spool.boot_id()
    target = tmp_path / spool.PRODUCER_FILE
    spool.write_json_atomic(target, document)
    spool.write_json_atomic(target, {**document, "pid": 8})
    assert orjson.loads(target.read_bytes())["pid"] == 8
    assert mode(target) == spool.FILE_MODE
    assert sorted(os.listdir(tmp_path)) == [spool.PRODUCER_FILE]


def test_private_directories_are_created_0700_below_the_existing_parent(tmp_path):
    before = mode(tmp_path)
    leaf = tmp_path / "spool" / spool.PRODUCERS_DIR / "producer"
    spool.ensure_private_dir(leaf)
    spool.ensure_private_dir(leaf)
    for directory in (tmp_path / "spool", tmp_path / "spool" / spool.PRODUCERS_DIR, leaf):
        assert mode(directory) == spool.DIR_MODE
    assert mode(tmp_path) == before


def test_spool_usage_counts_only_producer_files(tmp_path):
    assert spool.spool_usage_bytes(tmp_path / "missing") == 0
    (tmp_path / "producers" / "p1").mkdir(parents=True)
    (tmp_path / "producers" / "p2").mkdir()
    (tmp_path / "control").mkdir()
    (tmp_path / "producers" / "p1" / spool.file_name(1)).write_bytes(b"x" * 10)
    (tmp_path / "producers" / "p2" / spool.file_name(1, closed=False)).write_bytes(b"x" * 5)
    (tmp_path / "producers" / "stray").write_bytes(b"x" * 1000)
    (tmp_path / "control" / spool.BUDGETS_FILE).write_bytes(b"x" * 100)
    assert spool.spool_usage_bytes(tmp_path) == 15


def test_budget_documents_round_trip_and_invalid_files_raise(tmp_path):
    document = spool.budgets_document(
        sessions={"root": {"level": "degraded", "reason": "trajectory_bytes", "since": "2026-09-14T08:00:00Z"},
                  "fine": {"level": "normal"}, "odd": {"level": "paused"}, "bad": "blocked"},
        users={"u1": {"level": "blocked", "reason": "user_daily_bytes", "since": "2026-09-14T08:00:00Z"}})
    spool.write_budgets(tmp_path, document)
    path = spool.budgets_path(tmp_path)
    assert path == tmp_path / spool.CONTROL_DIR / spool.BUDGETS_FILE
    assert mode(path) == spool.FILE_MODE and mode(path.parent) == spool.DIR_MODE
    assert set(orjson.loads(path.read_bytes())) == {"version", "generated_at", "sessions", "users"}
    assert spool.parse_budgets(path.read_bytes()) == ({"root": "degraded"}, {"u1": "blocked"})
    assert spool.parse_budgets(b'{"version":1}') == ({}, {})
    for raw in (b"{", b"[]", b'{"version":2,"sessions":{}}', b'{"version":1,"sessions":[]}',
                b'{"version":1,"users":"u1"}', b'{"version":true}', b'{"version":1.0}'):
        with pytest.raises(spool.SpoolFormatError):
            spool.parse_budgets(raw)
