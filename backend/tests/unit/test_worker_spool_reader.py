"""Spool reader (SPEC §8.2): readiness, per-producer order, bounded batch reads, quarantine."""
import os
import time

from trajectory import spool
from trajectory.worker import spool_reader
from trajectory.worker.spool_reader import (counter_range, locate, quarantine_file, read_batch, remove_producer_dir,
    scan_spool, select_ready)


def _line(n: int, text: str = "x") -> bytes:
    return spool.encode_event_line(n, b"2026-09-14T08:00:00.000Z", b'{"type":"input.accepted","data":{"text":"%s"}}'
                                   % text.encode())


def _write(directory, counter, lines, *, closed=True, mtime=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / spool.file_name(counter, closed=closed)
    path.write_bytes(b"".join(lines))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_scan_lists_data_files_by_counter_with_totals(tmp_path):
    root = tmp_path / "producers"
    now = time.time()
    _write(root / "p1", 2, [_line(3)], mtime=now - 5)
    _write(root / "p1", 1, [_line(1), _line(2)], mtime=now - 10)
    _write(root / "p1", 3, [_line(4)], closed=False, mtime=now - 1)
    (root / "p1" / spool.PRODUCER_FILE).write_bytes(b'{"version":1,"producer_id":"p1","role":"backend"}')
    (root / "p1" / "notes.txt").write_text("ignored")
    (root / "stray-file").write_text("ignored")
    scan = scan_spool(tmp_path)
    producer = scan.producers["p1"]
    assert [(item.counter, item.closed) for item in producer.files] == [(1, True), (2, True), (3, False)]
    assert producer.document["role"] == "backend"
    assert scan.files == 3
    assert scan.bytes == sum(item.size for item in producer.files)
    assert abs(scan.oldest_mtime - (now - 10)) < 1
    assert producer.files[2].name == spool.file_name(3)
    assert scan_spool(tmp_path / "missing").producers == {}


def test_open_file_blocks_its_producer_until_abandoned(tmp_path):
    root = tmp_path / "producers"
    now = time.time()
    _write(root / "a", 1, [_line(1)], closed=False, mtime=now - 5)
    _write(root / "a", 2, [_line(2)], mtime=now - 100)
    _write(root / "b", 1, [_line(1)], mtime=now - 50)
    _write(root / "c", 7, [_line(9)], closed=False, mtime=now - 120)
    scan = scan_spool(tmp_path)
    ready = select_ready(scan, set(), now=now, abandon_seconds=60)
    # a#1 is a live .part: a#2 must wait although it is older. c#7 is abandoned.
    assert [(item.producer_id, item.counter) for item in ready] == [("c", 7), ("b", 1)]
    # Once a#1 counts as consumed (its deletion pending), a#2 is next.
    ready = select_ready(scan, {("a", spool.file_name(1))}, now=now, abandon_seconds=60)
    assert [(item.producer_id, item.counter) for item in ready] == [("c", 7), ("a", 2), ("b", 1)]
    assert select_ready(scan, set(), now=now + 60, abandon_seconds=60)[0].producer_id == "c"


def test_batches_respect_line_and_byte_limits_and_resume(tmp_path):
    lines = [_line(n, "y" * n) for n in range(1, 21)]
    path = _write(tmp_path, 1, lines)
    batch = read_batch(path, 0, max_lines=5, max_bytes=10**9, chunk_size=7)
    assert [line.data + b"\n" for line in batch.lines] == lines[:5]
    assert batch.end_offset == sum(len(line) for line in lines[:5])
    assert not batch.eof and batch.tail_bytes == 0
    assert batch.lines[1].start == batch.lines[0].end
    budget = len(lines[5]) + len(lines[6]) + 1
    second = read_batch(path, batch.end_offset, max_lines=100, max_bytes=budget)
    assert [line.data + b"\n" for line in second.lines] == lines[5:7]
    rest = read_batch(path, second.end_offset, max_lines=100, max_bytes=10**9, chunk_size=3)
    assert [line.data + b"\n" for line in rest.lines] == lines[7:]
    assert rest.eof and rest.tail_bytes == 0 and rest.end_offset == path.stat().st_size
    empty = read_batch(path, rest.end_offset, max_lines=100, max_bytes=10**9)
    assert empty.lines == [] and empty.eof and empty.end_offset == rest.end_offset


def test_first_line_is_returned_even_when_larger_than_the_byte_limit(tmp_path):
    big = _line(1, "z" * 50000)
    path = _write(tmp_path, 1, [big, _line(2)])
    batch = read_batch(path, 0, max_lines=10, max_bytes=100, chunk_size=4096)
    assert len(batch.lines) == 1 and batch.lines[0].data + b"\n" == big
    assert not batch.eof


def test_torn_tail_is_not_returned(tmp_path):
    path = _write(tmp_path, 1, [_line(1), _line(2), b'{"v":1,"k":"event","n":3,"t":"2026'], closed=False)
    batch = read_batch(path, 0, max_lines=10, max_bytes=10**9, chunk_size=16)
    assert len(batch.lines) == 2
    assert batch.eof and batch.tail_bytes == len(b'{"v":1,"k":"event","n":3,"t":"2026')


def test_counter_range_reads_damaged_content(tmp_path):
    path = _write(tmp_path, 1, [_line(4), b"garbage\n", b'{"v":1,"k":"control","n":9,"t":broken\n', _line(6)])
    assert counter_range(path, 0) == (4, 9)
    assert counter_range(path, path.stat().st_size) == (None, None)


def test_quarantine_moves_the_file_with_a_reason(tmp_path):
    root = tmp_path / "producers" / "p1"
    path = _write(root, 3, [_line(1), b"not json\n"])
    item = scan_spool(tmp_path).producers["p1"].files[0]
    target = quarantine_file(tmp_path, item, reason="unparsable_line", detail={"offset": 10, "error": "SpoolFormatError"})
    assert target.parent == tmp_path / "quarantine" and target.name == f"p1__{path.name}"
    assert not path.exists() and target.read_bytes().endswith(b"not json\n")
    reason = spool_reader.read_producer_document(target.with_name(target.name + ".reason"))
    assert reason["reason"] == "unparsable_line" and reason["offset"] == 10 and reason["producer_id"] == "p1"
    assert (os.stat(tmp_path / "quarantine").st_mode & 0o777) == 0o700
    # A second file with the same name does not overwrite the first.
    _write(root, 3, [_line(1)])
    item = scan_spool(tmp_path).producers["p1"].files[0]
    second = quarantine_file(tmp_path, item, reason="unsupported_version", detail={})
    assert second != target and second.exists() and target.exists()


def test_locate_follows_a_rename_and_directories_are_removed_only_when_empty(tmp_path):
    root = tmp_path / "producers" / "p1"
    part = _write(root, 1, [_line(1)], closed=False)
    item = scan_spool(tmp_path).producers["p1"].files[0]
    os.replace(part, part.with_name(spool.file_name(1)))
    assert locate(item) == root / spool.file_name(1)
    (root / spool.PRODUCER_FILE).write_text("{}")
    assert remove_producer_dir(root) is False
    os.unlink(root / spool.file_name(1))
    assert locate(item) is None
    assert remove_producer_dir(root) is True and not root.exists()
    assert remove_producer_dir(root) is True
