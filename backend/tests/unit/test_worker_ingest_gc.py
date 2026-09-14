"""Blob GC against ingest (SPEC §8.3, §8.11): a GC delete never removes an object that ingest references.

``StubGc`` reproduces the order of operations of ``RetentionService.process_gc_queue`` (archive package):
read the due entries, check that no available payload row references the key, delete the object outside
any transaction, and remove the completed entries at the end of the pass. It can stop between its reference
check and the delete, the window in which an ingest batch may store or reuse the same content-addressed key
and commit a reference. The worker wires it like the real service (``WorkerServices``).
"""
import asyncio
import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, select

from trajectory.storage import MemoryBlobStore, blob_key, decode_blob, encode_blob, key_prefix
from trajectory.store.database import trace_session
from trajectory.store.models import TrajectoryGcQueue, TrajectoryPayload
from trajectory.types import canonical
from tests.unit.test_worker_ingest import SpoolWriter, event, events_of, rows, settings, trace_db  # noqa: F401
from tests.unit.test_worker_services import _services

SYSTEM = "system prompt " * 200


class StubGc:
    """The GC step of RetentionService, with a pause between the reference check and the delete."""

    def __init__(self, store):
        self.blob_store = store
        self.checked = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()

    async def process_gc_queue(self, limit: int = 100) -> int:
        async with trace_session() as db:
            entries = (await db.execute(select(TrajectoryGcQueue.id, TrajectoryGcQueue.storage_key)
                                        .where(TrajectoryGcQueue.kind == "key",
                                               TrajectoryGcQueue.next_attempt_at <= datetime.now(timezone.utc))
                                        .order_by(TrajectoryGcQueue.id).limit(limit))).all()
        completed = []
        for entry_id, key in entries:
            if not await self._still_used(key):
                self.checked.set()
                await self.proceed.wait()
                await self.blob_store.delete(key)
            completed.append(entry_id)
        async with trace_session() as db:
            if completed:
                await db.execute(delete(TrajectoryGcQueue).where(TrajectoryGcQueue.id.in_(completed)))
        return len(completed)

    async def run_once(self) -> dict:
        return {"gc_processed": await self.process_gc_queue()}

    async def tombstone(self, db, trajectory, *, reason):  # pragma: no cover - no deletions in these tests
        raise AssertionError("unexpected tombstone")

    @staticmethod
    async def _still_used(key: str) -> bool:
        owner, _, rest = key[len(key_prefix()):].partition("/")
        async with trace_session() as db:
            return await db.scalar(select(TrajectoryPayload.payload_id).where(
                TrajectoryPayload.trajectory_id == owner, TrajectoryPayload.sha256 == rest.partition("/")[2],
                TrajectoryPayload.storage_key == key, TrajectoryPayload.availability == "available").limit(1)) is not None


def _system_object(trajectory_id: str) -> tuple[str, bytes]:
    body = canonical(SYSTEM)
    stored, _ = encode_blob(body, "application/json")
    return blob_key(trajectory_id, hashlib.sha256(body).hexdigest()), stored


async def _queue(key: str) -> None:
    now = datetime.now(timezone.utc)
    async with trace_session() as db:
        db.add(TrajectoryGcQueue(kind="key", storage_key=key, reason="content_not_referenced", attempts=0,
                                 next_attempt_at=now, created_at=now))


def _prepared(request_id: str) -> dict:
    return event("request.prepared", request_id=request_id, event_id=request_id,
                 data={"model": "m", "input": {"system": SYSTEM}})


async def _start(settings):
    store = MemoryBlobStore()
    gc = StubGc(store)
    services = _services(settings, blob_store=store, retention=gc)
    writer = SpoolWriter(settings.spool_dir)
    writer.events(event(event_id="first"))
    await services.ingest.run_once()
    trajectory, _ = await events_of("ses_1")
    return store, gc, services, writer, trajectory


async def _readable(store, key: str) -> bool:
    [row] = await rows(TrajectoryPayload, TrajectoryPayload.storage_key == key)
    return row.availability == "available" and decode_blob(store.objects[key], row.encoding) == canonical(SYSTEM)


async def test_a_gc_delete_after_its_check_keeps_an_object_that_ingest_references_meanwhile(trace_db, settings):
    store, gc, services, writer, trajectory = await _start(settings)
    key, stored = _system_object(trajectory.id)
    # An object nothing references (an upload whose batch never committed) with a queued key entry.
    await store.put(key, stored, content_type="application/json")
    await _queue(key)
    gc.proceed.clear()
    sweep = asyncio.create_task(services.retention.process_gc_queue())
    await asyncio.wait_for(gc.checked.wait(), 5)
    # The pass found no reference and is about to delete; a batch now uses the same content.
    writer.events(_prepared("r1"))
    ingest = asyncio.create_task(services.ingest.run_once())
    await asyncio.wait({ingest}, timeout=1)
    gc.proceed.set()
    await asyncio.wait_for(asyncio.gather(sweep, ingest), 5)
    _, stored_events = await events_of("ses_1")
    assert stored_events[-1].data["input"]["system"]["$ref"]["sha256"] == key.rsplit("/", 1)[1]
    assert key in store.objects and await _readable(store, key)
    assert await rows(TrajectoryGcQueue) == []


async def test_ingest_waits_for_a_gc_delete_in_progress(trace_db, settings):
    store, gc, services, writer, trajectory = await _start(settings)
    key, stored = _system_object(trajectory.id)
    await store.put(key, stored, content_type="application/json")
    await _queue(key)
    deleting, finish_delete = asyncio.Event(), asyncio.Event()

    async def slow_delete(_key):
        deleting.set()
        await finish_delete.wait()

    store.faults["delete"] = slow_delete
    sweep = asyncio.create_task(services.retention.process_gc_queue())
    await asyncio.wait_for(deleting.wait(), 5)
    writer.events(_prepared("r1"))
    ingest = asyncio.create_task(services.ingest.run_once())
    done, _ = await asyncio.wait({ingest}, timeout=0.5)
    # The batch stores the object again only once the delete has finished.
    blocked = not done
    finish_delete.set()
    await asyncio.wait_for(asyncio.gather(sweep, ingest), 5)
    assert blocked
    assert key in store.objects and await _readable(store, key)


async def test_a_queued_entry_makes_ingest_store_a_reused_object_again(trace_db, settings):
    store, gc, services, writer, trajectory = await _start(settings)
    writer.events(_prepared("r1"))
    await services.ingest.run_once()
    key, _ = _system_object(trajectory.id)
    assert await _readable(store, key)
    # A GC delete that succeeded while its entry stayed queued (its bookkeeping transaction failed), after a
    # writer referenced the key again: the row says the object exists, the entry says it may not.
    del store.objects[key]
    await _queue(key)
    puts = store.puts
    writer.events(_prepared("r2"))
    await services.ingest.run_once()
    assert store.puts == puts + 1 and await _readable(store, key)
    assert await rows(TrajectoryGcQueue) == []
    # Nothing queued: a reused object is not uploaded again.
    writer.events(_prepared("r3"))
    await services.ingest.run_once()
    assert store.puts == puts + 1
    assert await services.retention.process_gc_queue() == 0 and key in store.objects
