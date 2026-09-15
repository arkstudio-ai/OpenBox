"""Spool reader (SPEC §8.2): readiness, per-producer order, bounded batch reads, quarantine."""
import errno
import hashlib
import os
import stat
import time

from trajectory import spool
from trajectory.worker import spool_reader
from trajectory.worker.spool_reader import (counter_range, locate, quarantine_file, read_batch, remove_if_unchanged,
    remove_producer_dir, scan_spool, select_ready, stat_signature, sweep_blobs, sweep_quarantine)


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
    os.utime(root / "p1" / spool.PRODUCER_FILE, (now - 30, now - 30))
    (root / "p1" / "notes.txt").write_text("ignored")
    (root / "stray-file").write_text("ignored")
    scan = scan_spool(tmp_path)
    producer = scan.producers["p1"]
    assert [(item.counter, item.closed) for item in producer.files] == [(1, True), (2, True), (3, False)]
    assert producer.document["role"] == "backend" and abs(producer.heartbeat - (now - 30)) < 1
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


def test_a_newest_open_file_is_abandoned_only_once_its_producer_heartbeat_is_stale(tmp_path):
    root = tmp_path / "producers"
    now = time.time()
    for name, heartbeat_age in (("live", 5), ("stale", 90), ("own", 90), ("behind", 5), ("missing", None)):
        _write(root / name, 1, [_line(1)], closed=False, mtime=now - 120)
        if heartbeat_age is not None:
            document = root / name / spool.PRODUCER_FILE
            document.write_bytes(b"{}")
            os.utime(document, (now - heartbeat_age, now - heartbeat_age))
    # Behind a later file, an open file was left by a writer that went on: its mtime alone decides.
    _write(root / "behind", 2, [_line(2)], closed=False, mtime=now - 1)
    scan = scan_spool(tmp_path)
    assert scan.producers["missing"].heartbeat is None and abs(scan.producers["stale"].heartbeat - (now - 90)) < 1
    ready = select_ready(scan, set(), now=now, abandon_seconds=60, alive=lambda producer: producer.producer_id == "own")
    assert sorted(item.producer_id for item in ready) == ["behind", "missing", "stale"]
    # Once the live writer's heartbeat is stale too, its file is ready.
    assert "live" in {item.producer_id for item in select_ready(scan, set(), now=now + 60, abandon_seconds=60)}


def test_ready_cursor_follows_each_producer_and_examines_every_file_once(tmp_path, monkeypatch):
    """A pass over thousands of one-second files must not select afresh over every file after each finished one."""
    now = time.time()

    def producer(producer_id, files):
        return spool_reader.ProducerDir(producer_id, tmp_path / producer_id, None, heartbeat=now, files=[
            spool_reader.SpoolFile(producer_id, counter, tmp_path / producer_id / spool.file_name(counter, closed=closed),
                                   closed, 1, now - age) for counter, closed, age in files])

    scan = spool_reader.SpoolScan(producers={
        "a": producer("a", [(counter, True, 10_000 - counter) for counter in range(1, 3001)]),
        "b": producer("b", [(1, True, 9_997.5), (2, False, 1)]),
        "c": producer("c", [(1, True, 9_999.5), (2, True, 9_998.5)]),
    })
    computed = []
    original = spool_reader.SpoolFile.name
    monkeypatch.setattr(spool_reader.SpoolFile, "name",
                        property(lambda item: computed.append(item.counter) or original.fget(item)))
    done = {("c", spool.file_name(1))}  # consumed earlier, its deletion pending: skipped, never blocking
    cursor = spool_reader.ReadyCursor(scan, done, now=now, abandon_seconds=60)
    ready = cursor.ready()
    assert [(item.producer_id, item.counter) for item in ready] == [("a", 1), ("c", 2), ("b", 1)]
    assert ready == select_ready(scan, done, now=now, abandon_seconds=60)
    order, rounds = [], 0
    while ready:
        rounds += 1
        for item in ready:
            order.append((item.producer_id, item.counter))
            if item.producer_id == "b":
                cursor.block(item)  # b#1 stays unfinished: b#2 waits behind it (an open file anyway)
            else:
                cursor.advance(item)
        ready = cursor.ready()
    assert order == [("a", 1), ("c", 2), ("b", 1)] + [("a", counter) for counter in range(2, 3001)]
    assert (rounds, cursor.advanced) == (3000, 3001)
    # Every file was examined a bounded number of times, not once per round (4.5 million for 3,000 files).
    assert len(computed) < 2 * 3004


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


def _blob(directory, value: bytes) -> str:
    sha = hashlib.sha256(value).hexdigest()
    (directory / sha).write_bytes(value)
    return sha


def _blob_line(n: int, *shas: str) -> bytes:
    values = b",".join(b'"v%d":{"$blob":"%s"}' % (index, sha.encode()) for index, sha in enumerate(shas))
    return spool.encode_event_line(n, b"2026-09-14T08:00:00.000Z", b'{"type":"tool.finished","data":{%s}}' % values,
                                   version=spool.BLOB_VERSION)


