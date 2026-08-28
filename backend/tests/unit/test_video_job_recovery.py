"""Phase 0.5 stopgap: stranded segment jobs converge without a live tool call."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from video import job_recovery

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

DUMMY_TARGET = SimpleNamespace(provider="doubao", model="m", api_key="k", base_url="https://api.example")
DUMMY_SETTINGS = SimpleNamespace(max_provider_output_bytes=10**9, poll_interval_seconds=5)


async def _insert_asset(user_id: str) -> str:
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    asset_id = "asset_" + uuid.uuid4().hex[:12]
    async with get_db_session() as db:
        db.add(FileAsset(
            id=asset_id,
            user_id=user_id,
            session_id=None,
            project_id=None,
            name="v.mp4",
            oss_key=f"assets/{user_id}/{asset_id}/v.mp4",
            mime="video/mp4",
            size=0,
            status="pending",
            source="agent",
            transient=False,
            created_at=NOW(),
        ))
    return asset_id


async def _insert_job(*, age_seconds: int, **overrides) -> str:
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    user_id = overrides.pop("user_id", "u_" + uuid.uuid4().hex[:8])
    job_id = "vjob_" + uuid.uuid4().hex[:12]
    stamp = NOW() - timedelta(seconds=age_seconds)
    fields = dict(
        id=job_id,
        user_id=user_id,
        session_id=None,
        project_id=None,
        kind="segment",
        production_id=None,
        segment_id=None,
        idempotency_key=job_id,
        request_hash="",
        status="in_progress",
        provider_task_id="task_" + uuid.uuid4().hex[:8],
        output_asset_id=await _insert_asset(user_id),
        request_data={},
        result_data={},
        attempt=1,
        created_at=stamp,
        updated_at=stamp,
    )
    fields.update(overrides)
    async with get_db_session() as db:
        db.add(VideoJob(**fields))
    return job_id


async def _fetch(job_id: str):
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return await db.get(VideoJob, job_id)


async def _fetch_asset(asset_id: str):
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    async with get_db_session() as db:
        return await db.get(FileAsset, asset_id)


def _patch_provider(monkeypatch, payload, calls=None):
    from tool import video_production as vp

    monkeypatch.setattr(vp, "_configured_target", lambda model_override=None: (DUMMY_TARGET, DUMMY_SETTINGS))

    async def fake_status(target, task_id):
        if calls is not None:
            calls.append(task_id)
        return payload

    monkeypatch.setattr(vp, "_provider_status", fake_status)

    async def fake_copy(url, oss, key, max_bytes):
        return 4321

    monkeypatch.setattr(vp, "_copy_provider_video_to_oss", fake_copy)

    import core.oss
    monkeypatch.setattr(core.oss, "get_oss", lambda: None)


async def test_stale_in_progress_finalizes_when_provider_done(monkeypatch):
    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"})
    job_id = await _insert_job(age_seconds=600)

    advanced = await job_recovery.sweep()

    assert advanced == 1
    job = await _fetch(job_id)
    assert job.status == "completed"
    asset = await _fetch_asset(job.output_asset_id)
    assert asset.status == "ready"
    assert asset.size == 4321


async def test_stale_finalizing_reclaimed_and_completed(monkeypatch):
    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"})
    job_id = await _insert_job(age_seconds=600, status="finalizing")

    advanced = await job_recovery.sweep()

    assert advanced == 1
    job = await _fetch(job_id)
    assert job.status == "completed"


async def test_recent_finalizing_left_alone(monkeypatch):
    calls: list[str] = []
    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"}, calls)
    # Stale enough to be selected (>120s) but younger than the 300s
    # finalization threshold: another process may still be uploading.
    job_id = await _insert_job(age_seconds=200, status="finalizing")

    advanced = await job_recovery.sweep()

    assert advanced == 0
    assert calls == []
    assert (await _fetch(job_id)).status == "finalizing"


async def test_provider_still_running_only_refreshes(monkeypatch):
    _patch_provider(monkeypatch, {"status": "running"})
    job_id = await _insert_job(age_seconds=600)

    advanced = await job_recovery.sweep()

    assert advanced == 0
    job = await _fetch(job_id)
    assert job.status == "in_progress"
    assert (await _fetch_asset(job.output_asset_id)).status == "pending"


async def test_provider_failed_settles_job(monkeypatch):
    _patch_provider(monkeypatch, {"status": "failed", "error": {"message": "boom"}})
    job_id = await _insert_job(age_seconds=600)

    advanced = await job_recovery.sweep()

    assert advanced == 1
    job = await _fetch(job_id)
    assert job.status == "failed"
    assert job.error == "boom"
    assert (await _fetch_asset(job.output_asset_id)).status == "failed"


async def test_fresh_job_untouched(monkeypatch):
    calls: list[str] = []
    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"}, calls)
    job_id = await _insert_job(age_seconds=10)

    advanced = await job_recovery.sweep()

    assert advanced == 0
    assert calls == []
    assert (await _fetch(job_id)).status == "in_progress"


async def test_ambiguous_submitting_untouched(monkeypatch):
    calls: list[str] = []
    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"}, calls)
    job_id = await _insert_job(age_seconds=600, status="submitting", provider_task_id=None)

    advanced = await job_recovery.sweep()

    assert advanced == 0
    assert calls == []
    assert (await _fetch(job_id)).status == "submitting"


async def test_segment_business_row_updated(monkeypatch):
    from db.base import get_db_session
    from db.models.video_production import VideoProduction, VideoSegment

    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"})
    user_id = "u_" + uuid.uuid4().hex[:8]
    production_id = "prod_" + uuid.uuid4().hex[:8]
    segment_id = "seg_" + uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        db.add(VideoProduction(
            id=production_id, user_id=user_id, title="t", brief="b",
            created_at=NOW(), updated_at=NOW(),
        ))
        db.add(VideoSegment(
            id=segment_id, production_id=production_id, ordinal=1,
            script_text="s", prompt="p", content_hash="h",
            created_at=NOW(), updated_at=NOW(),
        ))
    job_id = await _insert_job(
        age_seconds=600, user_id=user_id,
        production_id=production_id, segment_id=segment_id,
    )

    advanced = await job_recovery.sweep()

    assert advanced == 1
    job = await _fetch(job_id)
    assert job.status == "completed"
    async with get_db_session() as db:
        segment = await db.get(VideoSegment, segment_id)
    assert segment.status == "generated"
    assert segment.output_asset_id == job.output_asset_id
    assert segment.generation_job_id == job_id
