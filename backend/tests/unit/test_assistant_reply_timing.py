"""Reply timing is a source-checked read of one exact settled main input."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.part import Part
from tests.unit.test_assistant_api import client_for, complete_answer
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_public_history import client_for as history_client


@pytest.fixture
async def reply():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    receipt, message, _ = await complete_answer(owner, workspace, main, "reply-timing")
    accepted = datetime.now(timezone.utc) - timedelta(minutes=2)
    settled = accepted + timedelta(seconds=66.474375)
    async with get_db_session() as db:
        row = await db.get(AgentInboxItem, receipt["inbox_id"])
        assert row.state == "settled" and row.result_message_id == message.id
        row.accepted_at, row.settled_at = accepted, settled
    return owner, other, workspace, main, receipt, message, accepted, settled


async def read_reply(reply, monkeypatch):
    owner, _, workspace, main, _, message, *_ = reply
    async with client_for(owner, workspace, monkeypatch) as client:
        response = await client.get("/api/assistant/messages", params={
            "session_id": main.id, "message_ids": message.id,
        })
        assert response.status_code == 200, response.text
        return response.json()["messages"][0]


async def test_exact_reply_timing_survives_real_http_history_and_source_projection(reply, monkeypatch):
    owner, _, workspace, main, receipt, message, accepted, settled = reply
    expected = {"accepted_at": accepted.isoformat(), "settled_at": settled.isoformat()}
    async with get_db_session() as db:
        events_before = await db.scalar(select(func.count()).select_from(AgentEvent))
    checked = await read_reply(reply, monkeypatch)
    assert checked["source_status"] == "available"
    assert checked["assistant_timing"] == expected
    async with history_client(owner, workspace) as client:
        for endpoint in ("history", "message?offset=0&limit=200"):
            response = await client.get(f"/api/agent/session/{main.id}/{endpoint}")
            assert response.status_code == 200, response.text
            rows = response.json()["messages"] if endpoint == "history" else response.json()
            assert next(row for row in rows if row["id"] == message.id)["assistant_timing"] == expected
            assert all("assistant_timing" not in row for row in rows if row["role"] == "user")
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AgentEvent)) == events_before
        item = await db.get(AgentInboxItem, receipt["inbox_id"])
        assert item.state == "settled" and item.result_message_id == message.id


@pytest.mark.parametrize("change", ["unsettled", "run_id", "generation", "turn_id", "result_message_id", "reversed_time"])
async def test_missing_or_mismatched_settlement_never_borrows_reply_timing(reply, monkeypatch, change):
    *_, receipt, message, accepted, settled = reply[0:]
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, receipt["inbox_id"])
        if change == "unsettled":
            item.state, item.settled_at = "claimed", None
            item.claim_expires_at = settled + timedelta(minutes=10)
        elif change == "reversed_time":
            item.settled_at = accepted - timedelta(seconds=1)
        else:
            setattr(item, change, {"run_id": "unrelated-run", "generation": item.generation + 1,
                "turn_id": "unrelated-turn", "result_message_id": item.message_id}[change])
    checked = await read_reply(reply, monkeypatch)
    assert checked["id"] == message.id and checked["source_status"] == "available"
    assert "assistant_timing" not in checked


async def test_revoked_source_and_other_actor_cannot_obtain_saved_reply_timing(reply, monkeypatch):
    owner, other, workspace, main, receipt, _, *_ = reply
    assert "assistant_timing" in await read_reply(reply, monkeypatch)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, receipt["inbox_id"])
        original = await db.scalar(select(Part).where(Part.message_id == item.message_id, Part.type == "text"))
        original.data = {**original.data, "text": "The original source changed."}
    checked = await read_reply(reply, monkeypatch)
    assert checked["source_status"] == "unavailable" and checked["parts"] == []
    assert "assistant_timing" not in checked
    async with history_client(other, workspace) as client:
        assert (await client.get(f"/api/agent/session/{main.id}/history")).status_code == 404

