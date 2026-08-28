"""Context assembly: bucketing, ordering, and the PENDING_NOTE invariant."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.user import User
from memory import service as memory_service
from memory.context import (
    PENDING_NOTE_TYPE,
    STABLE_TYPES,
    VOLATILE_TYPES,
    assemble_user_context,
)


async def _make_user() -> str:
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"ctx-{suffix}", created_at=now, updated_at=now))
    return user_id


def test_pending_note_is_in_neither_bucket():
    # The invariant that keeps unconfirmed proposals out of prompts.
    assert PENDING_NOTE_TYPE not in STABLE_TYPES | VOLATILE_TYPES


@pytest.mark.asyncio
async def test_assemble_renders_stable_and_volatile_sections():
    user_id = await _make_user()
    await memory_service.write_memory(
        user_id=user_id, scope="LONG_TERM", type="VOICE",
        value={"summary": "亲切专业"}, owner="SYSTEM_INFERRED", confidence=80,
    )
    await memory_service.write_memory(
        user_id=user_id, scope="LONG_TERM", type="BOUNDARY",
        value={"summary": "绝不夸大功效"}, owner="USER_CONFIRMED", confidence=95,
    )
    await memory_service.write_memory(
        user_id=user_id, scope="SHORT_TERM", type="IMPRESSION",
        value={"summary": "今天想做端午专题"}, owner="SYSTEM_INFERRED",
    )
    result = await assemble_user_context(user_id=user_id)
    context = result["context"]
    assert "## 创作者人设(已知)" in context
    assert "- **表达风格**: 亲切专业" in context
    assert "- **边界**: 绝不夸大功效" in context
    assert "## 最近对话印象" in context
    assert "- 今天想做端午专题" in context
    # CANDIDATE rows are deliberately included.
    assert result["stats"]["stable"] == 2
    assert result["stats"]["volatile"] == 1


@pytest.mark.asyncio
async def test_pending_proposal_never_reaches_context():
    user_id = await _make_user()
    await memory_service.propose_note(user_id=user_id, summary="绝密的未确认提案")
    result = await assemble_user_context(user_id=user_id)
    assert "绝密的未确认提案" not in result["context"]
    assert result["context"] == ""


@pytest.mark.asyncio
async def test_deprecated_and_volatile_limit():
    user_id = await _make_user()
    note = await memory_service.create_note(user_id=user_id, summary="deleted later")
    await memory_service.delete_memory(user_id=user_id, memory_id=note["id"])
    for index in range(8):
        await memory_service.write_memory(
            user_id=user_id, scope="SHORT_TERM", type="IMPRESSION",
            value={"summary": f"impression-{index}"}, owner="SYSTEM_INFERRED",
            confidence=50 + index,
        )
    result = await assemble_user_context(user_id=user_id, volatile_limit=5)
    assert "deleted later" not in result["context"]
    assert result["stats"]["volatile"] == 5


@pytest.mark.asyncio
async def test_project_scoping_layers_project_rows_over_user_global():
    user_id = await _make_user()
    await memory_service.write_memory(
        user_id=user_id, scope="LONG_TERM", type="IDENTITY",
        value={"summary": "global fact"}, owner="SYSTEM_INFERRED",
    )
    await memory_service.write_memory(
        user_id=user_id, project_id="project_a", scope="LONG_TERM", type="GOAL",
        value={"summary": "project-a goal"}, owner="SYSTEM_INFERRED",
    )
    await memory_service.write_memory(
        user_id=user_id, project_id="project_b", scope="LONG_TERM", type="GOAL",
        value={"summary": "project-b goal"}, owner="SYSTEM_INFERRED",
    )
    result = await assemble_user_context(user_id=user_id, project_id="project_a")
    assert "global fact" in result["context"]
    assert "project-a goal" in result["context"]
    assert "project-b goal" not in result["context"]


@pytest.mark.asyncio
async def test_hit_counters_increment_on_assembly():
    user_id = await _make_user()
    row = await memory_service.write_memory(
        user_id=user_id, scope="LONG_TERM", type="TAGS",
        value={"summary": "翡翠"}, owner="SYSTEM_INFERRED",
    )
    await assemble_user_context(user_id=user_id)
    from sqlalchemy import select

    from db.models.memory import UserMemory

    async with get_db_session() as db:
        stored = (
            await db.execute(select(UserMemory).where(UserMemory.id == row["id"]))
        ).scalar_one()
        assert stored.hit_count == 1
        assert stored.last_hit_at is not None
