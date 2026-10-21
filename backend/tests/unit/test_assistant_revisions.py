"""Pending-plan revisions use real SQL, provenance and the durable input queue."""
from datetime import datetime, timezone
import asyncio

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.history import read_history
from assistant.requests import question_version
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask
from db.models.file_asset import FileAsset
from db.models.question import QuestionCheckpoint
from question import question as q
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_requests_v2 import ask_in
from tests.unit.test_assistant_sessions_v2 import (assistant, conversation, main_turn, tool_call,
                                                   quiet_runtime, human_turn)  # noqa: F401


async def setup(*, shared=False):
    owner, other, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "水果店视频", visibility="workspace" if shared else "private")
    request_id, _ = await ask_in(session, owner, question="确认30秒讲稿？", options=[{"label": "可以"}])
    asset_id = "asset_" + generate_id()
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request_id)
        version = question_version(row)
        db.add(FileAsset(id=asset_id, user_id=owner, workspace_id=workspace, session_id=main.id,
            name="水果店.jpg", mime="image/jpeg", oss_key=f"assets/{owner}/fruit.jpg", size=20,
            status="ready", created_at=datetime.now(timezone.utc)))
    ctx, lease, human = await main_turn(owner, workspace, main, "讲稿可以，但改用这张水果店照片作为背景重新做。")
    args = {"request_id": request_id, "version": version,
        "instructions": "沿用30秒讲稿，改用选定水果店图片作为背景，重新规划视频，费用待新方案确认。",
        "attachment_ids": [asset_id], "source_message_ids": [human]}
    return owner, other, workspace, main, session, ctx, lease, args


async def test_revision_is_atomic_idempotent_and_runs_in_original_conversation():
    owner, _, _, _, session, ctx, lease, args = await setup()
    try:
        first, _ = await tool_call(ctx, "requests.revise", args)
        assert first.get("state") == "accepted", first
        replay, _ = await tool_call(ctx, "requests.revise", args)
        assert replay == first
        async with get_db_session() as db:
            card = await db.get(QuestionCheckpoint, args["request_id"])
            item = await db.get(AgentInboxItem, first["inbox_id"])
            assert card.status == "superseded" and card.answers is None
            assert item.session_id == session.id and item.attachments == args["attachment_ids"]
            assert item.origin == "assistant_delegation" and item.prompt == args["instructions"]
            assert item.origin_ref["revision_request"]["request_id"] == card.id
            assert len((await db.scalars(select(AssistantTask).where(
                AssistantTask.execution_session_id == session.id))).all()) == 1
        with pytest.raises(q.QuestionGone):
            await q.reply(args["request_id"], [["可以"]], user_id=owner)
        execution_lease = await reserve_run(session.id, owner)
        try:
            batch = await inbox.claim_inbox_boundary(execution_lease, step=1, include_next_turn=True)
            assert batch.messages and batch.messages[0].id
            async with get_db_session() as db:
                item = await db.get(AgentInboxItem, first["inbox_id"])
                assert item.state == "claimed" and item.message_id == batch.messages[0].id
        finally:
            await execution_lease.release(session_status="idle")
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("failure", ["stale", "answered", "missing_asset", "foreign"])
async def test_failed_revision_preserves_card_and_creates_no_partial_link(failure):
    owner, other, workspace, main, session, ctx, lease, args = await setup()
    try:
        if failure == "stale": args["version"] = "0" * 64
        if failure == "answered": await q.reply(args["request_id"], [["可以"]], user_id=owner)
        if failure == "missing_asset": args["attachment_ids"] = ["asset_missing"]
        if failure == "foreign":
            foreign = await conversation(other, workspace, main, "别人的视频", visibility="private")
            args["request_id"], _ = await ask_in(foreign, other)
        async with get_db_session() as db:
            before = await db.scalar(select(QuestionCheckpoint.status).where(QuestionCheckpoint.session_id == session.id))
        if failure == "missing_asset":
            with pytest.raises(inbox.InboxAttachmentError):
                await tool_call(ctx, "requests.revise", args)
        else:
            result, _ = await tool_call(ctx, "requests.revise", args)
            assert result.get("error"), result
        async with get_db_session() as db:
            assert await db.scalar(select(QuestionCheckpoint.status).where(QuestionCheckpoint.session_id == session.id)) == before
            assert await db.scalar(select(AssistantTask.id).where(AssistantTask.execution_session_id == session.id)) is None
            assert not (await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == session.id, AgentInboxItem.origin == "assistant_delegation"))).all()
    finally:
        await lease.release(session_status="idle")


