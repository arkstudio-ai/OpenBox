"""Permission facts on the spool and the durable reply that no longer reads trace tables."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from permission import permission as permissions
from permission.permission import PendingPermission, PermissionRequest, Rule
from tests.unit.trajectory_producer_support import business_statements, recording_spool  # noqa: F401
from trajectory import TraceContext, bind


class FakePubSub:
    """A subscription whose wake-up messages are all lost."""

    async def subscribe(self, channel):
        return None

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        await asyncio.sleep(0.01)
        return None

    async def unsubscribe(self, channel):
        return None

    async def aclose(self):
        return None


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def setex(self, key, ttl, value):
        self.calls.append(("setex", key, ttl))
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.values.pop(key, None)

    async def publish(self, channel, payload):
        self.calls.append(("publish", channel))

    def pubsub(self):
        return FakePubSub()


@pytest.fixture
def isolated_permissions(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(permissions, "_pending", {})
    monkeypatch.setattr(permissions, "_approved", {})
    monkeypatch.setattr(permissions, "_get_redis_client", lambda: redis)
    monkeypatch.setattr(permissions, "_push_waiting", AsyncMock())
    monkeypatch.setattr(permissions, "_push_resolved", AsyncMock())
    return redis


async def test_a_reply_is_stored_before_publish_and_survives_a_lost_wakeup(recording_spool, isolated_permissions,
                                                                         monkeypatch):
    redis = isolated_permissions
    asked = asyncio.Event()
    monkeypatch.setattr("permission.permission.bus.publish",
                        lambda kind, _data: asked.set() if kind == permissions.PERMISSION_ASKED else None)
    original = TraceContext("a", "session_a", turn_id="approval_turn", run_id="approval_run",
                            call_id="approval_call")
    with bind(original):
        waiting = asyncio.create_task(permissions.ask("session_a", "bash", ["deploy"],
                                                      input_data={"command": "deploy --dry-run"}, user_id="a"))
    await asyncio.wait_for(asked.wait(), timeout=2)
    [request_id] = list(permissions._pending)
    assert json.loads(redis.values[f"perm_req:{request_id}"])["trace_context"]["call_id"] == "approval_call"
    with pytest.raises(PermissionError):
        await permissions.reply(request_id, "once", user_id="b")
    assert not waiting.done()

    # Another worker replies: it knows the request only from Redis, and the
    # wake-up message it publishes never arrives.
    local = permissions._pending.pop(request_id)
    await permissions.reply(request_id, "reject", "use a preview first", user_id="a")
    permissions._pending[request_id] = local
    reply_calls = [call for call in redis.calls if call[1] == f"perm_reply:{request_id}"]
    assert reply_calls == [("setex", f"perm_reply:{request_id}", 24 * 60 * 60),
                           ("publish", f"perm_reply:{request_id}")]
    with pytest.raises(permissions.PermissionCorrectedError):
        await asyncio.wait_for(waiting, timeout=5)

    events = recording_spool.events()
    assert [item["type"] for item in events] == ["permission.requested", "permission.resolved"]
    assert all((item["session_id"], item["call_id"], item["run_id"]) == ("session_a", "approval_call", "approval_run")
               for item in events)
    assert events[1]["data"]["decision"] == "reject" and events[1]["event_id"] == f"permission.resolved:{request_id}"


async def test_a_stored_reply_resolves_only_its_own_request_even_with_recording_off(isolated_permissions,
                                                                                  monkeypatch):
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    redis = isolated_permissions
    pending = PendingPermission(request=PermissionRequest(id="req-1", user_id="a", session_id="session_a",
                                                         tool="bash"))
    for foreign in ({"action": "once", "user_id": "b", "session_id": "session_a"},
                    {"action": "once", "user_id": "a", "session_id": "session_b"},
                    {"action": "maybe", "user_id": "a", "session_id": "session_a"}):
        redis.values["perm_reply:req-1"] = json.dumps(foreign)
        assert not await permissions._read_recorded_reply("req-1", pending)
    redis.values["perm_reply:req-1"] = "not json"
    assert not await permissions._read_recorded_reply("req-1", pending)
    redis.values["perm_reply:req-1"] = json.dumps({"action": "always", "message": None, "user_id": "a",
                                                   "session_id": "session_a"})
    assert await permissions._read_recorded_reply("req-1", pending)
    assert (pending.result, pending.error_message) == ("always", None)


async def test_policy_decisions_are_recorded_without_any_database_transaction(recording_spool, isolated_permissions,
                                                                             business_statements):
    call = TraceContext("u1", "s1", turn_id="turn", run_id="run", call_id="call-1")
    with bind(call):
        await permissions.ask("s1", "bash", ["ls"], input_data={"command": "ls"},
                              config_rules=[Rule(permission="bash", pattern="*", action="allow")], user_id="u1")
        with pytest.raises(permissions.PermissionDeniedError):
            await permissions.ask("s1", "bash", ["rm -rf /"],
                                  config_rules=[Rule(permission="bash", pattern="*", action="deny")], user_id="u1")
    assert business_statements == []
    events = recording_spool.events()
    assert [(item["type"], item["data"]["source_kind"]) for item in events] == [
        ("permission.requested", "policy"), ("permission.resolved", "policy")] * 2
    assert [item["data"]["status"] for item in events if item["type"] == "permission.resolved"] == [
        "completed", "denied"]
    assert {item["call_id"] for item in events} == {"call-1"}
