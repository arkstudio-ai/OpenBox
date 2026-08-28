"""Cross-user prompt-hash reuse: hit path, miss guards, copy fallback."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

import tool.video_production as vp
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.user import User
from db.models.video_job import VideoJob
from tool.tool import ToolContext

PROMPT_HASH = None  # assigned per test via _seed_completed


async def _make_user() -> str:
    suffix = uuid4().hex[:10]
    user_id = f"user_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"dd-{suffix}", created_at=now, updated_at=now))
    return user_id


async def _seed_completed(user_id: str, *, deleted: bool = False) -> tuple[str, VideoJob, FileAsset]:
    prompt_hash = uuid4().hex + uuid4().hex  # 64 chars
    suffix = uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    asset = FileAsset(
        id=f"asset_{suffix}",
        user_id=user_id,
        session_id=None,
        project_id=None,
        name="segment.mp4",
        oss_key=f"assets/{user_id}/asset_{suffix}/segment.mp4",
        mime="video/mp4",
        size=1234,
        status="ready",
        source="agent",
        transient=False,
        is_deleted=deleted,
        created_at=now,
    )
    job = VideoJob(
        id=f"video_{suffix}",
        user_id=user_id,
        kind="segment",
        idempotency_key=f"key_{suffix}",
        request_hash="rh",
        prompt_hash=prompt_hash,
        status="completed",
        output_asset_id=asset.id,
        created_at=now,
        updated_at=now,
        completed_at=now,
    )
    async with get_db_session() as db:
        db.add(asset)
        db.add(job)
    return prompt_hash, job, asset


@pytest.mark.asyncio
async def test_reuse_hit_is_cross_user_and_newest_first():
    producer = await _make_user()
    prompt_hash, job, asset = await _seed_completed(producer)
    hit = await vp._find_reusable_segment(prompt_hash)
    assert hit is not None
    assert hit[0].id == job.id
    assert hit[1].id == asset.id


@pytest.mark.asyncio
async def test_soft_deleted_source_never_hits():
    producer = await _make_user()
    prompt_hash, _job, _asset = await _seed_completed(producer, deleted=True)
    assert await vp._find_reusable_segment(prompt_hash) is None


@pytest.mark.asyncio
async def test_complete_from_reuse_copies_into_new_users_key(monkeypatch):
    producer = await _make_user()
    consumer = await _make_user()
    prompt_hash, source_job, source_asset = await _seed_completed(producer)
    # The consumer's reserved (pending) job + asset, as _create_pending_job makes them.
    now = datetime.now(timezone.utc)
    suffix = uuid4().hex[:10]
    reserved_asset = FileAsset(
        id=f"asset_{suffix}",
        user_id=consumer,
        session_id=None,
        project_id=None,
        name="mine.mp4",
        oss_key=f"assets/{consumer}/asset_{suffix}/mine.mp4",
        mime="video/mp4",
        size=0,
        status="pending",
        source="agent",
        transient=False,
        created_at=now,
    )
    reserved_job = VideoJob(
        id=f"video_{suffix}",
        user_id=consumer,
        kind="segment",
        idempotency_key=f"key_{suffix}",
        request_hash="rh2",
        prompt_hash=prompt_hash,
        status="submitting",
        output_asset_id=reserved_asset.id,
        created_at=now,
        updated_at=now,
    )
    async with get_db_session() as db:
        db.add(reserved_asset)
        db.add(reserved_job)

    copies = {}

    class FakeOss:
        async def copy(self, src_key, dest_key):
            copies["src"] = src_key
            copies["dest"] = dest_key
            return {"size": 1234, "mime": "video/mp4", "etag": "abc"}

    monkeypatch.setattr("core.oss.get_oss", lambda: FakeOss())
    ctx = ToolContext(session_id="s", user_id=consumer)
    completed = await vp._complete_from_reuse(reserved_job, source_job, source_asset, ctx)

    assert completed.status == "completed"
    assert copies["src"] == source_asset.oss_key
    assert copies["dest"] == reserved_asset.oss_key  # own key, never shared
    assert completed.result_data["reuse"] is True
    assert completed.result_data["reused_from_job"] == source_job.id
    async with get_db_session() as db:
        refreshed = await db.get(FileAsset, reserved_asset.id)
        assert refreshed.status == "ready"
        assert refreshed.size == 1234


@pytest.mark.asyncio
async def test_copy_failure_returns_none_for_paid_fallback(monkeypatch):
    producer = await _make_user()
    consumer = await _make_user()
    prompt_hash, source_job, source_asset = await _seed_completed(producer)
    _hash2, reserved_job, _reserved_asset = await _seed_completed(consumer)

    class BrokenOss:
        async def copy(self, src_key, dest_key):
            return None

    monkeypatch.setattr("core.oss.get_oss", lambda: BrokenOss())
    ctx = ToolContext(session_id="s", user_id=consumer)
    result = await vp._complete_from_reuse(reserved_job, source_job, source_asset, ctx)
    assert result is None  # caller proceeds with the normal paid submit
