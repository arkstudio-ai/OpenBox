"""Overload, cancellation and decoded-size limits protect trace writers and normal routes."""
import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest
import zstandard
from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import DBAPIError

from tests.unit.test_worker_projection_support import (JSON, add_payload, add_trajectory, blobs, trace_db)  # noqa: F401
from trajectory.payload import Resolver, blob_cache, fetch_blob, read_payload, reset_blob_cache
from trajectory.read_budget import ReadTooLarge, current_read_budget, read_budget
from trajectory.store.database import trace_read_session, trace_session
from trajectory.store.models import SessionTrajectory
from trajectory.worker.metrics import get_metrics
from trajectory.worker.read_limits import AdmittedResponse, BoundedReadRoute, ReadAdmission


async def test_object_io_holds_no_connection_and_a_writer_can_commit(trace_db, blobs):
    await add_trajectory("trj_read", "session_read")
    row = await add_payload(blobs, "trj_read", b"x" * 100_000, first_seq=1, media_type=JSON)
    observed = []

    async def slow_object(_key):
        observed.append(trace_db.pool.checkedout())
        # A separate transaction must be able to commit during the object read.
        async with trace_session() as writer:
            await writer.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_read").values(
                recording_status="degraded"))

    blobs.get_hook = slow_object
    with read_budget(200_000):
        async with trace_read_session() as reader:
            trajectory = await reader.get(SessionTrajectory, "trj_read")
            assert inspect(trajectory).detached and trace_db.pool.checkedout() == 0
            _, content = await read_payload(reader, trajectory.id, row.payload_id, through_seq=1)
            assert content == b"x" * 100_000
            assert (await reader.get(SessionTrajectory, trajectory.id)).recording_status == "degraded"
            assert trace_db.pool.checkedout() == 0
            assert (await reader.scalars(select(SessionTrajectory))).one().id == trajectory.id
            for statement in (update(SessionTrajectory).values(recording_status="blocked"),
                              select(SessionTrajectory).with_for_update()):
                with pytest.raises(ValueError):
                    await reader.execute(statement)
            with pytest.raises(ValueError):
                await reader.get(SessionTrajectory, trajectory.id, with_for_update={})
    assert observed == [0]


@pytest.mark.parametrize("encoding", ["identity", "zstd"])
async def test_decoded_budget_stops_cold_and_cached_blobs_and_does_not_affect_recording(blobs, encoding):
    raw = b"large repetitive content" * 100_000
    key, sha = "trajectories/trj_test/blobs/large", hashlib.sha256(raw).hexdigest()
    stored = zstandard.ZstdCompressor().compress(raw) if encoding == "zstd" else raw
    await blobs.put(key, stored, content_type="application/octet-stream")
    with read_budget(32_768) as budget:
        with pytest.raises(ReadTooLarge):
            await fetch_blob(blobs, key, encoding, sha)
        assert budget.used <= 32_768
    # Background work has no request budget, even after a rejected download.
    assert current_read_budget() is None
    assert await fetch_blob(blobs, key, encoding, sha) == raw
    reads = blobs.gets
    with read_budget(len(raw) - 1):
        with pytest.raises(ReadTooLarge):
            await fetch_blob(blobs, key, encoding, sha)
    assert blobs.gets == reads  # Cached content still consumes the request budget.
    with read_budget(len(raw)):
        assert await fetch_blob(blobs, key, encoding, sha) == raw


async def test_concurrent_blob_fetches_share_one_budget(blobs):
    raw = b"x" * 4096
    sha = hashlib.sha256(raw).hexdigest()
    keys = [f"trajectories/trj_test/blobs/{index}" for index in range(3)]
    for key in keys:
        await blobs.put(key, raw, content_type=JSON)
    with read_budget(8192) as budget:
        results = await asyncio.gather(*(fetch_blob(blobs, key, "identity", sha) for key in keys),
                                       return_exceptions=True)
        assert sum(isinstance(result, ReadTooLarge) for result in results) == 1
        assert budget.used == 8192
    reset_blob_cache()


async def test_oversized_read_drains_sibling_object_requests_before_returning(monkeypatch):
    import trajectory.payload as payload
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def fetch(_store, key, *_args):
        if key == "too_large":
            await entered.wait()
            raise ReadTooLarge("too large")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(payload, "fetch_blob", fetch)
    rows = [SimpleNamespace(storage_key=key, encoding="identity", sha256=None) for key in ("too_large", "slow")]
    with pytest.raises(ReadTooLarge):
        await Resolver(None, "trj_test", through_seq=1)._json(rows, "invalid JSON")
    assert cancelled.is_set()


