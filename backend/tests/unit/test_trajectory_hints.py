"""Trajectory hints on their own Redis channel (WAVE3 shared contract 3).

Fan-out between processes over ``trajectory:hints`` only, strict decoding,
reconnects, and a worker app that opens the hint channel instead of the
business bus.
"""
import asyncio
import json

import fakeredis
import httpx
import pytest

from bus import bus, trajectory_hints
from bus.trajectory_hints import CHANNEL, HintChannel
from tests.unit.test_worker_app_harness import FakeServices, trace_url  # noqa: F401

HINT = {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1", "trajectory_id": "trj_a1",
        "committed_seq": "4"}


@pytest.fixture
def received():
    events = []
    unsubscribe = bus.subscribe("trajectory.available", events.append)
    yield events
    unsubscribe()


@pytest.fixture
async def server(monkeypatch):
    monkeypatch.setattr(trajectory_hints, "_channel", None)
    monkeypatch.setattr(trajectory_hints, "RECONNECT_MIN_SECONDS", 0.01)
    yield fakeredis.FakeServer()
    await trajectory_hints.close_trajectory_hints()


def redis(server):
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


async def eventually(condition, timeout: float = 3.0) -> bool:
    for _ in range(int(timeout / 0.01)):
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.01)
    return False


def subscribers(probe, count: int):
    async def check():
        return dict(await probe.pubsub_numsub(CHANNEL)).get(CHANNEL) == count
    return check


async def test_hints_reach_other_processes_on_the_trajectory_channel_only(server, received):
    here = await trajectory_hints.init_trajectory_hints("redis://unused", client=redis(server))
    assert await trajectory_hints.init_trajectory_hints("redis://unused", client=redis(server)) is here
    elsewhere = HintChannel(redis(server))
    elsewhere.start()
    probe = redis(server)
    try:
        assert await eventually(subscribers(probe, 2))
        trajectory_hints.publish_trajectory_hint(HINT)
        assert received == [{"type": "trajectory.available", "data": HINT}]  # this process, at once
        assert await eventually(lambda: len(received) == 2)  # the other process, over Redis
        assert received[1] == {"type": "trajectory.available", "data": HINT}
        # A trajectory event on the business channel reaches no hint listener.
        await probe.publish("bus:events", json.dumps({"worker_id": "other",
                                                      "event": {"type": "trajectory.available", "data": HINT}}))
        await asyncio.sleep(0.1)
        assert len(received) == 2  # the publisher also ignored its own message
    finally:
        await elsewhere.close()
        await probe.aclose()


def test_only_well_formed_hints_of_other_processes_are_dispatched_with_contract_keys(received):
    channel = HintChannel(client=None, sender_id="here")
    for raw in ("not json", None, json.dumps(["list"]),
                json.dumps({"sender": "x", "type": "session.status", "data": HINT}),
                json.dumps({"sender": "x", "type": "trajectory.available", "data": "text"}),
                json.dumps({"sender": "here", "type": "trajectory.available", "data": HINT})):
        assert channel.receive(raw) is None
    assert received == []
    data = channel.receive(json.dumps({"sender": "there", "type": "trajectory.available",
                                       "data": {**HINT, "deleted": True, "content": "never forwarded"}}))
    assert data == {**HINT, "deleted": True}
    assert received == [{"type": "trajectory.available", "data": {**HINT, "deleted": True}}]


async def test_the_listener_subscribes_again_after_redis_fails(server, received):
    real = redis(server)
    failures = [ConnectionError("redis restarting")]

    class Flaky:
        def pubsub(self):
            if failures:
                raise failures.pop()
            return real.pubsub()

        async def aclose(self):
            await real.aclose()

    listener = HintChannel(Flaky(), sender_id="listener")
    listener.start()
    probe = redis(server)
    try:
        assert await eventually(subscribers(probe, 1)) and failures == []
        await HintChannel(probe, sender_id="publisher").publish(HINT)
        assert await eventually(lambda: received == [{"type": "trajectory.available", "data": HINT}])
    finally:
        await listener.close()
        await probe.aclose()
    assert listener._listener is None and await eventually(subscribers(redis(server), 0))


async def test_unreachable_redis_is_not_fatal_and_a_closed_channel_stays_local(server, received):
    class Down:
        def __init__(self):
            self.closed = False

        async def ping(self):
            raise ConnectionError("down")

        def pubsub(self):
            raise ConnectionError("down")

        async def publish(self, channel, message):
            raise ConnectionError("down")

        async def aclose(self):
            self.closed = True

    client = Down()
    channel = await trajectory_hints.init_trajectory_hints("redis://unused", client=client)
    assert channel is not None and not channel._listener.done()
    trajectory_hints.publish_trajectory_hint(HINT)  # the failed Redis publish is logged, never raised
    await asyncio.sleep(0.05)
    assert received == [{"type": "trajectory.available", "data": HINT}]
    await trajectory_hints.close_trajectory_hints()
    assert client.closed and channel._listener is None

    probe = redis(server)
    pubsub = probe.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        trajectory_hints.publish_trajectory_hint({**HINT, "committed_seq": "5"})
        assert received[-1]["data"]["committed_seq"] == "5"
        await asyncio.sleep(0.05)
        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1) is None
    finally:
        await pubsub.aclose()
        await probe.aclose()


async def test_the_worker_app_opens_the_hint_channel_and_never_the_business_bus(trace_url, tmp_path, monkeypatch):
    from cache.memory_cache import MemoryCache
    from core.config import OpenBoxConfig
    from trajectory.auth import HttpBackend
    from trajectory.storage import MemoryBlobStore
    from trajectory.worker import app as worker_app

    calls = []

    async def init(url, **kwargs):
        calls.append(("init", url))

    async def close():
        calls.append(("close",))

    async def business_bus(*args, **kwargs):
        raise AssertionError("the trajectory worker must not open the business bus")

    monkeypatch.setattr(trajectory_hints, "init_trajectory_hints", init)
    monkeypatch.setattr(trajectory_hints, "close_trajectory_hints", close)
    monkeypatch.setattr(bus, "init_redis_bus", business_bus)
    monkeypatch.setattr(worker_app, "_auth_stores", lambda: (MemoryCache(), True))
    monkeypatch.setattr("core.config.get_config",
                        lambda: OpenBoxConfig(jwt_secret="server-secret", redis_url="redis://hints.invalid:6379/3"))
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path))
    backend = HttpBackend("http://backend", "token", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    app = worker_app.create_app(database_url=trace_url, blob_store=MemoryBlobStore(),
                                services_factory=lambda store: FakeServices(store), backend=backend)
    try:
        async with app.router.lifespan_context(app):
            assert calls == [("init", "redis://hints.invalid:6379/3")]
    finally:
        await backend.close()
    assert calls == [("init", "redis://hints.invalid:6379/3"), ("close",)]
