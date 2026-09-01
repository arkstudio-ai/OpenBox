"""Public prompt routes use the durable Inbox without preempting a live turn."""
import asyncio
from datetime import datetime, timezone
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text

from agent import inbox
from agent.driver import get_driver_state, reserve_run
from api import sessions as sessions_api
from api.sessions import PromptBody
from auth import middleware
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from models.message import MessageInfo, MessageRole
from session.session import create_assistant_message, update_message_info


async def _seed() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"flight-user-{suffix}"
    project_id = f"flight-project-{suffix}"
    session_id = f"flight-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await db.execute(text(
            "CREATE TABLE IF NOT EXISTS kv_store ("
            " key VARCHAR(255) PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " updated_at TIMESTAMP)"
        ))
        db.add(User(
            id=user_id,
            username=f"flight-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Single flight",
            slug=f"flight-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            agent="build",
            model="test/model",
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
    return user_id, session_id


@pytest.mark.asyncio
async def test_busy_followup_is_accepted_without_preempt_then_runs_fifo(monkeypatch):
    user_id, session_id = await _seed()
    starts = [asyncio.Event(), asyncio.Event()]
    finishes = [asyncio.Event(), asyncio.Event()]
    driven = []

    async def no_quota(*_args, **_kwargs):
        return None

    async def fake_drive(lease, batch):
        index = len(driven)
        driven.append((lease, batch))
        starts[index].set()
        await finishes[index].wait()
        await inbox.settle_claimed_inbox_items(
            lease, result_message_id=None, outcome="succeeded",
        )
        await lease.release(session_status="idle")

    monkeypatch.setattr(sessions_api, "check_concurrent_agents", no_quota)
    monkeypatch.setattr(inbox, "_drive_claimed", fake_drive)

    first = await sessions_api.send_message_async(
        session_id,
        PromptBody(text="first", client_message_id=f"first-{uuid.uuid4().hex}"),
        current_user={"user_id": user_id},
    )
    await asyncio.wait_for(starts[0].wait(), timeout=1)
    first_state = await get_driver_state(session_id)
    assert first_state is not None

    second = await sessions_api.send_message_async(
        session_id,
        PromptBody(text="second", client_message_id=f"second-{uuid.uuid4().hex}"),
        current_user={"user_id": user_id},
    )
    still_first = await get_driver_state(session_id)
    assert still_first is not None
    assert still_first.run_id == first_state.run_id == first["runId"]
    assert still_first.generation == first_state.generation
    assert still_first.abort_requested_at is None
    assert not driven[0][0].abort.is_set()
    assert second["state"] == "accepted"
    assert second["runId"] is None

    async with get_db_session() as db:
        rows = list((await db.execute(select(AgentInboxItem).where(
            AgentInboxItem.session_id == session_id,
        ).order_by(AgentInboxItem.created_at, AgentInboxItem.id))).scalars().all())
    assert [row.state for row in rows] == ["claimed", "accepted"]

    finishes[0].set()
    await inbox.quiesce_inbox_tasks(timeout=1)
    await inbox.wake_inbox_session(session_id, user_id)
    await asyncio.wait_for(starts[1].wait(), timeout=1)
    assert driven[1][1].receipts[0].id == second["inboxId"]
    finishes[1].set()
    await inbox.quiesce_inbox_tasks(timeout=1)


@pytest.mark.asyncio
async def test_async_client_id_retry_returns_same_inbox_and_message(monkeypatch):
    user_id, session_id = await _seed()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def no_quota(*_args, **_kwargs):
        return None

    async def fake_drive(lease, batch):
        started.set()
        await finish.wait()
        await inbox.settle_claimed_inbox_items(
            lease, result_message_id=None, outcome="succeeded",
        )
        await lease.release(session_status="idle")

    monkeypatch.setattr(sessions_api, "check_concurrent_agents", no_quota)
    monkeypatch.setattr(inbox, "_drive_claimed", fake_drive)
    client_id = f"stable-{uuid.uuid4().hex}"
    first = await sessions_api.send_message_async(
        session_id,
        PromptBody(text="once", client_message_id=client_id),
        current_user={"user_id": user_id},
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    retry = await sessions_api.send_message_async(
        session_id,
        PromptBody(text="once", client_message_id=client_id),
        current_user={"user_id": user_id},
    )
    assert retry["inboxId"] == first["inboxId"]
    async with get_db_session() as db:
        assert (await db.execute(select(func.count(AgentInboxItem.id)).where(
            AgentInboxItem.session_id == session_id,
        ))).scalar_one() == 1
        assert (await db.execute(select(func.count(Message.id)).where(
            Message.session_id == session_id,
            Message.role == "user",
        ))).scalar_one() == 1
    finish.set()
    await inbox.quiesce_inbox_tasks(timeout=1)


@pytest.mark.asyncio
async def test_sync_prompt_waits_for_its_exact_terminal_item(monkeypatch):
    user_id, session_id = await _seed()

    async def fake_drive(lease, batch):
        trigger_id = batch.receipts[-1].message_id
        assistant = await create_assistant_message(
            session_id,
            trigger_id,
            model_id="test/model",
            agent="build",
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
        )
        await update_message_info(
            MessageInfo(
                id=assistant.id,
                sessionID=session_id,
                role=MessageRole.ASSISTANT,
                parent_id=trigger_id,
                model_id="test/model",
                agent="build",
                finish="stop",
            ),
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
        )
        await inbox.settle_claimed_inbox_items(
            lease,
            result_message_id=assistant.id,
            outcome="succeeded",
        )
        await lease.release(session_status="idle")

    monkeypatch.setattr(inbox, "_drive_claimed", fake_drive)
    result = await asyncio.wait_for(sessions_api.send_message(
        session_id,
        PromptBody(text="answer me", client_message_id=f"sync-{uuid.uuid4().hex}"),
        current_user={"user_id": user_id},
    ), timeout=2)
    assert result["role"] == "assistant"
    assert result["finish"] == "stop"
    async with get_db_session() as db:
        item = (await db.execute(select(AgentInboxItem).where(
            AgentInboxItem.session_id == session_id,
        ))).scalar_one()
    assert item.state == "settled"
    assert item.result_message_id == result["id"]


@pytest.mark.asyncio
async def test_sync_idle_inject_returns_202_receipt_without_waiting_or_waking():
    user_id, session_id = await _seed()
    app = FastAPI()
    app.include_router(sessions_api.router)
    app.dependency_overrides[middleware.get_current_user] = lambda: {
        "user_id": user_id,
        "role": "admin",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await asyncio.wait_for(client.post(
            f"/session/{session_id}/message",
            json={
                "text": "context for a later turn",
                "delivery": "inject",
                "client_message_id": f"inject-{uuid.uuid4().hex}",
            },
        ), timeout=1)

    assert response.status_code == 202
    payload = response.json()
    assert payload["ok"] is True
    assert payload["state"] == "accepted"
    assert payload["runId"] is None
    assert await get_driver_state(session_id) is None
    assert payload["inboxId"] not in inbox._item_events
    current = await inbox.get_inbox_item(payload["inboxId"], user_id=user_id)
    assert current is not None and current.state == "accepted"
    await inbox.cancel_inbox_items(
        session_id=session_id,
        user_id=user_id,
        item_ids=(payload["inboxId"],),
        reason="test cleanup",
    )


@pytest.mark.asyncio
async def test_stop_race_aborts_driver_claimed_during_inbox_cancellation(monkeypatch):
    user_id, session_id = await _seed()
    leases = []
    abort_calls: list[dict] = []

    async def racing_cancel(**_kwargs):
        lease = await reserve_run(session_id, user_id)
        leases.append(lease)
        return ()

    async def exact_abort(
        called_session_id,
        called_user_id,
        **kwargs,
    ):
        assert called_session_id == session_id
        assert called_user_id == user_id
        abort_calls.append(kwargs)
        return True

    import session.abort as abort_module

    monkeypatch.setattr(inbox, "cancel_inbox_items", racing_cancel)
    monkeypatch.setattr(abort_module, "abort_session_turn", exact_abort)
    result = await sessions_api.abort_session(
        session_id,
        current_user={"user_id": user_id},
    )
    try:
        assert result == {"ok": True, "marked": True, "canceledInbox": 0}
        assert len(leases) == 1
        assert abort_calls == [{
            "reason": "user_stop",
            "was_active": True,
            "expected_run_id": leases[0].run_id,
            "expected_generation": leases[0].generation,
        }]
    finally:
        for lease in leases:
            await lease.release(session_status="idle")