async def test_archived_segments_consume_budget_on_cache_hits_without_duplicate_blob_cache(blobs):
    from tests.unit.test_trajectory_segments import KEY, _manifest, _row
    from trajectory.repository import _segment_lines
    from trajectory.segments import encode_segment

    stored, meta = encode_segment([_row(index) for index in range(5, 9)])
    await blobs.put(KEY, stored, content_type="application/octet-stream")
    segment = SimpleNamespace(**_manifest(meta))
    with read_budget(meta["raw_bytes"] - 1):
        with pytest.raises(ReadTooLarge):
            await _segment_lines(segment, blobs)
    with read_budget(meta["raw_bytes"]):
        assert len((await _segment_lines(segment, blobs)).lines) == 4
    assert blob_cache().size == 0
    reads = blobs.gets
    with read_budget(meta["raw_bytes"] - 1):
        with pytest.raises(ReadTooLarge):
            await _segment_lines(segment, blobs)
    assert blobs.gets == reads


async def test_admission_rejects_overflow_but_normal_routes_and_writers_continue(monkeypatch, trace_db):
    monkeypatch.setenv("TRAJECTORY_READ_CONCURRENCY", "1")
    monkeypatch.setenv("TRAJECTORY_READ_MAX_WAITING", "1")
    monkeypatch.setenv("TRAJECTORY_READ_WAIT_MS", "1000")
    app, router = FastAPI(), APIRouter(route_class=BoundedReadRoute)
    entered, release = asyncio.Event(), asyncio.Event()

    @router.get("/trace")
    async def slow():
        entered.set()
        await release.wait()
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.get("/trace"))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(client.get("/trace"))
        admission = app.state.trajectory_read_admission
        try:
            async with asyncio.timeout(1):
                while admission.waiting != 1:
                    await asyncio.sleep(0)
            refused = await client.get("/trace")
            assert refused.status_code == 429 and refused.headers["retry-after"] == "1"
            assert (await client.get("/health")).status_code == 200
            await asyncio.wait_for(add_trajectory("trj_while_busy", "session_while_busy"), 1)
            assert (admission.active, admission.waiting) == (1, 1)
        finally:
            release.set()
            responses = await asyncio.gather(first, second)
        assert all(response.status_code == 200 for response in responses)
        assert (admission.active, admission.waiting) == (0, 0)


