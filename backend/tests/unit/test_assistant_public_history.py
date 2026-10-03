"""Generic transcript routes cannot bypass current assistant source validity."""
import json

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from api import sessions as routes
from assistant.public_history import public_messages
from db.base import get_db_session
from db.models.part import Part
from db.models.workspace import WorkspaceMember
from models.message import TextPart
from session.session import get_messages, get_session, save_part, update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn
from tests.unit.test_assistant_api import client_for as assistant_client
from tests.unit.test_assistant_foundation import accounts
from assistant.service import ensure_main_session


def client_for(owner, workspace):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/agent")
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": owner, "workspace_id": workspace}
    app.dependency_overrides[routes.get_workspace] = lambda: {"id": workspace}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://assistant.test")


async def test_generic_history_and_legacy_message_pages_revalidate_derived_answers(monkeypatch):
    ctx, lease, message, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        await save_part(TextPart(session_id=ctx.session_id, message_id=message.id,
            text="PRIVATE_DERIVED_ANSWER"), is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
        message.finish = "stop"
        await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
        session = await get_session(ctx.session_id, user_id=ctx.user_id)
        # Keep an earlier in-memory page to model a response racing revocation.
        cached = await get_messages(ctx.session_id, user_id=ctx.user_id)
        async with client_for(ctx.user_id, ctx.workspace_id) as client:
            for suffix in ("history", "message?offset=0&limit=200", f"history?after={message.id}"):
                response = await client.get(f"/api/agent/session/{ctx.session_id}/{suffix}")
                assert response.status_code == 200, response.text
                assert "PRIVATE_DERIVED_ANSWER" in response.text
            async with get_db_session() as db:
                source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
                source.data = {**source.data, "text": "Original evidence changed"}
            for suffix in ("history", "message?offset=0&limit=200", f"history?after={message.id}"):
                response = await client.get(f"/api/agent/session/{ctx.session_id}/{suffix}")
                assert response.status_code == 200, response.text
                assert "PRIVATE_DERIVED_ANSWER" not in response.text
                items = response.json()["messages"] if suffix.startswith("history") else response.json()
                answer = next(row for row in items if row["id"] == message.id)
                assert answer["source_status"] == "unavailable" and answer["parts"] == []
            fresh = await public_messages(session, cached, actor_user_id=ctx.user_id)
            assert "PRIVATE_DERIVED_ANSWER" not in json.dumps(fresh)
            async with assistant_client(ctx.user_id, ctx.workspace_id, monkeypatch) as check:
                response = await check.get("/api/assistant/messages", params={"session_id": ctx.session_id, "message_ids": message.id})
                assert response.status_code == 200
                assert response.json()["messages"][0]["source_status"] == "unavailable"
                assert "PRIVATE_DERIVED_ANSWER" not in response.text
            # Redaction is a read projection, never loss of the original evidence.
            assert "PRIVATE_DERIVED_ANSWER" in json.dumps([row.model_dump() for row in await get_messages(ctx.session_id)])
            async with get_db_session() as db:
                (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
            assert (await client.get(f"/api/agent/session/{ctx.session_id}/history")).status_code == 404
    finally:
        await lease.release(session_status="idle")


async def test_message_revalidation_is_bounded_private_and_does_not_create_an_assistant(monkeypatch):
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    params = {"session_id": main.id, "message_ids": "unknown-message"}
    async with assistant_client(other, workspace, monkeypatch) as client:
        assert (await client.get("/api/assistant/messages", params=params)).status_code == 404
        assert (await client.get("/api/assistant")).json()["state"] == "not_created"
    async with assistant_client(owner, workspace, monkeypatch) as client:
        missing = await client.get("/api/assistant/messages", params=params)
        assert missing.status_code == 200
        assert missing.json()["messages"][0]["parts"] == []
        assert missing.json()["messages"][0]["source_status"] == "unavailable"
        assert (await client.get("/api/assistant/messages", params={"session_id": main.id})).status_code == 422
        oversized = [("session_id", main.id)] + [("message_ids", f"message-{n}") for n in range(101)]
        assert (await client.get("/api/assistant/messages", params=oversized)).status_code == 422
        assert (await client.get("/api/assistant/messages", params={**params, "message_ids": "x" * 65})).status_code == 422
        assert (await client.get("/api/assistant/messages", params={**params, "session_id": "another-main"})).status_code == 404


async def test_unfinished_assistant_history_has_no_unverified_copy_or_structured_body():
    ctx, lease, message, _, _ = await read_turn()
    try:
        await save_part(TextPart(session_id=ctx.session_id, message_id=message.id,
            text="UNVERIFIED_STREAM_TEXT"), is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
        async with client_for(ctx.user_id, ctx.workspace_id) as client:
            response = await client.get(f"/api/agent/session/{ctx.session_id}/history")
            assert response.status_code == 200
            assert "UNVERIFIED_STREAM_TEXT" not in response.text
            row = next(row for row in response.json()["messages"] if row["id"] == message.id)
            assert row["source_status"] == "pending"
    finally:
        await lease.release(session_status="idle")
