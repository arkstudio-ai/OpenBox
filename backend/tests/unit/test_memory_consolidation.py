"""Periodic consolidation of exact duplicates, and recall-aware ranking of core memories."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select

from core.config import MemoryConfig
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryOutbox, MemoryRevision, MemorySource, MemorySourceLink, MemoryTombstone
from memory import service
from memory.orchestrator import core_memory_candidates
from memory.outbox import MemoryIndexWorker
from memory.policy import resolve_access_scope
from memory.reconcile import consolidate_duplicates
from tests.support.memory_scope import create_memory_user

SAME = "用户习惯用表格对比方案。"


async def owner(*projects):
    user = f"user_{uuid4().hex[:10]}"
    workspace = await create_memory_user(user, user, project_ids=projects)
    return user, workspace


async def note(user, workspace, project, summary, *, age_minutes):
    created = await service.create_note(user_id=user, workspace_id=workspace, project_id=project, summary=summary)
    async with get_db_session() as db:
        row = await db.get(UserMemory, created["id"])
        row.updated_at = row.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return created["id"]


async def test_exact_duplicates_in_one_scope_are_superseded_never_deleted():
    project_a, project_b = f"prj-{uuid4().hex[:8]}", f"prj-{uuid4().hex[:8]}"
    user, workspace = await owner(project_a, project_b)
    config = MemoryConfig(v2_write=True, allowed_user_ids=[user])
    withdrawn = await note(user, workspace, project_a, SAME, age_minutes=1)
    newest = await note(user, workspace, project_a, "  用户习惯用表格对比方案。 ", age_minutes=2)
    older = await note(user, workspace, project_a, SAME, age_minutes=3)
    other_project = await note(user, workspace, project_b, SAME, age_minutes=4)
    personal_new = await note(user, workspace, None, SAME, age_minutes=5)
    personal_old = await note(user, workspace, None, SAME, age_minutes=6)
    different = await note(user, workspace, project_a, "用户习惯用列表。", age_minutes=7)
    async with get_db_session() as db:
        # The most recent copy no longer stands: its evidence is gone.
        source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == withdrawn))
        source.status = "UNAVAILABLE"
        before = {row.id: row.revision for row in (await db.scalars(select(UserMemory).where(
            UserMemory.user_id == user))).all()}

    result = await consolidate_duplicates(config)
    assert result["retired"] == 2 and result["next_offset"] == 0

    async with get_db_session() as db:
        rows = {row.id: row for row in (await db.scalars(select(UserMemory).where(UserMemory.user_id == user))).all()}
        assert set(rows) == set(before)  # Nothing hard-deleted.
        retired = {older: newest, personal_old: personal_new}
        for memory_id, kept in retired.items():
            row = rows[memory_id]
            assert row.status == "DEPRECATED" and row.valid_to is not None and row.deleted_at is None
            assert row.fact_identity is None and row.evidence["consolidated_into"] == kept
            assert row.revision == before[memory_id] + 1
            revision = await db.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == memory_id,
                                                                    MemoryRevision.revision == row.revision))
            assert revision.reason == "consolidated_duplicate" and revision.value["summary"]
            tombstone = await db.scalar(select(MemoryTombstone).where(MemoryTombstone.object_kind == "superseded",
                MemoryTombstone.object_id == f"{memory_id}:{before[memory_id]}"))
            # Marks the supersession without suppressing the still-remembered wording.
            assert tombstone.content_hash is None and tombstone.fact_key is None
            assert await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.object_id == memory_id,
                                                                 MemoryOutbox.operation == "DELETE"))
        for memory_id in (withdrawn, newest, other_project, personal_new, different):
            assert rows[memory_id].status == "ACTIVE" and rows[memory_id].revision == before[memory_id]
        # A fact consolidated away is still remembered, and is not suppressed.
        scope = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=project_a)
        assert not await service.is_candidate_suppressed(db, scope, summary=SAME, sources=[
            {"source_kind": "manual", "body": SAME, "occurred_at": datetime.now(timezone.utc) - timedelta(days=1)}])
    active = await service.list_active_memories(user_id=user, workspace_id=workspace, project_id=project_a)
    assert sorted(item["id"] for item in active) == sorted([newest, personal_new, different])
    assert (await consolidate_duplicates(config))["retired"] == 0


async def test_consolidation_is_bounded_and_pages_past_groups_it_cannot_settle():
    project = f"prj-{uuid4().hex[:8]}"
    user, workspace = await owner(project)
    config = MemoryConfig(v2_write=True, allowed_user_ids=[user])
    for text in ("甲事实。", "乙事实。", "丙事实。"):
        for age in (1, 2):
            await note(user, workspace, project, text, age_minutes=age)
    first = await consolidate_duplicates(config, groups=2)
    assert first == {"groups": 2, "retired": 2, "next_offset": 2}
    # Settled groups drop out of the listing: this page is past the end and wraps.
    second = await consolidate_duplicates(config, offset=first["next_offset"], groups=2)
    assert second == {"groups": 0, "retired": 0, "next_offset": 0}
    assert await consolidate_duplicates(config, offset=second["next_offset"], groups=2) == {
        "groups": 1, "retired": 1, "next_offset": 0}
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(UserMemory).where(
            UserMemory.user_id == user, UserMemory.status == "ACTIVE")) == 3
    assert (await consolidate_duplicates(MemoryConfig(v2_write=False, allowed_user_ids=[user])))["groups"] == 0


async def test_index_worker_tick_runs_consolidation():
    project = f"prj-{uuid4().hex[:8]}"
    user, workspace = await owner(project)
    for age in (1, 2):
        await note(user, workspace, project, SAME, age_minutes=age)
    worker = MemoryIndexWorker(MemoryConfig(v2_write=True, allowed_user_ids=[user]))
    assert (await worker._consolidate())["retired"] == 1 and worker._consolidation_offset == 0


async def test_core_block_prefers_recently_recalled_memories_within_a_tier():
    project = f"prj-{uuid4().hex[:8]}"
    user, workspace = await owner(project)
    now = datetime.now(timezone.utc)
    ids = {name: await note(user, workspace, project, f"记忆 {name}。", age_minutes=age)
           for name, age in (("hit_yesterday", 50), ("hit_now", 40), ("hit_now_often", 30),
                             ("stale_hits", 1), ("never_hit", 20), ("verified", 60))}
    hits = {"hit_yesterday": (now - timedelta(days=1), 1), "hit_now": (now - timedelta(hours=1), 1),
            "hit_now_often": (now - timedelta(hours=1), 5), "stale_hits": (now - timedelta(days=60), 100),
            "verified": (now - timedelta(minutes=5), 9)}
    async with get_db_session() as db:
        for name, (last_hit_at, hit_count) in hits.items():
            row = await db.get(UserMemory, ids[name])
            row.last_hit_at, row.hit_count = last_hit_at, hit_count
        # A recently recalled memory of a lower tier never jumps a higher one.
        verified = await db.get(UserMemory, ids["verified"])
        verified.owner, verified.type = "SYSTEM_VERIFIED", "PREFERENCE"
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=project)
        ranked = await core_memory_candidates(db, scope)
    names = {memory_id: name for name, memory_id in ids.items()}
    assert [names[memory_id] for memory_id in ranked] == [
        "hit_now_often", "hit_now", "hit_yesterday",  # recalled within the window: latest, then most often
        "stale_hits", "never_hit",                     # the rest by how recently they changed
        "verified"]
