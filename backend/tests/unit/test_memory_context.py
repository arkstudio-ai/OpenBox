"""Context assembly: bucketing, ordering, and the PENDING_NOTE invariant."""
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from db.base import get_db_session
from tests.support.memory_scope import create_memory_user
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
    await create_memory_user(user_id, f"ctx-{suffix}", project_ids=(f"project_a_{user_id}", f"project_b_{user_id}"))
    return user_id


async def _confirmed_fact(**kwargs):
    row = await memory_service.write_memory(**kwargs)
    return await memory_service.confirm_note(user_id=kwargs['user_id'], proposal_id=row['id'], expected_revision=row['revision'])


def test_pending_note_is_in_neither_bucket():
    # The invariant that keeps unconfirmed proposals out of prompts.
    assert PENDING_NOTE_TYPE not in STABLE_TYPES | VOLATILE_TYPES


@pytest.mark.asyncio
async def test_assemble_renders_stable_and_volatile_sections():
    user_id = await _make_user()
    await _confirmed_fact(
        user_id=user_id, scope="LONG_TERM", type="VOICE",
        value={"summary": "亲切专业"}, owner="SYSTEM_INFERRED", confidence=80,
    )
    await _confirmed_fact(
        user_id=user_id, scope="LONG_TERM", type="BOUNDARY",
        value={"summary": "绝不夸大功效"}, owner="USER_CONFIRMED", confidence=95,
    )
    await _confirmed_fact(
        user_id=user_id, scope="SHORT_TERM", type="IMPRESSION",
        value={"summary": "今天想做端午专题"}, owner="SYSTEM_INFERRED",
    )
    result = await assemble_user_context(user_id=user_id)
    context = result["context"]
    sections = context.split("\n\n")
    stable = json.loads(sections[0].split("\n", 1)[1])
    recent = json.loads(sections[1].split("\n", 1)[1])
    assert [(item["category"], item["text"]) for item in stable] == [
        ("VOICE", "亲切专业"), ("BOUNDARY", "绝不夸大功效")]
    assert recent[0]["text"] == "今天想做端午专题"
    assert all(item["revision"] == 2 and item["confirmation_status"] == "CONFIRMED"
               for item in stable + recent)
    # Only explicitly confirmed rows may enter context.
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
        await _confirmed_fact(
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
    await _confirmed_fact(
        user_id=user_id, scope="LONG_TERM", type="IDENTITY",
        value={"summary": "global fact"}, owner="SYSTEM_INFERRED",
    )
    await _confirmed_fact(
        user_id=user_id, project_id=f"project_a_{user_id}", scope="LONG_TERM", type="GOAL",
        value={"summary": "project-a goal"}, owner="SYSTEM_INFERRED",
    )
    await _confirmed_fact(
        user_id=user_id, project_id=f"project_b_{user_id}", scope="LONG_TERM", type="GOAL",
        value={"summary": "project-b goal"}, owner="SYSTEM_INFERRED",
    )
    result = await assemble_user_context(user_id=user_id, project_id=f"project_a_{user_id}")
    assert "global fact" in result["context"]
    assert "project-a goal" in result["context"]
    assert "project-b goal" not in result["context"]
    records = json.loads(result["context"].split("\n", 1)[1])
    by_text = {item["text"]: item for item in records}
    assert by_text["global fact"]["storage_scope"]["project_id"] is None
    assert by_text["project-a goal"]["storage_scope"]["project_id"] == f"project_a_{user_id}"


@pytest.mark.asyncio
async def test_hit_counters_increment_on_assembly():
    user_id = await _make_user()
    row = await _confirmed_fact(
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


@pytest.mark.asyncio
async def test_same_category_does_not_merge_different_subjects():
    user_id = await _make_user()
    statements = ["用户维护北斗项目。", "同事黎安维护南星项目。"]
    ids = []
    for statement in statements:
        row = await _confirmed_fact(user_id=user_id, scope="LONG_TERM", type="IDENTITY",
                                    value={"summary": statement}, owner="SYSTEM_INFERRED")
        ids.append(row["id"])
    result = await assemble_user_context(user_id=user_id)
    records = json.loads(result["context"].split("\n", 1)[1])
    assert {item["id"]: item["text"] for item in records} == dict(zip(ids, statements))