async def test_cancelled_and_timed_out_waiters_leave_no_slots_or_queue_entries(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_READ_CONCURRENCY", "1")
    monkeypatch.setenv("TRAJECTORY_READ_WAIT_MS", "10")
    admission = ReadAdmission()
    assert await admission.acquire()
    assert not await admission.acquire()
    assert admission.waiting == 0
    pending = asyncio.create_task(admission.acquire())
    async with asyncio.timeout(1):
        while not admission.waiting:
            await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert admission.waiting == 0 and admission.active == 1
    admission.release()
    assert await admission.acquire()
    admission.release()


async def test_response_errors_release_admission_and_return_bounded_failures(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_READ_CONCURRENCY", "1")
    monkeypatch.setenv("TRAJECTORY_READ_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("TRAJECTORY_READ_MAX_BYTES", "32")
    monkeypatch.setenv("TRAJECTORY_READ_RESPONSE_BYTES", "64")
    app, router = FastAPI(), APIRouter(route_class=BoundedReadRoute)

    @router.get("/trace/{kind}")
    async def read(kind: str):
        if kind == "slow":
            await asyncio.Event().wait()
        if kind == "decoded":
            current_read_budget().consume(33)
        if kind == "statement":
            original = Exception("cancelled by statement_timeout")
            original.sqlstate = "57014"
            raise DBAPIError("SELECT", {}, original)
        return {"data": "x" * (100 if kind == "body" else 1)}

    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for kind, status in (("slow", 503), ("decoded", 413), ("body", 413), ("statement", 503)):
            response = await client.get(f"/trace/{kind}")
            assert response.status_code == status and response.headers["cache-control"] == "no-store"
            assert app.state.trajectory_read_admission.active == 0
            assert (await client.get("/trace/small")).status_code == 200


async def test_stream_keeps_admission_until_send_or_cancellation(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_READ_CONCURRENCY", "1")
    admission = ReadAdmission()
    assert await admission.acquire()
    sent = asyncio.Event()

    async def body():
        yield b"content"
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body":
            sent.set()

    response = AdmittedResponse(StreamingResponse(body()), admission)
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    task = asyncio.create_task(response(scope, None, send))
    await asyncio.wait_for(sent.wait(), 1)
    assert admission.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.active == 0


async def test_a_json_response_releases_its_slot_before_a_slow_client_receives_it(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_READ_CONCURRENCY", "1")
    app, router = FastAPI(), APIRouter(route_class=BoundedReadRoute)

    @router.get("/trace")
    async def read():
        return {"ok": True}

    app.include_router(router)
    sending, release = asyncio.Event(), asyncio.Event()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            sending.set()
            await release.wait()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/trace", "raw_path": b"/trace", "root_path": "", "query_string": b"",
             "headers": [], "client": ("test", 1), "server": ("test", 80)}
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(sending.wait(), 1)
        assert app.state.trajectory_read_admission.active == 0
    finally:
        release.set()
        await task


async def test_downloads_stream_under_transfer_slots_and_give_their_read_slot_back(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_READ_CONCURRENCY", "1")
    monkeypatch.setenv("TRAJECTORY_READ_TRANSFERS", "4")
    monkeypatch.setenv("TRAJECTORY_READ_WAIT_MS", "10")
    app, router = FastAPI(), APIRouter(route_class=BoundedReadRoute)
    release, closed = asyncio.Event(), []

    class Download(StreamingResponse):
        """A spooled download: streams once the client is ready, closes its content when it never runs."""

        def __init__(self):
            super().__init__(self.content(), media_type="application/octet-stream")

        async def content(self):
            yield b"content"
            await release.wait()

        def close(self):
            closed.append(True)

    @router.get("/trace/payloads/{name}")
    async def download(name: str):
        return Download()

    @router.get("/trace")
    async def read():
        return {"ok": True}

    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        streams = [asyncio.create_task(client.get(f"/trace/payloads/{index}")) for index in range(4)]
        async with asyncio.timeout(1):
            while getattr(app.state, "trajectory_read_transfers", None) is None or app.state.trajectory_read_transfers.active < 4:
                await asyncio.sleep(0)
        admission, transfers = app.state.trajectory_read_admission, app.state.trajectory_read_transfers
        # Four streams to slow clients hold no read slot: the one configured answers a JSON read at once.
        assert (admission.active, transfers.active) == (0, 4)
        assert get_metrics().snapshot()["gauges"]["read_transfers"] == 4
        assert (await client.get("/trace")).status_code == 200
        refused = await client.get("/trace/payloads/5")
        assert refused.status_code == 429 and refused.headers["retry-after"] == "1"
        assert refused.json()["detail"]["code"] == "trajectory_read_busy" and closed == [True]
        assert (admission.active, transfers.active) == (0, 4)
        release.set()
        responses = await asyncio.gather(*streams)
        assert [(response.status_code, response.content) for response in responses] == [(200, b"content")] * 4
        assert (admission.active, transfers.active) == (0, 0) and closed == [True]
        assert get_metrics().snapshot()["gauges"]["read_transfers"] == 0


async def test_subscription_watermarks_read_within_the_pool_reserve(trace_db, monkeypatch):
    from trajectory.store.database import READ_POOL_RESERVE
    from trajectory.worker import ws
    running, peak, release = 0, 0, asyncio.Event()

    async def get_trajectory(_db, session_id, *, optional=False):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        recorded = session_id != "s0"
        return (SimpleNamespace(id=session_id, user_id="owner"),
                SimpleNamespace(id=f"trj_{session_id}", committed_seq=7) if recorded else None)

    monkeypatch.setattr(ws.repository, "get_trajectory", get_trajectory)
    app, count = FastAPI(), READ_POOL_RESERVE + 3
    reads = [asyncio.create_task(ws._header(f"s{index}", app)) for index in range(count)]
    async with asyncio.timeout(1):
        while running < READ_POOL_RESERVE:
            await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    assert peak == READ_POOL_RESERVE
    release.set()
    headers = await asyncio.gather(*reads)
    assert [item["session_id"] for item in headers] == [f"s{index}" for index in range(count)]
    # Just the watermark, never the session header; an unrecorded session subscribes at "0".
    assert headers[1] == {"user_id": "owner", "session_id": "s1", "trajectory_id": "trj_s1", "committed_seq": "7"}
    assert headers[0] == {"user_id": "owner", "session_id": "s0", "trajectory_id": None, "committed_seq": "0"}


async def test_the_read_pool_reserves_connections_for_subscriptions_and_follows_engine_replacement(monkeypatch):
    from trajectory.store import database
    await database.close_trace_engine()
    monkeypatch.delenv("TRAJECTORY_READ_DB_POOL_SIZE", raising=False)
    monkeypatch.setenv("TRAJECTORY_READ_CONCURRENCY", "3")
    database.init_trace_engine("postgresql+asyncpg://reader:secret@127.0.0.1:1/openbox_trace_test_pool")
    try:
        database.TraceReader()._factory()
        assert database._read_engine.pool.size() == 3 + database.READ_POOL_RESERVE
        # A replaced engine does not keep the read pool of the old one.
        database.init_trace_engine("postgresql+asyncpg://reader:secret@127.0.0.1:1/openbox_trace_test_other")
        assert database._read_engine is None and database._read_factory is None
    finally:
        await database.close_trace_engine()
