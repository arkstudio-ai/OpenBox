"""Clearing someone's original words reaches copies of those words, not identical material elsewhere."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from db.base import get_db_session
from db.models.memory_v2 import MemoryTombstone
from memory import service
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401

TEXT = "项目预算为一千元"


async def active(scope, project):
    return [item["id"] for item in await service.list_active_memories(**identity(scope), project_id=project)]


@pytest.mark.parametrize("authority_scope", ["sqlite", "postgres"], indirect=True)
async def test_clearing_one_project_source_spares_the_same_words_elsewhere(authority_scope):
    scope = authority_scope
    first = await service.create_note(**identity(scope), project_id=scope["p1"], summary=TEXT)
    other = await service.create_note(**identity(scope), project_id=scope["p2"], summary=TEXT)
    [source] = await service.get_sources(**identity(scope), memory_id=first["id"])
    result = await service.forget_memory(**identity(scope), memory_id=first["id"], expected_revision=first["revision"],
                                         mode="sources", source_ids=[source["id"]])
    assert result["memory_ids"] == [first["id"]]
    assert await active(scope, scope["p1"]) == []
    # Identical, independent words in another project are untouched.
    assert await active(scope, scope["p2"]) == [other["id"]]
    # Saying it again later in the same project is new evidence.
    async with get_db_session() as db:
        await db.execute(update(MemoryTombstone).where(MemoryTombstone.user_id == scope["user_id"]).values(
            deleted_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    again = await service.create_note(**identity(scope), project_id=scope["p1"], summary=TEXT)
    assert await active(scope, scope["p1"]) == [again["id"]]


@pytest.mark.parametrize("authority_scope", ["sqlite", "postgres"], indirect=True)
async def test_the_detail_read_shows_current_text_only_while_its_sources_allow(authority_scope):
    scope = authority_scope
    note = await service.create_note(**identity(scope), project_id=scope["p1"], summary=TEXT)
    current = await service.get_memory(**identity(scope), memory_id=note["id"])
    assert current["body_available"] and current["summary"] == TEXT and current["revision"] == note["revision"]
    edited = await service.edit_note(**identity(scope), memory_id=note["id"], expected_revision=note["revision"],
                                     summary="项目预算为一千二百元")
    assert (await service.get_memory(**identity(scope), memory_id=note["id"]))["revision"] == edited["revision"]
    sources = await service.get_sources(**identity(scope), memory_id=note["id"])
    await service.forget_memory(**identity(scope), memory_id=note["id"], expected_revision=edited["revision"],
                                mode="sources", source_ids=[item["id"] for item in sources if not item["superseded"]])
    gone = await service.get_memory(**identity(scope), memory_id=note["id"])
    assert not gone["body_available"] and gone["summary"] == "" and gone["value"] == {}
    assert await service.get_memory(**{**identity(scope), "user_id": scope["other"]}, memory_id=note["id"]) is None