def _reason(target):
    return spool_reader.read_producer_document(target.with_name(target.name + spool.REASON_SUFFIX))


def test_quarantine_keeps_the_blobs_its_file_references_beside_it(tmp_path, monkeypatch):
    blobs = spool.blobs_dir(tmp_path)
    blobs.mkdir(parents=True)
    first, second, missing = _blob(blobs, b'"first value"'), _blob(blobs, b'"second value"'), "f" * 64
    root = tmp_path / "producers" / "p1"
    _write(root, 1, [_blob_line(1, first, missing), _blob_line(2, second, first), b"not json\n"])
    target = quarantine_file(tmp_path, scan_spool(tmp_path, sweep=False).producers["p1"].files[0],
                             reason="unparsable_line", detail={})
    kept = [target.with_name(spool.quarantine_blob_name(target.name, sha)) for sha in (first, second)]
    # Hard links: no bytes are copied.
    assert [os.stat(path).st_ino for path in kept] == [os.stat(blobs / sha).st_ino for sha in (first, second)]
    assert not target.with_name(spool.quarantine_blob_name(target.name, missing)).exists()
    assert _reason(target)["blobs"] == 2

    def no_hard_links(source, destination):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_hard_links)
    _write(root, 2, [_blob_line(3, second), b"not json\n"])
    other = quarantine_file(tmp_path, scan_spool(tmp_path, sweep=False).producers["p1"].files[0],
                            reason="unparsable_line", detail={})
    copy = other.with_name(spool.quarantine_blob_name(other.name, second))
    assert copy.read_bytes() == b'"second value"' and os.stat(copy).st_ino != os.stat(blobs / second).st_ino
    assert stat.S_IMODE(os.stat(copy).st_mode) == spool.FILE_MODE and _reason(other)["blobs"] == 1
    # Once the blob sweep deleted them from blobs/, the quarantined lines can still be read in full.
    for sha in (first, second):
        (blobs / sha).unlink()
    assert [path.read_bytes() for path in kept] == [b'"first value"', b'"second value"']
    quarantine = tmp_path / spool.QUARANTINE_DIR
    paths = list(quarantine.iterdir())
    usage = spool.spool_usage(tmp_path)
    assert usage.quarantine_files == 2 and len(paths) == 7
    assert usage.quarantine_bytes == sum(max(os.stat(path).st_size, os.stat(path).st_blocks * 512) for path in paths)


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


def _quarantined(directory, name, size, age, now):
    """A quarantined data file with its sidecar, quarantined ``age`` seconds before ``now``."""
    directory.mkdir(parents=True, exist_ok=True)
    data, reason = directory / name, directory / (name + spool.REASON_SUFFIX)
    data.write_bytes(b"x" * size)
    reason.write_bytes(b"{}")
    os.utime(reason, (now - age, now - age))
    return data, reason


def test_quarantine_keeps_its_byte_limit_deleting_the_oldest_files_first(tmp_path):
    now = time.time()
    quarantine = tmp_path / "quarantine"
    oldest = _quarantined(quarantine, "p0__a.jsonl", 40, 300, now)
    older = _quarantined(quarantine, "p0__b.jsonl", 40, 200, now)
    _write(tmp_path / "producers" / "p1", 3, [_line(1)])
    item = scan_spool(tmp_path).producers["p1"].files[0]
    target = quarantine_file(tmp_path, item, reason="unparsable_line", detail={}, max_bytes=40 + item.size)
    assert target.exists() and target.with_name(target.name + spool.REASON_SUFFIX).exists()
    assert not any(path.exists() for path in oldest) and all(path.exists() for path in older)
    # A file larger than the limit on its own goes too, after every older one; its path is still returned.
    _write(tmp_path / "producers" / "p1", 4, [_line(2)])
    item = scan_spool(tmp_path).producers["p1"].files[0]
    second = quarantine_file(tmp_path, item, reason="unparsable_line", detail={}, max_bytes=10)
    assert second.parent == quarantine and not second.exists()
    assert os.listdir(quarantine) == []


def test_quarantine_sweep_deletes_old_files_and_orphans_then_the_oldest_beyond_the_byte_limit(tmp_path):
    now, week = time.time(), 7 * 86400
    quarantine = tmp_path / "quarantine"
    _quarantined(quarantine, "p1__a.jsonl", 100, week + 60, now)
    # Without a sidecar the data file's own mtime is its age.
    bare = quarantine / "p1__b.jsonl"
    bare.write_bytes(b"x" * 10)
    os.utime(bare, (now - week - 60, now - week - 60))
    orphan = quarantine / ("p1__c.jsonl" + spool.REASON_SUFFIX)
    orphan.write_bytes(b"{}")
    os.utime(orphan, (now - week - 60, now - week - 60))
    (quarantine / ("p1__d.jsonl" + spool.REASON_SUFFIX)).write_bytes(b"{}")
    _quarantined(quarantine, "p1__e.jsonl", 30, 3 * 86400, now)
    _quarantined(quarantine, "p1__f.jsonl", 30, 60, now)
    assert sweep_quarantine(tmp_path, max_bytes=40, max_age_seconds=week, now=now) == (3, 140)
    assert sorted(os.listdir(quarantine)) == ["p1__d.jsonl.reason", "p1__f.jsonl", "p1__f.jsonl.reason"]
    assert sweep_quarantine(tmp_path / "missing", max_bytes=0, max_age_seconds=0) == (0, 0)


