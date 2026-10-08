"""A voice ticket opens only the voice socket; a plain ticket never opens it."""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import auth.ticket as tickets
from cache.memory_cache import MemoryCache


@pytest.fixture
def store(monkeypatch):
    cache = MemoryCache()
    monkeypatch.setattr(tickets, "_cache", cache)
    return cache


async def test_audiences_never_cross_and_a_refusal_does_not_burn_the_ticket(store):
    voice = await tickets.create_ticket("u1", workspace_id="w1", audience="voice")
    plain = await tickets.create_ticket("u1", workspace_id="w1")
    assert await tickets.consume_ticket(voice) is None  # what /ws/agent asks for
    assert await tickets.consume_ticket(plain, audience="voice") is None
    identity = await tickets.consume_ticket(voice, audience="voice")
    assert identity["user_id"] == "u1" and identity["workspace_id"] == "w1" and identity["audience"] == "voice"
    assert await tickets.consume_ticket(voice, audience="voice") is None  # still single use
    assert (await tickets.consume_ticket(plain))["user_id"] == "u1"


async def test_each_socket_refuses_the_other_audience_with_4001(store, monkeypatch):
    from api import voice as voice_socket, ws as agent_socket
    monkeypatch.setattr(agent_socket, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(voice_socket, "is_auth_enabled", lambda: True)
    app = FastAPI()
    app.include_router(agent_socket.router)
    app.include_router(voice_socket.router)
    voice = await tickets.create_ticket("u1", workspace_id="w1", audience="voice")
    plain = await tickets.create_ticket("u1", workspace_id="w1")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/ws/agent?ticket={voice}") as socket:
            socket.receive_json()
    assert refused.value.code == 4001
    with client.websocket_connect(f"/ws/assistant/voice?ticket={plain}") as socket:
        assert socket.receive() == {"type": "websocket.close", "code": 4001, "reason": ""}