async def test_status_reads_and_shared_send_confirmation_leave_original_card_pending():
    owner, _, _, _, session, ctx, lease, args = await setup(shared=True)
    try:
        result, _ = await tool_call(ctx, "requests.list", {"kind": "question"})
        assert any(item["id"] == args["request_id"] for item in result["other_conversations"])
        inspected, _ = await tool_call(ctx, "requests.get", {"kind": "question", "request_id": args["request_id"]})
        assert inspected["state"] == "pending" and inspected["task_id"] is None
        assert inspected["version"] == args["version"]
        result, _ = await tool_call(ctx, "requests.revise", args)
        assert "suspended" in result
        async with get_db_session() as db:
            assert (await db.get(QuestionCheckpoint, args["request_id"])).status == "pending"
            assert await db.scalar(select(AssistantTask.id).where(AssistantTask.execution_session_id == session.id)) is None
        from assistant.confirmations import CONFIRM
        await q.reply(result["suspended"], [[CONFIRM]], user_id=owner)
        accepted, _ = await tool_call(ctx, "requests.revise", args)
        assert accepted.get("state") == "accepted", accepted
        replay, _ = await tool_call(ctx, "requests.revise", args)
        assert replay == accepted  # A retry must not ask for send consent twice.
    finally:
        await lease.release(session_status="idle")


async def test_ordinary_card_read_rejects_another_owners_question():
    _, other, workspace, main, _, ctx, lease, _ = await setup()
    foreign = await conversation(other, workspace, main, "另一人的任务", visibility="workspace")
    request_id, _ = await ask_in(foreign, other)
    try:
        value, _ = await tool_call(ctx, "requests.get", {"kind": "question", "request_id": request_id})
        assert value["error"] == "ASSISTANT_REQUEST_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")


async def test_revision_racing_a_manual_card_answer_applies_only_one_transition():
    owner, _, _, _, session, ctx, lease, args = await setup()
    try:
        results = await asyncio.gather(tool_call(ctx, "requests.revise", args),
                                     q.reply(args["request_id"], [["可以"]], user_id=owner),
                                     return_exceptions=True)
        async with get_db_session() as db:
            card = await db.get(QuestionCheckpoint, args["request_id"])
            items = list((await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == session.id, AgentInboxItem.origin == "assistant_delegation"))).all())
            assert card.status in {"answered", "superseded"}, results
            assert len(items) == int(card.status == "superseded"), results
            assert (card.answers is None) == (card.status == "superseded")
    finally:
        await lease.release(session_status="idle")


async def test_lost_wake_returns_committed_revision_receipt(monkeypatch):
    _, _, _, _, _, ctx, lease, args = await setup()
    def fail(*_):
        raise RuntimeError("wake unavailable")
    monkeypatch.setattr(inbox, "schedule_inbox_wake", fail)
    try:
        result, _ = await tool_call(ctx, "requests.revise", args)
        assert result["state"] == "accepted"
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, result["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_history_pagination_retains_scope_when_only_page_size_changes():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "水果店历史", visibility="private")
    for i in range(3):
        await human_turn(session.id, owner, f"要求{i}", f"回复{i}")
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id, session_id=session.id, include_tools=False)
    first = await read_history(**scope, limit=2, max_chars=5)
    second = await read_history(**scope, cursor=first["next_cursor"], limit=10, max_chars=5)
    assert first["next_cursor"] and second["items"]
    assert first["items"] != second["items"]
