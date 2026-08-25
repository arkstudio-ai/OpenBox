"""Recovery guarantees for browser disconnects and reconnects."""

import asyncio
from types import SimpleNamespace

import pytest

from api import ws as ws_mod


@pytest.mark.asyncio
async def test_reconnect_replays_durable_session_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sessions(*, user_id: str):
        assert user_id == "user-1"
        return [
            SimpleNamespace(id="session-busy", status=SimpleNamespace(value="busy")),
            SimpleNamespace(id="session-idle", status=SimpleNamespace(value="idle")),
        ]

    import session.session

    monkeypatch.setattr(session.session, "list_sessions", sessions)
    queue: asyncio.Queue = asyncio.Queue()

    await ws_mod._enqueue_recovery_snapshot("user-1", queue)

    assert await queue.get() == {
        "type": "session.status",
        "data": {"userId": "user-1", "sessionId": "session-busy", "status": "busy"},
    }
    assert await queue.get() == {
        "type": "session.status",
        "data": {"userId": "user-1", "sessionId": "session-idle", "status": "idle"},
    }


@pytest.mark.asyncio
async def test_container_cleanup_is_deferred_while_agent_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ws_mod.WSConnectionManager()

    async def active(_user_id: str) -> bool:
        return True

    monkeypatch.setattr(ws_mod, "_has_active_agent_sessions", active)

    assert await manager._cleanup_user_if_inactive("user-1") == "active"
