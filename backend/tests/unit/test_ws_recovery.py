"""Recovery guarantees for browser disconnects and reconnects."""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from api import ws as ws_mod
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.project import Project
from db.models.session import Session
from db.models.user import User


@pytest.mark.asyncio
async def test_reconnect_replays_durable_session_statuses() -> None:
    suffix = uuid.uuid4().hex[:10]
    user_id = f"ws-user-{suffix}"
    project_id = f"ws-project-{suffix}"
    busy_id = f"ws-busy-{suffix}"
    idle_id = f"ws-idle-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="WS recovery",
            slug=project_id,
            created_at=now,
            updated_at=now,
        ))
        db.add_all([
            Session(
                id=busy_id,
                user_id=user_id,
                project_id=project_id,
                status="busy",
                created_at=now,
                updated_at=now,
            ),
            Session(
                id=idle_id,
                user_id=user_id,
                project_id=project_id,
                status="idle",
                created_at=now,
                updated_at=now,
            ),
        ])
        await db.flush()
        db.add_all([
            AgentDriverState(
                session_id=busy_id,
                user_id=user_id,
                generation=2,
                phase="running",
                updated_at=now,
            ),
            AgentDriverState(
                session_id=idle_id,
                user_id=user_id,
                generation=1,
                phase="idle",
                updated_at=now,
            ),
        ])
    queue: asyncio.Queue = asyncio.Queue()

    await ws_mod._enqueue_recovery_snapshot(user_id, queue)

    frames = [
        await asyncio.wait_for(queue.get(), timeout=1),
        await asyncio.wait_for(queue.get(), timeout=1),
    ]
    by_session = {frame["data"]["sessionId"]: frame for frame in frames}
    assert by_session[busy_id] == {
        "type": "session.status",
        "data": {
            "userId": user_id,
            "sessionId": busy_id,
            "status": "busy",
            "generation": 2,
        },
    }
    assert by_session[idle_id] == {
        "type": "session.status",
        "data": {
            "userId": user_id,
            "sessionId": idle_id,
            "status": "idle",
            "generation": 1,
        },
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
