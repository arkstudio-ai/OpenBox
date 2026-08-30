"""The creator_context tool: identity from ToolContext, proposal card branches."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.user import User
from memory import service as memory_service
from question.question import QuestionRejectedError
from tool.creator_context import (
    CreatorContextArgs,
    creator_context_tool,
    execute_creator_context,
)
from tool.tool import ToolContext


async def _make_ctx() -> ToolContext:
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"cc-{suffix}", created_at=now, updated_at=now))
    return ToolContext(session_id=f"session_{suffix}", user_id=user_id, message_id="m1", part_id="p1")


def test_tool_is_registered_and_not_parallel_safe():
    assert creator_context_tool.parallel_safe is False
    assert creator_context_tool.sandbox_required is False
    from tool.registry import register_builtin_tools, get_tool

    register_builtin_tools()
    assert get_tool("creator_context") is not None


def test_args_validation():
    with pytest.raises(ValueError, match="write_memory requires"):
        CreatorContextArgs(action="write_memory")
    for note_type in ("PENDING_NOTE", "USER_NOTE"):
        with pytest.raises(ValueError, match="propose_memory"):
            CreatorContextArgs(
                action="write_memory", scope="LONG_TERM", type=note_type,
                value={"summary": "s"}, owner="SYSTEM_INFERRED",
            )
    with pytest.raises(ValueError, match="summary"):
        CreatorContextArgs(action="propose_memory")


@pytest.mark.asyncio
async def test_identity_comes_only_from_tool_context():
    ctx = await _make_ctx()
    # No user-id argument exists on the schema at all.
    assert "user_id" not in CreatorContextArgs.model_fields
    await execute_creator_context(
        CreatorContextArgs(
            action="write_memory", scope="SHORT_TERM", type="IMPRESSION",
            value={"summary": "会话印象"}, owner="SYSTEM_INFERRED", ttl_seconds=60,
        ),
        ctx,
    )
    rows = await memory_service.search_memories(user_id=ctx.user_id)
    assert len(rows) == 1
    other = await memory_service.search_memories(user_id="someone_else")
    assert other == []


@pytest.mark.asyncio
async def test_get_user_context_empty_and_populated(monkeypatch):
    ctx = await _make_ctx()
    empty = await execute_creator_context(CreatorContextArgs(action="get_user_context"), ctx)
    assert empty.title == "No creator context yet"
    await memory_service.create_note(user_id=ctx.user_id, summary="主营翡翠饰品")
    filled = await execute_creator_context(CreatorContextArgs(action="get_user_context"), ctx)
    assert "主营翡翠饰品" in filled.output


@pytest.mark.asyncio
async def test_proposal_confirmed(monkeypatch):
    ctx = await _make_ctx()

    async def approve(session_id, questions, tool=None, user_id="default"):
        assert questions[0].detail["kind"] == "memory_proposal"
        return [["记住"]]

    monkeypatch.setattr("tool.creator_context.question_mod.ask", approve)
    result = await execute_creator_context(
        CreatorContextArgs(action="propose_memory", summary="主打翡翠带货"), ctx
    )
    assert result.metadata["decision"] == "confirmed"
    active = await memory_service.list_active_memories(user_id=ctx.user_id)
    assert active[0]["type"] == "USER_NOTE"
    assert active[0]["confidence"] == 90


@pytest.mark.asyncio
async def test_proposal_rejected(monkeypatch):
    ctx = await _make_ctx()

    async def reject(session_id, questions, tool=None, user_id="default"):
        return [["不用记"]]

    monkeypatch.setattr("tool.creator_context.question_mod.ask", reject)
    result = await execute_creator_context(
        CreatorContextArgs(action="propose_memory", summary="别记这个"), ctx
    )
    assert result.metadata["decision"] == "rejected"
    assert await memory_service.list_active_memories(user_id=ctx.user_id) == []


@pytest.mark.asyncio
async def test_proposal_custom_text_confirms_with_edit(monkeypatch):
    ctx = await _make_ctx()

    async def custom(session_id, questions, tool=None, user_id="default"):
        return [["其实是主营和田玉"]]

    monkeypatch.setattr("tool.creator_context.question_mod.ask", custom)
    result = await execute_creator_context(
        CreatorContextArgs(action="propose_memory", summary="主营翡翠"), ctx
    )
    assert result.metadata["decision"] == "confirmed_edited"
    active = await memory_service.list_active_memories(user_id=ctx.user_id)
    assert active[0]["value"]["summary"] == "其实是主营和田玉"


@pytest.mark.asyncio
async def test_proposal_dismissed_stays_pending_and_out_of_context(monkeypatch):
    ctx = await _make_ctx()

    async def dismiss(session_id, questions, tool=None, user_id="default"):
        raise QuestionRejectedError("dismissed")

    monkeypatch.setattr("tool.creator_context.question_mod.ask", dismiss)
    result = await execute_creator_context(
        CreatorContextArgs(action="propose_memory", summary="悬而未决"), ctx
    )
    assert result.metadata["decision"] == "dismissed"
    assert "NOT saved" in result.output
    pending = await memory_service.search_memories(
        user_id=ctx.user_id, type="PENDING_NOTE", status="CANDIDATE"
    )
    assert len(pending) == 1
    from memory.context import assemble_user_context

    assembled = await assemble_user_context(user_id=ctx.user_id)
    assert "悬而未决" not in assembled["context"]
