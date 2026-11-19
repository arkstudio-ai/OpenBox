"""``payload._gather_reads``: a failed read never cancels an object request already in flight.

A cancelled httpx request can wait forever inside httpcore's shielded close, and a ``gather`` over it then
never returns: on gw2 (2026-11-19) one missing blob stopped the projector for every session that way.
"""
import asyncio

import pytest

from trajectory import payload
from trajectory.types import CorruptContent


class Store:
    """``get`` of "missing" fails at once; other keys answer when released, and record a cancellation."""

    def __init__(self):
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.release = asyncio.Event()

    async def get(self, key: str) -> bytes:
        self.started.append(key)
        if key == "missing":
            raise FileNotFoundError(key)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(key)
            raise
        return b"{}"


@pytest.fixture(autouse=True)
def fresh_cache():
    payload.reset_blob_cache()
    yield
    payload.reset_blob_cache()


async def test_results_keep_their_order_and_an_empty_group_is_empty():
    async def value(n, delay):
        await asyncio.sleep(delay)
        return n

    assert await payload._gather_reads() == []
    assert await payload._gather_reads(value(1, 0.02), value(2, 0), value(3, 0.01)) == [1, 2, 3]


async def test_a_failed_read_lets_requests_in_flight_finish_and_starts_no_queued_one(monkeypatch):
    monkeypatch.setattr(payload, "FETCH_CONCURRENCY", 2)
    payload._fetch_limits.clear()
    store = Store()
    keys = ["slow-1", "missing", "queued-1", "queued-2"]
    group = asyncio.create_task(payload._gather_reads(
        *(payload.fetch_blob(store, key, "identity", None) for key in keys)))
    await asyncio.sleep(0.05)
    # The failure is known, yet the group still waits for the request in flight instead of cancelling it.
    assert not group.done()
    store.release.set()
    with pytest.raises(CorruptContent):
        await asyncio.wait_for(group, 2)
    assert store.cancelled == []
    assert "slow-1" in store.started and "missing" in store.started
    assert "queued-2" not in store.started  # it got its slot after the failure and never asked the store


async def test_cancelling_the_caller_does_not_cancel_requests_in_flight(monkeypatch):
    monkeypatch.setattr(payload, "FETCH_CONCURRENCY", 1)
    payload._fetch_limits.clear()
    store = Store()
    group = asyncio.create_task(payload._gather_reads(
        *(payload.fetch_blob(store, key, "identity", None) for key in ("slow-1", "queued-1"))))
    await asyncio.sleep(0.05)
    group.cancel()
    await asyncio.sleep(0.05)
    assert not group.done() and store.cancelled == []  # draining, not cancelling
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(group, 2)
    assert store.cancelled == [] and store.started == ["slow-1"]


async def test_a_request_that_never_returns_no_longer_holds_the_caller(monkeypatch):
    monkeypatch.setattr(payload, "FETCH_CONCURRENCY", 2)
    monkeypatch.setattr(payload, "DRAIN_SECONDS", 0.1)
    payload._fetch_limits.clear()
    store = Store()  # never released: "stuck" stands for a request lost inside the HTTP client
    with pytest.raises(CorruptContent):
        await asyncio.wait_for(payload._gather_reads(
            *(payload.fetch_blob(store, key, "identity", None) for key in ("stuck", "missing"))), 2)
    store.release.set()
    await asyncio.sleep(0)
