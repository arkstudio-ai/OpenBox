"""Recovery guarantees for browser disconnects and reconnects."""

import asyncio

import pytest

from api import ws as ws_mod
from tests.unit.test_durable_questions import state  # noqa: F401


@pytest.mark.asyncio
async def test_reconnect_replays_durable_session_statuses(state) -> None:
    from agent.driver import reserve_run
    lease = await reserve_run("s1", "u1")
    queue: asyncio.Queue = asyncio.Queue()
    try:
        await ws_mod._enqueue_recovery_snapshot("u1", queue)
        assert queue.qsize() == 1  # Other owners are excluded.
        assert queue.get_nowait() == {
            "type": "session.status",
            "data": {"userId": "u1", "sessionId": "s1", "status": "busy", "generation": lease.generation, "recovery": True},
        }
        await lease.release(session_status="idle")
        await ws_mod._enqueue_recovery_snapshot("u1", queue)
        assert queue.get_nowait()["data"] == {
            "userId": "u1", "sessionId": "s1", "status": "idle", "generation": lease.generation, "recovery": True,
        }
    finally:
        await lease.release()


@pytest.mark.asyncio
async def test_container_cleanup_is_deferred_while_agent_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ws_mod.WSConnectionManager()

    async def active(_user_id: str) -> bool:
        return True

    monkeypatch.setattr(ws_mod, "_has_active_agent_sessions", active)

    assert await manager._cleanup_user_if_inactive("user-1") == "active"


@pytest.mark.asyncio
async def test_recovery_larger_than_queue_capacity_is_consumed_and_connection_stays_live(monkeypatch):
    """The real endpoint must start sending before a 1000-slot queue fills."""
    from fastapi import WebSocketDisconnect
    sent, recovered, disconnected = [], asyncio.Event(), asyncio.Event()

    class Socket:
        async def accept(self): pass
        async def send_json(self, value):
            sent.append(value)
            if len(sent) == 1102: recovered.set()
        async def receive_text(self):
            await disconnected.wait()
            raise WebSocketDisconnect()
        async def close(self, **kwargs): pass

    class Access:
        user_id, workspace_id = "owner", None
        async def check(self): return "user"
        async def watch(self): await asyncio.Future()

    async def enqueue(user_id, queue):
        for n in range(1101):
            # No session body: egress authority is exercised separately.
            await queue.put({"type": "server.heartbeat", "data": {"n": n}})

    manager = ws_mod.WSConnectionManager()
    monkeypatch.setattr(ws_mod, "ws_manager", manager)
    monkeypatch.setattr(manager, "schedule_cleanup", lambda _: None)
    monkeypatch.setattr(ws_mod, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(ws_mod.SocketAccess, "from_ticket", lambda *a, **k: Access())
    monkeypatch.setattr(ws_mod, "_enqueue_recovery_snapshot", enqueue)
    task = asyncio.create_task(ws_mod.agent_websocket(Socket(), ticket="", surface="assistant"))
    try:
        await asyncio.wait_for(recovered.wait(), 2)
        assert not task.done()  # Finishing hydration must not close the socket.
        assert [e["data"]["n"] for e in sent[1:]] == list(range(1101))
    finally:
        disconnected.set()
        await asyncio.wait_for(task, 2)
    assert not manager.has_connections("default")
