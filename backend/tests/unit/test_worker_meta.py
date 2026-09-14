"""Metadata controls and ownership checks (SPEC §8.3, §8.5)."""
from datetime import datetime, timedelta, timezone

import pytest

from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import (TrajectoryMetaAsset, TrajectoryMetaSession, TrajectoryMetaUser,
    TrajectoryMetaWorkspace)
from trajectory.worker.meta import (DELETED, OWNERSHIP, MetaCache, apply_meta, mark_asset_deleted, mark_session_deleted,
    ownership_verdict, parse_time)

AT = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
async def trace_db(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    yield engine
    await close_trace_engine()


def _session(identifier, updated, **fields):
    return {"type": "session.meta", "session": {"id": identifier, "updated_at": updated, **fields}}


async def _apply(control, line_time=AT):
    async with trace_session() as db:
        return await apply_meta(db, MetaCache(), control["type"], control, line_time=line_time, now=AT)


async def test_session_upserts_are_last_writer_wins(trace_db):
    assert await _apply(_session("s1", "2026-09-14T08:00:00Z", user_id="u1", title="first", kind="normal"))
    assert not await _apply(_session("s1", "2026-09-14T07:59:59Z", user_id="u1", title="older"))
    assert await _apply(_session("s1", "2026-09-14T08:00:01Z", user_id="u1", title="newer", is_deleted=True,
                                 deleted_at="2026-09-14T08:00:01Z", model="m" * 200))
    # Equal timestamp: applies, but a stale snapshot cannot undelete.
    assert await _apply(_session("s1", "2026-09-14T08:00:01Z", user_id="u1", title="same", is_deleted=False))
    async with trace_session() as db:
        row = await db.get(TrajectoryMetaSession, "s1")
        assert (row.title, row.is_deleted, len(row.model), row.kind) == ("same", True, 128, "normal")
        assert parse_time(row.updated_at) == AT + timedelta(seconds=1)


async def test_invalid_records_are_ignored_and_line_time_fills_updated_at(trace_db):
    assert not await _apply({"type": "session.meta", "session": {"id": "s1", "updated_at": "2026-09-14T08:00:00Z"}})
    assert not await _apply({"type": "session.meta", "session": {"id": "x" * 65, "user_id": "u"}})
    assert not await _apply({"type": "user.meta", "user": "nope"})
    later = AT + timedelta(minutes=5)
    assert await _apply({"type": "user.meta", "user": {"id": "u1", "username": "alice", "email": "a@x", "role": "admin",
                                                       "is_active": False}}, line_time=later)
    assert await _apply({"type": "workspace.meta", "workspace": {"id": "w1", "name": "N" * 300,
                                                                 "updated_at": "2026-09-14T08:00:00Z"}})
    assert await _apply({"type": "asset.meta", "asset": {"id": "a1", "user_id": "u1", "oss_key": "assets/u1/a1/x.png",
                                                         "mime": "image/png", "size": 12, "status": "ready",
                                                         "updated_at": "2026-09-14T08:00:00Z"}})
    async with trace_session() as db:
        user = await db.get(TrajectoryMetaUser, "u1")
        assert (user.username, user.is_active, parse_time(user.updated_at)) == ("alice", False, later)
        assert len((await db.get(TrajectoryMetaWorkspace, "w1")).name) == 128
        asset = await db.get(TrajectoryMetaAsset, "a1")
        assert (asset.size, asset.mime, asset.is_deleted) == (12, "image/png", False)


async def test_session_deleted_creates_or_marks_the_replica(trace_db):
    async with trace_session() as db:
        cache = MetaCache()
        await mark_session_deleted(db, cache, session_id="s9", user_id="u1", deleted_at=AT, now=AT)
        assert cache.sessions["s9"].deleted
    await _apply(_session("s1", "2026-09-14T09:00:00Z", user_id="u1"))
    async with trace_session() as db:
        await mark_session_deleted(db, MetaCache(), session_id="s1", user_id="u1", deleted_at=AT, now=AT)
    async with trace_session() as db:
        created, marked = await db.get(TrajectoryMetaSession, "s9"), await db.get(TrajectoryMetaSession, "s1")
        assert created.is_deleted and created.user_id == "u1"
        assert marked.is_deleted and parse_time(marked.updated_at) == AT + timedelta(hours=1)
    # A later snapshot with an older timestamp cannot revive it.
    assert not await _apply(_session("s1", "2026-09-14T08:30:00Z", user_id="u1", is_deleted=False))


async def test_ownership_uses_known_metadata_only(trace_db):
    for identifier, user, parent, deleted in (("root", "u1", None, False), ("child", "u1", "root", False),
                                              ("grandchild", "u1", "child", False), ("other_root", "u1", None, False),
                                              ("stray", "u1", "other_root", False), ("foreign", "u2", "root", False),
                                              ("gone", "u1", "root", True), ("loop_a", "u1", "loop_b", False),
                                              ("loop_b", "u1", "loop_a", False), ("orphan", "u1", "missing", False)):
        await _apply(_session(identifier, "2026-09-14T08:00:00Z", user_id=user, parent_id=parent, is_deleted=deleted))
    async with trace_session() as db:
        cache = MetaCache()

        async def verdict(source, root="root", user="u1"):
            return await ownership_verdict(db, cache, user_id=user, root_session_id=root, source_session_id=source)

        assert await verdict("root") is None
        assert await verdict("grandchild") is None
        assert await verdict("unknown_child") is None
        assert await verdict("orphan") is None
        assert await verdict("stray") == OWNERSHIP
        assert await verdict("foreign") == OWNERSHIP
        assert await verdict("gone") == DELETED
        assert await verdict("loop_a") == OWNERSHIP
        assert await verdict("root", user="u2") == OWNERSHIP
        assert await verdict(None, root="unknown_root", user="anyone") is None
    async with trace_session() as db:
        await mark_session_deleted(db, MetaCache(), session_id="root", user_id="u1", deleted_at=AT, now=AT)
    async with trace_session() as db:
        assert await ownership_verdict(db, MetaCache(), user_id="u1", root_session_id="root",
                                       source_session_id="child") == DELETED


async def test_asset_deleted_creates_or_marks_the_replica(trace_db):
    # Payload revocation is trajectory.lifecycle.revoke_asset (tests/unit/test_worker_retention.py).
    async with trace_session() as db:
        cache = MetaCache()
        await mark_asset_deleted(db, cache, asset_id="asset_new", user_id="u1", deleted_at=AT, now=AT)
        assert cache.assets["asset_new"].deleted
        await mark_asset_deleted(db, MetaCache(), asset_id="asset_ownerless", user_id=None, deleted_at=AT, now=AT)
    assert await _apply({"type": "asset.meta", "asset": {"id": "asset_1", "user_id": "u1", "oss_key": "assets/u1/a.png",
                                                         "updated_at": "2026-09-14T07:00:00Z"}})
    later = AT + timedelta(hours=1)
    async with trace_session() as db:
        await mark_asset_deleted(db, MetaCache(), asset_id="asset_1", user_id="u1", deleted_at=later, now=later)
    async with trace_session() as db:
        created, marked = await db.get(TrajectoryMetaAsset, "asset_new"), await db.get(TrajectoryMetaAsset, "asset_1")
        assert (created.is_deleted, created.user_id, parse_time(created.deleted_at)) == (True, "u1", AT)
        assert await db.get(TrajectoryMetaAsset, "asset_ownerless") is None
        assert (marked.is_deleted, parse_time(marked.deleted_at), marked.oss_key) == (True, later, "assets/u1/a.png")
