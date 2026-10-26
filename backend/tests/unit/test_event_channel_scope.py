"""Separate environment fan-out while retaining same-environment delivery."""
import asyncio
import json

import fakeredis
import pytest

from bus import bus, trajectory_hints
from bus.channels import event_channel


def test_namespace_is_explicit_and_legacy_compatible(monkeypatch):
    monkeypatch.delenv("EVENT_BUS_NAMESPACE", raising=False)
    assert event_channel("bus:events") == "bus:events"
    monkeypatch.setenv("EVENT_BUS_NAMESPACE", "memory-qa_1")
    assert event_channel("bus:events") == "memory-qa_1:bus:events"
    assert event_channel("trajectory:hints") == "memory-qa_1:trajectory:hints"


@pytest.mark.parametrize("namespace", [" ", "qa:*", "qa:other", "a" * 65, "qa\n"])
def test_invalid_scope_never_falls_back_to_shared_channels(monkeypatch, namespace):
    monkeypatch.setenv("EVENT_BUS_NAMESPACE", namespace)
    with pytest.raises(ValueError):
        event_channel("bus:events")


async def test_business_listener_and_publisher_use_the_same_frozen_scope(monkeypatch):
    import redis.asyncio
    server = fakeredis.FakeServer()
    client = fakeredis.aioredis.FakeRedis(server=server, db=6, decode_responses=True)
    peer = fakeredis.aioredis.FakeRedis(server=server, db=10, decode_responses=True)
    monkeypatch.setattr(redis.asyncio, "from_url", lambda *a, **k: client)
    monkeypatch.setenv("EVENT_BUS_NAMESPACE", "qa-main")
    received = []
    unsubscribe = bus.subscribe("qa.scope", lambda event: received.append(event["data"]))
    await bus.init_redis_bus("redis://unused/6")
    try:
        for _ in range(100):
            if dict(await peer.pubsub_numsub("qa-main:bus:events")).get("qa-main:bus:events"):
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("listener did not subscribe")
        envelope = lambda text: json.dumps({"worker_id": "another-worker", "event": {
            "type": "qa.scope", "data": {"value": text}}})
        assert await peer.publish("qa-benchmark:bus:events", envelope("foreign")) == 0
        assert await peer.publish("bus:events", envelope("legacy")) == 0
        await peer.publish("qa-main:bus:events", envelope("same-environment"))
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(.01)
        assert received == [{"value": "same-environment"}]
        # A later environment edit cannot split publish from listen.
        monkeypatch.setenv("EVENT_BUS_NAMESPACE", "changed")
        pubsub = peer.pubsub()
        await pubsub.subscribe("qa-main:bus:events")
        await pubsub.get_message(timeout=1)
        await bus.publish_confirmed("qa.scope", {"value": "outgoing"})
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        assert json.loads(message["data"])["event"]["data"] == {"value": "outgoing"}
        await pubsub.aclose()
    finally:
        unsubscribe()
        await bus.close_redis_bus()
        await peer.aclose()


async def test_trajectory_publisher_uses_the_same_environment_scope(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setenv("EVENT_BUS_NAMESPACE", "qa-main")
    client = AsyncMock()
    channel = trajectory_hints.HintChannel(client, sender_id="one")
    monkeypatch.setenv("EVENT_BUS_NAMESPACE", "changed")
    await channel.publish({"user_id": "test-user", "committed_seq": "1"})
    assert client.publish.call_args.args[0] == "qa-main:trajectory:hints"
