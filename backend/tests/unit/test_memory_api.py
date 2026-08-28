"""Memory HTTP endpoints: direct route-function calls (assets-router pattern)."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.memories import (
    ConfirmBody,
    CreateNoteBody,
    EditNoteBody,
    confirm_proposal,
    create_note,
    delete_memory,
    edit_note,
    list_memories,
    list_pending,
    reject_proposal,
)
from db.base import get_db_session
from db.models.user import User
from memory import service as memory_service


async def _make_user() -> dict:
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"api-{suffix}", created_at=now, updated_at=now))
    return {"user_id": user_id, "role": "admin"}


@pytest.mark.asyncio
async def test_create_list_edit_delete_note():
    user = await _make_user()
    created = await create_note(CreateNoteBody(summary="主营和田玉"), current_user=user)
    assert created["status"] == "ACTIVE"

    listing = await list_memories(limit=50, current_user=user)
    assert [row["id"] for row in listing["memories"]] == [created["id"]]

    edited = await edit_note(created["id"], EditNoteBody(summary="改为翡翠"), current_user=user)
    assert edited["value"]["summary"] == "改为翡翠"

    assert (await delete_memory(created["id"], current_user=user))["ok"] is True
    # Default listing hides DEPRECATED rows.
    assert (await list_memories(limit=50, current_user=user))["memories"] == []


@pytest.mark.asyncio
async def test_pending_confirm_and_reject_flow():
    user = await _make_user()
    proposal = await memory_service.propose_note(user_id=user["user_id"], summary="待确认")
    pending = await list_pending(current_user=user)
    assert [row["id"] for row in pending["memories"]] == [proposal["id"]]

    confirmed = await confirm_proposal(
        proposal["id"], ConfirmBody(edited_summary="确认后的表述"), current_user=user
    )
    assert confirmed["status"] == "ACTIVE"
    assert confirmed["value"]["summary"] == "确认后的表述"

    # Already confirmed → no longer a pending proposal.
    with pytest.raises(HTTPException) as exc:
        await reject_proposal(proposal["id"], current_user=user)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ownership_reads_as_404():
    owner = await _make_user()
    intruder = await _make_user()
    note = await create_note(CreateNoteBody(summary="private"), current_user=owner)
    for call in (
        edit_note(note["id"], EditNoteBody(summary="hax"), current_user=intruder),
        delete_memory(note["id"], current_user=intruder),
        confirm_proposal(note["id"], None, current_user=intruder),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 404
    assert (await list_memories(limit=50, current_user=intruder))["memories"] == []
