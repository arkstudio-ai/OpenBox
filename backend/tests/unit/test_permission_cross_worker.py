"""Distributed permission replies preserve durable Always semantics."""

import json

import pytest

import permission.permission as permission_mod
from permission.permission import PendingPermission, PermissionRequest, Rule


class FakePubSub:
    async def subscribe(self, _channel):
        return None

    async def get_message(self, **_kwargs):
        return None

    async def unsubscribe(self, _channel):
        return None

    async def aclose(self):
        return None


class FakeRedis:
    def __init__(self, request_data):
        self.values = {
            f"perm_req:{request_data['id']}": json.dumps(request_data)
        }
        self.published = []
        self.getdel_calls = []
        self.timeline = []

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        self.getdel_calls.append(key)
        return self.values.pop(key, None)

    async def setex(self, key, _ttl, value):
        self.timeline.append(("setex", key))
        self.values[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def publish(self, channel, payload):
        self.timeline.append(("publish", channel))
        self.published.append((channel, payload))

    def pubsub(self):
        return FakePubSub()


def request_data():
    return {
        "id": "permission-remote",
        "user_id": "user-a",
        "session_id": "session-a",
        "tool": "bash",
        "input": {"command": "echo ok"},
        "patterns": ["echo ok"],
        "always": ["*"],
        "metadata": {},
        "is_doom_loop": False,
        "created_at": "2026-08-31T00:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()
    monkeypatch.setattr(permission_mod.bus, "publish", lambda *_a, **_k: None)
    yield
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()


@pytest.mark.asyncio
async def test_remote_worker_always_is_persisted_before_reply_and_syncs_owner_cache(
    monkeypatch,
):
    data = request_data()
    redis = FakeRedis(data)
    timeline = redis.timeline

    async def loaded(user_id):
        permission_mod._loaded_users.add(user_id)

    async def persist(user_id, rule, *, raise_on_error=False):
        timeline.append(("persist", user_id, rule.pattern, raise_on_error))
        return True

    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)
    monkeypatch.setattr(permission_mod, "load_persisted_rules", loaded)
    monkeypatch.setattr(permission_mod, "_persist_rule", persist)

    # There is deliberately no local PendingPermission: the HTTP reply landed
    # on a different worker from the agent loop that is waiting.
    await permission_mod.reply(
        data["id"], "always", user_id=data["user_id"]
    )

    assert permission_mod._approved[data["user_id"]] == [
        Rule(permission="bash", pattern="*", action="allow")
    ]
    response = json.loads(redis.values[f"perm_resp:{data['id']}"])
    assert response["granted_rules"] == [
        {"permission": "bash", "pattern": "*", "action": "allow"}
    ]
    assert timeline.index(("persist", "user-a", "*", True)) < timeline.index(
        ("setex", f"perm_resp:{data['id']}")
    )

    # Simulate the waiting worker's independent memory and durable-response
    # consumption. It receives the same grant without writing a duplicate row.
    permission_mod._approved.clear()
    pending = PendingPermission(request=PermissionRequest.model_validate(data))
    permission_mod._apply_reply_data(pending, response)
    assert pending.result == "always"
    assert permission_mod._approved[data["user_id"]] == [
        Rule(permission="bash", pattern="*", action="allow")
    ]


@pytest.mark.asyncio
async def test_wrong_user_cannot_consume_remote_permission_request(monkeypatch):
    data = request_data()
    redis = FakeRedis(data)
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)

    with pytest.raises(PermissionError):
        await permission_mod.reply(data["id"], "reject", user_id="user-b")

    assert redis.getdel_calls == []
    assert f"perm_req:{data['id']}" in redis.values


@pytest.mark.asyncio
async def test_durable_response_closes_pubsub_subscribe_race(monkeypatch):
    data = request_data()
    redis = FakeRedis(data)
    response = {
        "action": "always",
        "message": None,
        "user_id": data["user_id"],
        "session_id": data["session_id"],
        "granted_rules": [
            {"permission": "bash", "pattern": "*", "action": "allow"}
        ],
    }
    redis.values[f"perm_resp:{data['id']}"] = json.dumps(response)
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: redis)
    pending = PendingPermission(request=PermissionRequest.model_validate(data))

    await permission_mod._wait_via_redis(data["id"], pending)

    assert pending.result == "always"
    assert permission_mod._approved[data["user_id"]] == [
        Rule(permission="bash", pattern="*", action="allow")
    ]


def test_global_reply_event_updates_an_already_warm_worker_cache():
    permission_mod._loaded_users.add("user-a")

    permission_mod._sync_grants_from_bus(
        {
            "data": {
                "userId": "user-a",
                "granted_rules": [
                    {"permission": "read", "pattern": "*", "action": "allow"}
                ],
            }
        }
    )

    assert permission_mod._approved["user-a"] == [
        Rule(permission="read", pattern="*", action="allow")
    ]