def test_kept_blobs_go_with_their_quarantined_file_count_towards_its_bytes_and_age_out_as_orphans(tmp_path):
    now, week = time.time(), 7 * 86400
    quarantine = tmp_path / "quarantine"

    def keep(data, char, size):
        (quarantine / spool.quarantine_blob_name(data.name, char * 64)).write_bytes(b"x" * size)

    old, _ = _quarantined(quarantine, "p1__a.jsonl", 10, week + 60, now)
    keep(old, "a", 5)
    large, _ = _quarantined(quarantine, "p1__b.jsonl", 10, 120, now)
    keep(large, "b", 40)
    recent, _ = _quarantined(quarantine, "p1__c.jsonl", 10, 60, now)
    orphans = [quarantine / spool.quarantine_blob_name(f"p1__{char}.jsonl", char * 64) for char in "de"]
    for path in orphans:
        path.write_bytes(b"x")
    os.utime(orphans[0], (now - week - 60, now - week - 60))
    # The old file goes by age, the large one (10 bytes and a 40 byte blob) by size; each takes its blob along.
    assert sweep_quarantine(tmp_path, max_bytes=45, max_age_seconds=week, now=now) == (2, 65)
    assert sorted(os.listdir(quarantine)) == sorted([recent.name, recent.name + spool.REASON_SUFFIX, orphans[1].name])


def test_scan_applies_quarantine_limits_once_per_interval_without_a_blobs_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(spool_reader, "_quarantine_swept", {})
    now, week = time.time(), 7 * 86400
    limits = {"quarantine_max_bytes": 1024, "quarantine_max_age_seconds": week}
    first, _ = _quarantined(tmp_path / "quarantine", "p1__a.jsonl", 10, week + 60, now)
    scan_spool(tmp_path, **limits)
    assert not first.exists() and not spool.blobs_dir(tmp_path).exists()
    second, _ = _quarantined(tmp_path / "quarantine", "p1__b.jsonl", 10, week + 60, now)
    scan_spool(tmp_path, **limits)
    assert second.exists()  # swept less than BLOB_SWEEP_SECONDS ago
    monkeypatch.setattr(spool_reader, "BLOB_SWEEP_SECONDS", 0.0)
    scan_spool(tmp_path)
    assert second.exists()  # no limits given
    scan_spool(tmp_path, **limits)
    assert not second.exists()


def test_blob_sweep_reads_old_data_files_instead_of_keeping_every_later_blob(tmp_path, monkeypatch):
    now = time.time()
    blobs = spool.blobs_dir(tmp_path)
    blobs.mkdir(parents=True)

    def blob(char, age):
        path = blobs / (char * 64)
        path.write_bytes(b'"value"')
        os.utime(path, (now - age, now - age))
        return path

    # An hour-old data file references blob a; blob b is newer than that file but referenced by nothing.
    referenced, unreferenced, fresh = blob("a", 3900), blob("b", 1800), blob("c", 30)
    event = b'{"type":"tool.finished","data":{"output":{"$blob":"%s"}}}' % (b"a" * 64)
    _write(tmp_path / "producers" / "p1", 1, [spool.encode_event_line(1, b"2026-09-14T08:00:00.000Z", event,
                                                                      version=spool.BLOB_VERSION)], mtime=now - 3600)
    _write(tmp_path / "producers" / "p2", 1, [_line(1)], closed=False, mtime=now - 30)
    files = [item for producer in scan_spool(tmp_path, sweep=False).producers.values() for item in producer.files]
    # Over the read limit the oldest data file sets the cutoff, as if none were old.
    assert sweep_blobs(tmp_path, data_files=files, now=now, max_read_bytes=10) == (0, 0)
    # A reference split across read chunks is still found.
    monkeypatch.setattr(spool_reader, "READ_CHUNK_BYTES", 7)
    scan_spool(tmp_path)
    assert referenced.exists() and fresh.exists() and not unreferenced.exists()
    assert sorted(os.listdir(blobs)) == sorted([spool_reader.SWEEP_MARKER, "a" * 64, "c" * 64])


def test_remove_if_unchanged_puts_back_a_file_written_since_its_signature(tmp_path):
    path = _write(tmp_path, 1, [_line(1)], closed=False)
    signature = stat_signature(path)
    with open(path, "ab") as handle:
        handle.write(_line(2))
    assert remove_if_unchanged(path, signature) is False
    assert path.read_bytes() == _line(1) + _line(2)
    signature = stat_signature(path)
    os.utime(path, (signature[1] - 10, signature[1] - 10))  # same size, another mtime
    assert remove_if_unchanged(path, signature) is False and path.exists()
    assert remove_if_unchanged(path, stat_signature(path)) is True
    assert os.listdir(tmp_path) == []
    assert remove_if_unchanged(path, signature) is False
