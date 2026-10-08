"""Generic transcript routes page assistant sessions like ordinary chats (V2).

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 4.2, D1): saved messages are returned
as stored. A later edit of an original source neither hides nor rewrites an
answer; only the reader's current access to the session is checked.
"""
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


def client_for(owner, workspace):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/agent")
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": owner, "workspace_id": workspace}
    app.dependency_overrides[routes.get_workspace] = lambda: {"id": workspace}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://assistant.test")


def page_rows(suffix, response):
    return response.json()["messages"] if suffix.startswith("history") else response.json()


async def test_generic_history_pages_keep_saved_answers_and_recheck_membership():
    ctx, lease, message, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        await save_part(TextPart(session_id=ctx.session_id, message_id=message.id,
            text="PRIVATE_DERIVED_ANSWER"), is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
        message.finish = "stop"
        await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
        session = await get_session(ctx.session_id, user_id=ctx.user_id)
        cached = await get_messages(ctx.session_id, user_id=ctx.user_id)
        suffixes = ("history", "message?offset=0&limit=200", f"history?after={message.id}")
        async with client_for(ctx.user_id, ctx.workspace_id) as client:
            for suffix in suffixes:
                response = await client.get(f"/api/agent/session/{ctx.session_id}/{suffix}")
                assert response.status_code == 200, response.text
                assert "PRIVATE_DERIVED_ANSWER" in response.text
            async with get_db_session() as db:
                source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
                source.data = {**source.data, "text": "Original evidence changed"}
            # Revocation is not retroactive (D1): the saved answer is unchanged.
            for suffix in suffixes:
                response = await client.get(f"/api/agent/session/{ctx.session_id}/{suffix}")
                assert response.status_code == 200, response.text
                answer = next(row for row in page_rows(suffix, response) if row["id"] == message.id)
                assert "source_status" not in answer
                assert [part["text"] for part in answer["parts"] if part["type"] == "text"] == ["PRIVATE_DERIVED_ANSWER"]
            assert "PRIVATE_DERIVED_ANSWER" in json.dumps(await public_messages(session, cached, actor_user_id=ctx.user_id))
            async with get_db_session() as db:
                (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
            assert (await client.get(f"/api/agent/session/{ctx.session_id}/history")).status_code == 404
    finally:
        await lease.release(session_status="idle")


async def test_unfinished_assistant_answer_pages_like_an_ordinary_session():
    ctx, lease, message, _, _ = await read_turn()
    try:
        await save_part(TextPart(session_id=ctx.session_id, message_id=message.id,
            text="STREAMING_TEXT"), is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
        async with client_for(ctx.user_id, ctx.workspace_id) as client:
            response = await client.get(f"/api/agent/session/{ctx.session_id}/history")
            assert response.status_code == 200
            row = next(row for row in response.json()["messages"] if row["id"] == message.id)
            assert row["finish"] is None and "source_status" not in row and "assistant_timing" not in row
            assert [part["text"] for part in row["parts"] if part["type"] == "text"] == ["STREAMING_TEXT"]
    finally:
        await lease.release(session_status="idle")
