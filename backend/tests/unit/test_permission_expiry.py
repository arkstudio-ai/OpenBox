"""Unlinked permission requests expire; assistant-linked ones keep their durable expiry."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import permission.permission as permission_mod
from permission.permission import PendingPermission, PermissionRequest, PermissionUnavailableError


class FakePubSub:
    async def subscribe(self, _channel):
        return None

    async def get_message(self, **_kwargs):
        await asyncio.sleep(0.01)
        return None

    async def unsubscribe(self, _channel):
        return None

    async def aclose(self):
        return None


class FakeRedis:
    def __init__(self, request_data=None, *, deliverable=True):
        self.values = {}
        self.ttls = {}
        self.deliverable = deliverable
        if request_data is not None:
            self.values[f"perm_req:{request_data['id']}"] = json.dumps(request_data)

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        return self.values.pop(key, None)

    async def setex(self, key, ttl, value):
        if not self.deliverable and not key.startswith("perm_req:"):
            raise ConnectionError("fixture redis refuses replies")
        self.ttls[key] = ttl
        self.values[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def publish(self, _channel, _payload):
        if not self.deliverable:
            raise ConnectionError("fixture redis refuses replies")

    def pubsub(self):
        return FakePubSub()


def stamp(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def request_data(expires_in):
    return {"id": "permission-expiring", "user_id": "user-a", "session_id": "session-a", "tool": "bash",
            "input": {"command": "echo ok"}, "patterns": ["echo ok"], "always": ["*"], "metadata": {},
            "is_doom_loop": False, "created_at": stamp(-1), "expires_at": stamp(expires_in)}


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()
    monkeypatch.setattr(permission_mod.bus, "publish", lambda *_a, **_k: None)
    monkeypatch.setattr(permission_mod, "_use_db", lambda: False)

    async def quiet(*_args):
        return None

    monkeypatch.setattr(permission_mod, "_push_waiting", quiet)
    monkeypatch.setattr(permission_mod, "_push_resolved", quiet)
    yield
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()


async def ask():
    await permission_mod.ask("session-a", "bash", ["echo ok"], input_data={"command": "echo ok"}, user_id="user-a")


@pytest.mark.parametrize("with_redis", [False, True])
async def test_unanswered_request_times_out_at_its_expiry(monkeypatch, with_redis):
    redis = FakeRedis() if with_redis else None
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)
    monkeypatch.setattr(permission_mod, "PERMISSION_REQUEST_TTL_SECONDS", 0.2)
    with pytest.raises(PermissionUnavailableError):
        await asyncio.wait_for(ask(), timeout=5)
    assert permission_mod._pending == {}
    if with_redis:
        assert list(redis.ttls.values()) == [0.2] and redis.values == {}


async def test_new_request_carries_its_expiry(monkeypatch):
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: None)
    waiter = asyncio.create_task(ask())
    while not permission_mod._pending:
        await asyncio.sleep(0.01)
    pending, = permission_mod._pending.values()
    created = datetime.fromisoformat(pending.request.created_at)
    assert datetime.fromisoformat(pending.request.expires_at) - created == timedelta(seconds=300)
    await permission_mod.reply(pending.request.id, "once", user_id="user-a")
    await asyncio.wait_for(waiter, timeout=5)


async def test_reply_after_expiry_is_refused_without_consuming(monkeypatch):
    data = request_data(-1)
    redis = FakeRedis(data)
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)
    with pytest.raises(KeyError, match="expired"):
        await permission_mod.reply(data["id"], "once", user_id="user-a")
    assert f"perm_req:{data['id']}" in redis.values and f"perm_resp:{data['id']}" not in redis.values

    local = PendingPermission(request=PermissionRequest(**{**data, "id": "permission-local"}))
    permission_mod._pending[local.request.id] = local
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: None)
    with pytest.raises(KeyError, match="expired"):
        await permission_mod.reply(local.request.id, "always", user_id="user-a")
    assert local.result is None and not local.event.is_set()


async def test_restored_request_keeps_only_its_remaining_time(monkeypatch):
    data = request_data(100)
    redis = FakeRedis(data)
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)

    async def loaded(user_id):
        permission_mod._loaded_users.add(user_id)

    async def broken(*_args, **_kwargs):
        raise OSError("fixture persistence failure")

    monkeypatch.setattr(permission_mod, "load_persisted_rules", loaded)
    monkeypatch.setattr(permission_mod, "_persist_rule", broken)
    with pytest.raises(OSError):
        await permission_mod.reply(data["id"], "always", user_id="user-a")
    key = f"perm_req:{data['id']}"
    assert key in redis.values and 0 < redis.ttls[key] <= 100


async def test_undeliverable_reply_restores_with_the_remaining_time(monkeypatch):
    data = request_data(100)
    redis = FakeRedis(data, deliverable=False)
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)
    with pytest.raises(RuntimeError, match="could not reach"):
        await permission_mod.reply(data["id"], "once", user_id="user-a")
    key = f"perm_req:{data['id']}"
    assert key in redis.values and 0 < redis.ttls[key] <= 100


async def test_request_that_expires_during_persistence_is_not_restored(monkeypatch):
    data = request_data(0.2)
    redis = FakeRedis(data)
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)
    pending = PendingPermission(request=PermissionRequest(**data))
    permission_mod._pending[data["id"]] = pending

    async def loaded(user_id):
        permission_mod._loaded_users.add(user_id)

    async def slow_failure(*_args, **_kwargs):
        await asyncio.sleep(0.3)
        raise OSError("fixture persistence failure")

    monkeypatch.setattr(permission_mod, "load_persisted_rules", loaded)
    monkeypatch.setattr(permission_mod, "_persist_rule", slow_failure)
    with pytest.raises(OSError):
        await permission_mod.reply(data["id"], "always", user_id="user-a")
    assert f"perm_req:{data['id']}" not in redis.values and data["id"] not in permission_mod._pending


async def test_linked_request_waits_on_its_durable_decision_not_a_local_clock(monkeypatch):
    """The assistant's versioned replies decide a linked request's expiry (``fresh``)."""
    import assistant.permission_requests as durable

    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: None)
    monkeypatch.setattr(permission_mod, "PERMISSION_REQUEST_TTL_SECONDS", 0.05)

    async def register(request):
        # Mirrors durable.register: the event freezes its own expiry.
        return request.model_copy(update={"expires_at": stamp(-1), "assistant": {"task_id": "task-a"}})

    async def decided(pending):
        await asyncio.sleep(0.2)
        pending.result = "once"

    monkeypatch.setattr(durable, "register", register)
    monkeypatch.setattr(permission_mod, "_wait_linked", decided)
    await asyncio.wait_for(ask(), timeout=5)

    receipt = {"ok": True, "request_kind": "permission", "state": "accepted"}

    async def maybe_reply(*_args, **_kwargs):
        return receipt

    monkeypatch.setattr(durable, "maybe_reply", maybe_reply)
    assert await permission_mod.reply("linked-request", "once", user_id="user-a",
                                      reply_id="r", expected_request_revision="v", options_hash="h") == receipt
