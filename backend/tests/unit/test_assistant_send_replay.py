"""A current send approval never turns display text into a second confirmation."""
from datetime import datetime, timezone
from dataclasses import replace

import pytest
from sqlalchemy import select

from assistant.confirmations import CONFIRM, KIND, confirmed_sends
from assistant.runtime import runtime_view
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.question import QuestionCheckpoint
from question import question as q
from question.continuation import apply_answers, resume_main_through_inbox
from agent import inbox
from agent.driver import reserve_run
from session.session import create_assistant_message
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import (
    assistant, finish_turn, followup, inputs, main_turn, quiet_runtime, tool_call, watched_conversation,
)  # noqa: F401


@pytest.mark.parametrize("operation", ["tasks.followup", "assets.attach"])
@pytest.mark.parametrize("changed", ["text", "attachment_ids", "task_id", "source_message_ids"])
async def test_current_send_rejects_drift_then_sends_original_once(operation, changed):
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "把照片转给视频任务，原时长和字幕不变。")
    asset_id = "asset_" + generate_id()
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=owner, workspace_id=workspace, session_id=main.id,
            name="未命名图片.jpg", mime="image/jpeg", oss_key=f"assets/{owner}/photo.jpg", size=20,
            status="ready", created_at=datetime.now(timezone.utc)))
    args = followup(linked["task_id"], linked["task_revision"], "改用选定照片，保留原时长和字幕。", human,
                    attachment_ids=[asset_id])
    try:
        asked, _ = await tool_call(ctx, operation, args)
        await finish_turn(ctx, lease, "等待确认发送。")
        await lease.release(session_status="idle")
        await q.reply(asked["suspended"], [[CONFIRM]], owner)
        generation = await apply_answers(main.id, owner)
        assert generation is not None
        assert await resume_main_through_inbox(main.id, owner, generation)
        lease = await reserve_run(main.id, owner)
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
            agent="assistant", user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
        ctx = replace(ctx, run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
        scope = await runtime_view(session_id=main.id, user_id=owner,
                                   run_id=ctx.run_id, generation=ctx.run_generation)
        assert scope["tool_ids"] == {operation}
        altered = {**args, changed: {
            "text": args["text"] + "\n\n附带文件（成员也能看到）：未命名图片.jpg",
            "attachment_ids": ["asset_invented"], "task_id": "different-task",
            "source_message_ids": ["different-human"],
        }[changed]}
        refused, _ = await tool_call(ctx, operation, altered)
        assert refused["error"] == "ASSISTANT_CONFIRMATION_INPUT_CHANGED"
        assert await inputs(session.id) == []
        async with get_db_session() as db:
            rows = list(await db.scalars(select(QuestionCheckpoint).where(
                QuestionCheckpoint.session_id == main.id)))
            assert len(rows) == 1 and rows[0].status == "answered"
            assert not rows[0].continuation[KIND].get("consumed")
            assert (await confirmed_sends(db, ctx))[0]["arguments"] == args
        accepted, _ = await tool_call(ctx, operation, args)
        assert accepted.get("state") == "accepted", accepted
        sent = await inputs(session.id)
        assert len(sent) == 1 and sent[0].prompt == args["text"] and sent[0].attachments == [asset_id]
        duplicate, _ = await tool_call(ctx, operation, args)
        assert duplicate["error"] == "ASSISTANT_CONFIRMATION_UNAVAILABLE"
        assert len(await inputs(session.id)) == 1
        async with get_db_session() as db:
            assert len(list(await db.scalars(select(QuestionCheckpoint).where(
                QuestionCheckpoint.session_id == main.id)))) == 1
            # A later human request is not bound to the previous approval.
            item = await db.scalar(select(AgentInboxItem).where(
                AgentInboxItem.run_id == ctx.run_id, AgentInboxItem.generation == ctx.run_generation))
            item.origin = "human"
        async with get_db_session() as db:
            assert await confirmed_sends(db, ctx, scope_only=True) == []
    finally:
        await lease.release(session_status="idle")
