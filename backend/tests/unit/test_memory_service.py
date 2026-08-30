"""Creator-memory service: lifecycle, ownership, truncation, expiry."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.user import User
from memory import service as memory_service


async def _make_user() -> str:
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"memory-{suffix}", created_at=now, updated_at=now))
    return user_id


@pytest.mark.asyncio
async def test_write_memory_creates_candidate_and_truncates_summary():
    user_id = await _make_user()
    row = await memory_service.write_memory(
        user_id=user_id,
        scope="SHORT_TERM",
        type="IMPRESSION",
        value={"summary": "x" * 5000},
        owner="SYSTEM_INFERRED",
        ttl_seconds=3600,
    )
    assert row["status"] == "CANDIDATE"
    assert len(row["value"]["summary"]) == memory_service.MAX_SUMMARY_CHARS


@pytest.mark.asyncio
async def test_write_memory_validates_scope_owner_and_proposal_only_notes():
    user_id = await _make_user()
    with pytest.raises(ValueError):
        await memory_service.write_memory(
            user_id=user_id, scope="PUBLIC", type="TAGS",
            value={"summary": "s"}, owner="SYSTEM_INFERRED",
        )
    with pytest.raises(ValueError):
        await memory_service.write_memory(
            user_id=user_id, scope="LONG_TERM", type="TAGS",
            value={"summary": "s"}, owner="ADMIN",
        )
    for note_type in (memory_service.PENDING_NOTE_TYPE, memory_service.USER_NOTE_TYPE):
        with pytest.raises(ValueError, match="propose_note"):
            await memory_service.write_memory(
                user_id=user_id, scope="LONG_TERM", type=note_type,
                value={"summary": "s"}, owner="SYSTEM_INFERRED",
            )


@pytest.mark.asyncio
async def test_propose_confirm_lifecycle_with_edited_summary():
    user_id = await _make_user()
    proposal = await memory_service.propose_note(
        user_id=user_id, summary="用户主打和田玉带货", session_id="session_x"
    )
    assert proposal["type"] == "PENDING_NOTE"
    assert proposal["status"] == "CANDIDATE"
    assert proposal["confidence"] == 30

    confirmed = await memory_service.confirm_note(
        user_id=user_id, proposal_id=proposal["id"], edited_summary="主营和田玉平安扣"
    )
    assert confirmed["type"] == "USER_NOTE"
    assert confirmed["owner"] == "USER_CONFIRMED"
    assert confirmed["status"] == "ACTIVE"
    assert confirmed["confidence"] == 90
    assert confirmed["value"]["summary"] == "主营和田玉平安扣"
    # A confirmed note is no longer pending.
    assert await memory_service.confirm_note(user_id=user_id, proposal_id=proposal["id"]) is None


@pytest.mark.asyncio
async def test_reject_note_soft_deletes_and_cross_user_isolation():
    user_a = await _make_user()
    user_b = await _make_user()
    proposal = await memory_service.propose_note(user_id=user_a, summary="fact")
    # Another user can neither confirm nor reject it.
    assert await memory_service.confirm_note(user_id=user_b, proposal_id=proposal["id"]) is None
    assert await memory_service.reject_note(user_id=user_b, proposal_id=proposal["id"]) is False
    assert await memory_service.reject_note(user_id=user_a, proposal_id=proposal["id"]) is True
    rows = await memory_service.search_memories(user_id=user_a, status="DEPRECATED")
    assert [row["id"] for row in rows] == [proposal["id"]]


@pytest.mark.asyncio
async def test_confirm_note_falls_back_to_newest_pending():
    user_id = await _make_user()
    await memory_service.propose_note(user_id=user_id, summary="older")
    newer = await memory_service.propose_note(user_id=user_id, summary="newer")
    confirmed = await memory_service.confirm_note(user_id=user_id)
    assert confirmed["id"] == newer["id"]


@pytest.mark.asyncio
async def test_search_orders_by_confidence_then_recency_and_filters_expiry():
    user_id = await _make_user()
    low = await memory_service.write_memory(
        user_id=user_id, scope="LONG_TERM", type="TAGS",
        value={"summary": "low"}, owner="SYSTEM_INFERRED", confidence=10,
    )
    high = await memory_service.write_memory(
        user_id=user_id, scope="LONG_TERM", type="TAGS",
        value={"summary": "high"}, owner="SYSTEM_INFERRED", confidence=95,
    )
    expired = await memory_service.write_memory(
        user_id=user_id, scope="SHORT_TERM", type="IMPRESSION",
        value={"summary": "expired"}, owner="SYSTEM_INFERRED", ttl_seconds=1,
    )
    # Force the ttl into the past.
    from sqlalchemy import update

    from db.models.memory import UserMemory

    async with get_db_session() as db:
        await db.execute(
            update(UserMemory)
            .where(UserMemory.id == expired["id"])
            .values(ttl=datetime(2020, 1, 1, tzinfo=timezone.utc))
        )
    rows = await memory_service.search_memories(user_id=user_id)
    ids = [row["id"] for row in rows]
    assert ids.index(high["id"]) < ids.index(low["id"])
    assert expired["id"] not in ids


@pytest.mark.asyncio
async def test_note_edit_and_delete_are_owner_checked():
    user_a = await _make_user()
    user_b = await _make_user()
    note = await memory_service.create_note(user_id=user_a, summary="original")
    assert note["status"] == "ACTIVE"
    assert await memory_service.edit_note(user_id=user_b, memory_id=note["id"], summary="hax") is None
    edited = await memory_service.edit_note(user_id=user_a, memory_id=note["id"], summary="edited")
    assert edited["value"]["summary"] == "edited"
    assert await memory_service.delete_memory(user_id=user_b, memory_id=note["id"]) is False
    assert await memory_service.delete_memory(user_id=user_a, memory_id=note["id"]) is True
    # Soft delete: gone from active listings, but the trace remains.
    assert await memory_service.list_active_memories(user_id=user_a) == []
