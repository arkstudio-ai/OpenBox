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
            "data": {"userId": "u1", "sessionId": "s1", "status": "busy", "generation": lease.generation},
        }
        await lease.release(session_status="idle")
        await ws_mod._enqueue_recovery_snapshot("u1", queue)
        assert queue.get_nowait()["data"] == {
            "userId": "u1", "sessionId": "s1", "status": "idle", "generation": lease.generation,
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
