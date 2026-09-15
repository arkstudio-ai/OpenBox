"""Ingest crash recovery with real process kills (SPEC §4, §8.3): no duplicate and no lost event.

A child process ingests the spool and is stopped with SIGKILL at a commit boundary: after its blob uploads
and before the transaction, inside the transaction before COMMIT, after a commit and before the next batch,
and after a file's last commit and before the file is deleted. A fresh ingest service then resumes from
the committed offsets. No ``finally`` block or rollback handler of the killed process runs.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from trajectory import spool
from trajectory.storage import LocalBlobStore, decode_blob
from trajectory.store.models import TrajectoryEventKey, TrajectoryIngestFile, TrajectoryPayload
from trajectory.types import canonical
from trajectory.worker.ingest import INFLIGHT_FILE, IngestService
from tests.unit.test_worker_ingest import (FakeMetrics, FakeRetention, SpoolWriter, event, events_of, rows,  # noqa: F401
    settings, trace_db)

BACKEND = Path(__file__).resolve().parents[2]
SYSTEM = "system prompt " * 300

CHILD = r'''
import asyncio, os, sys, time
from dataclasses import replace
from pathlib import Path

boundary, url, spool_dir, blob_root, marker = sys.argv[1:6]
os.environ["TRAJECTORY_SPOOL_DIR"] = spool_dir

from trajectory.storage import LocalBlobStore
from trajectory.store.database import init_trace_engine
from trajectory.worker import ingest, spool_reader
from trajectory.worker.settings import WorkerSettings


class Metrics:
    def inc(self, *args, **kwargs):
        pass

    def set_gauge(self, *args, **kwargs):
        pass


def hang():
    Path(marker).write_text(boundary)
    while True:  # blocks the event loop and every pending commit: the parent kills the process here
        time.sleep(1)


calls = {"count": 0}


def nth(number):
    calls["count"] += 1
    return calls["count"] == number


if boundary == "uploaded":
    commit = ingest.IngestService._commit

    async def patched_commit(self, *args, **kwargs):
        if any(path.is_file() for path in Path(blob_root).rglob("*")):
            hang()
        return await commit(self, *args, **kwargs)

    ingest.IngestService._commit = patched_commit
elif boundary == "transaction":
    finish = ingest._Transaction.finish

    async def patched_finish(self, db, **kwargs):
        await finish(self, db, **kwargs)
        await db.flush()
        if nth(2):
            hang()

    ingest._Transaction.finish = patched_finish
elif boundary == "committed":
    after_commit = ingest._Transaction.after_commit

    def patched_after_commit(self, result):
        after_commit(self, result)
        if nth(1):
            hang()

    ingest._Transaction.after_commit = patched_after_commit
elif boundary == "consumed":
    def patched_remove(path):
        hang()

    spool_reader.remove_file = patched_remove


async def main():
    init_trace_engine(url)
    settings = replace(WorkerSettings.from_env(), ingest_batch_lines=2)
    service = ingest.IngestService(settings, blob_store=LocalBlobStore(blob_root), metrics=Metrics())
    await service.run_once()
    Path(marker).write_text("finished")


asyncio.run(main())
'''


def _write_spool(writer: SpoolWriter) -> list[str]:
    ids = [f"e{index}" for index in range(8)]
    writer.events(event("request.prepared", request_id="r0", event_id=ids[0],
                        data={"model": "m", "input": {"system": SYSTEM}}),
                  *[event(event_id=identifier, run_id="run_1") for identifier in ids[1:5]], age=20)
    writer.events(*[event(event_id=identifier, run_id="run_1") for identifier in ids[5:]], age=10)
    return ids


def _kill_at(boundary: str, trace_url: str, spool_dir: Path, blob_root: Path, marker: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", CHILD, boundary, trace_url, str(spool_dir), str(blob_root),
                              str(marker)], cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            if child.poll() is not None or time.monotonic() > deadline:
                output = child.stdout.read().decode(errors="replace") if child.stdout else ""
                raise AssertionError(f"the child never reached {boundary}: {output[-2000:]}")
            time.sleep(0.02)
        assert marker.read_text() == boundary
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=30) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if child.stdout:
            child.stdout.close()


@pytest.mark.parametrize("boundary, committed_lines", [
    ("uploaded", None), ("transaction", 2), ("committed", 2), ("consumed", 5)])
async def test_a_killed_ingest_resumes_without_duplicates_or_losses(trace_db, settings, tmp_path, boundary,
                                                                    committed_lines):
    writer = SpoolWriter(settings.spool_dir)
    ids = _write_spool(writer)
    blob_root, marker = tmp_path / "blobs", tmp_path / "marker"
    await asyncio.to_thread(_kill_at, boundary, str(trace_db.url.render_as_string(hide_password=False)),
                            settings.spool_dir, blob_root, marker)
    # Killed inside a batch of the first file: no finally block removed the in-flight marker that names it.
    inflight = settings.spool_dir / spool.CONTROL_DIR / INFLIGHT_FILE
    assert json.loads(inflight.read_bytes())["file"] == spool.file_name(1)

    # What the killed process left behind: committed offsets only.
    files = await rows(TrajectoryIngestFile)
    if committed_lines is None:
        assert files == [] and await rows(TrajectoryEventKey) == []
        assert any(path.is_file() for path in blob_root.rglob("*"))
    else:
        [file_row] = [row for row in files if row.file_name.endswith("1.jsonl")]
        assert file_row.lines_consumed == committed_lines
        assert file_row.done == (boundary == "consumed")

    store = LocalBlobStore(blob_root)
    service = IngestService(replace(settings, ingest_batch_lines=2), blob_store=store, metrics=FakeMetrics(),
                            retention=FakeRetention())
    for _ in range(5):
        if not (await service.run_once())["lines"]:
            break
    assert not inflight.exists()

    trajectory, stored = await events_of("ses_1")
    assert [row.event_id for row in stored] == [f"evt_start_{trajectory.id}", *ids]
    assert [row.seq for row in stored] == list(range(1, len(ids) + 2))
    assert (trajectory.committed_seq, trajectory.next_seq, trajectory.event_count) == (9, 10, 9)
    keys = await rows(TrajectoryEventKey)
    assert sorted(key.event_id for key in keys) == sorted(row.event_id for row in stored)
    assert await rows(TrajectoryIngestFile) == [] and not list(writer.directory.glob("*.jsonl"))
    [payload] = await rows(TrajectoryPayload)
    assert payload.availability == "available" and payload.first_seq == 2
    assert json.loads(decode_blob(await store.get(payload.storage_key), payload.encoding)) == SYSTEM
    assert decode_blob(await store.get(payload.storage_key), payload.encoding) == canonical(SYSTEM)
