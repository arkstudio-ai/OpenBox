"""Stranded direct segment jobs converge without a live tool call."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from video import job_recovery

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

DUMMY_TARGET = SimpleNamespace(provider="bossip", model="m", api_key="k", base_url="https://api.example")
RELAY_TARGET = SimpleNamespace(
    provider="bossip",
    model="m",
    api_key="k",
    base_url="https://relay.example",
    channel="ark",
    wire_format="bossip_videos",
)
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
    from tool.video_providers import provider_route_fingerprint

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
        request_data={
            "provider_route_fingerprint": provider_route_fingerprint(DUMMY_TARGET),
            "provider_wire_format": "tokenspace_contents",
        },
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


async def _retire_test_job(job_id: str) -> None:
    """Keep an intentionally stranded row out of later shared-DB sweeps."""
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        if job is not None:
            job.status = "cancelled"
            job.updated_at = NOW()


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


async def test_legacy_runtime_link_does_not_block_domain_recovery(monkeypatch):
    """The removed orchestration ledger no longer owns linked domain rows."""
    from tool.video_providers import provider_route_fingerprint

    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"})
    job_id = await _insert_job(
        age_seconds=600,
        request_data={
            "skill_job_id": "retired_job_123",
            "provider_route_fingerprint": provider_route_fingerprint(DUMMY_TARGET),
            "provider_wire_format": "tokenspace_contents",
        },
    )

    advanced = await job_recovery.sweep()

    assert advanced == 1
    assert (await _fetch(job_id)).status == "completed"


async def test_legacy_direct_job_is_not_polled_through_a_new_relay(monkeypatch):
    """Missing wire metadata predates relay support and means TokenSpace."""
    from tool import video_production as vp

    calls: list[str] = []
    monkeypatch.setattr(
        vp,
        "_configured_target",
        lambda model_override=None: (RELAY_TARGET, DUMMY_SETTINGS),
    )

    async def must_not_poll(target, task_id):
        calls.append(task_id)
        raise AssertionError("a legacy TokenSpace task was sent to the relay")

    monkeypatch.setattr(vp, "_provider_status", must_not_poll)
    job_id = await _insert_job(age_seconds=600, request_data={})
    job_recovery._route_mismatch_once.discard(job_id)
    warnings: list[str] = []
    monkeypatch.setattr(job_recovery.log, "warning", warnings.append)

    try:
        assert await job_recovery.sweep() == 0
        assert await job_recovery.sweep() == 0

        assert calls == []
        assert (await _fetch(job_id)).status == "in_progress"
        assert len([message for message in warnings if job_id in message]) == 1
    finally:
        await _retire_test_job(job_id)


async def test_fingerprint_mismatch_is_not_polled_or_mutated(monkeypatch):
    from tool import video_production as vp
    from tool.video_providers import provider_route_fingerprint

    calls: list[str] = []
    monkeypatch.setattr(
        vp,
        "_configured_target",
        lambda model_override=None: (RELAY_TARGET, DUMMY_SETTINGS),
    )

    async def must_not_poll(_target, task_id):
        calls.append(task_id)
        raise AssertionError("a task fingerprinted for another route was polled")

    monkeypatch.setattr(vp, "_provider_status", must_not_poll)
    job_id = await _insert_job(
        age_seconds=600,
        request_data={
            "provider_route_fingerprint": provider_route_fingerprint(DUMMY_TARGET),
            "provider_wire_format": "tokenspace_contents",
        },
    )
    job_recovery._route_mismatch_once.discard(job_id)

    try:
        assert await job_recovery.sweep() == 0
        assert calls == []
        job = await _fetch(job_id)
        assert job.status == "in_progress"
        assert job.error is None
    finally:
        await _retire_test_job(job_id)


async def test_legacy_matching_relay_wire_without_fingerprint_is_quarantined(monkeypatch):
    from tool import video_production as vp

    calls: list[str] = []
    monkeypatch.setattr(
        vp,
        "_configured_target",
        lambda model_override=None: (RELAY_TARGET, DUMMY_SETTINGS),
    )

    async def must_not_poll(_target, task_id):
        calls.append(task_id)
        raise AssertionError("a legacy task without full route identity was polled")

    monkeypatch.setattr(vp, "_provider_status", must_not_poll)
    job_id = await _insert_job(
        age_seconds=600,
        request_data={"provider_wire_format": "bossip_videos"},
    )

    try:
        assert await job_recovery.sweep() == 0
        assert calls == []
        assert (await _fetch(job_id)).status == "in_progress"
    finally:
        await _retire_test_job(job_id)
        job_recovery._route_mismatch_once.discard(job_id)


async def test_matching_route_fingerprint_still_recovers(monkeypatch):
    from tool import video_production as vp
    from tool.video_providers import provider_route_fingerprint

    _patch_provider(
        monkeypatch,
        {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"},
    )
    monkeypatch.setattr(
        vp,
        "_configured_target",
        lambda model_override=None: (RELAY_TARGET, DUMMY_SETTINGS),
    )
    job_id = await _insert_job(
        age_seconds=600,
        request_data={
            "provider_route_fingerprint": provider_route_fingerprint(RELAY_TARGET),
            "provider_wire_format": "bossip_videos",
        },
    )

    assert await job_recovery.sweep() == 1
    assert (await _fetch(job_id)).status == "completed"


async def test_mismatch_batch_does_not_starve_a_newer_matching_job(monkeypatch):
    from tool import video_production as vp
    from tool.video_providers import provider_route_fingerprint

    calls: list[str] = []
    _patch_provider(
        monkeypatch,
        {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"},
        calls,
    )
    monkeypatch.setattr(
        vp,
        "_configured_target",
        lambda model_override=None: (RELAY_TARGET, DUMMY_SETTINGS),
    )
    mismatch_ids = [
        await _insert_job(
            age_seconds=700 - offset,
            request_data={"provider_wire_format": "tokenspace_contents"},
        )
        for offset in range(job_recovery.MAX_JOBS_PER_SWEEP)
    ]
    matching_id = await _insert_job(
        age_seconds=600,
        request_data={
            "provider_route_fingerprint": provider_route_fingerprint(RELAY_TARGET),
            "provider_wire_format": "bossip_videos",
        },
    )
    matching_task_id = (await _fetch(matching_id)).provider_task_id
    job_recovery._scan_after = None

    try:
        assert await job_recovery.sweep() == 0
        assert calls == []
        assert await job_recovery.sweep() == 1
        assert calls == [matching_task_id]
        assert (await _fetch(matching_id)).status == "completed"
    finally:
        for job_id in mismatch_ids:
            await _retire_test_job(job_id)
            job_recovery._route_mismatch_once.discard(job_id)
        job_recovery._scan_after = None


async def test_stale_finalizing_reclaimed_and_completed(monkeypatch):
    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"})
    job_id = await _insert_job(age_seconds=600, status="finalizing")

    advanced = await job_recovery.sweep()

    assert advanced == 1
    job = await _fetch(job_id)
    assert job.status == "completed"


async def test_route_mismatch_does_not_reclaim_stale_finalizing(monkeypatch):
    from tool import video_production as vp

    calls: list[str] = []
    monkeypatch.setattr(
        vp,
        "_configured_target",
        lambda model_override=None: (RELAY_TARGET, DUMMY_SETTINGS),
    )

    async def must_not_poll(_target, task_id):
        calls.append(task_id)
        raise AssertionError("mismatched stale finalization was polled")

    monkeypatch.setattr(vp, "_provider_status", must_not_poll)
    job_id = await _insert_job(
        age_seconds=600,
        status="finalizing",
        request_data={},
    )
    job_recovery._route_mismatch_once.discard(job_id)

    try:
        assert await job_recovery.sweep() == 0
        job = await _fetch(job_id)
        assert calls == []
        assert job.status == "finalizing"
        assert job.error is None
    finally:
        await _retire_test_job(job_id)


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


async def test_other_provider_http_error_remains_retryable(monkeypatch):
    import httpx

    from tool import video_production as vp
    from tool.video_providers import provider_route_fingerprint

    calls: list[str] = []
    monkeypatch.setattr(
        vp,
        "_configured_target",
        lambda model_override=None: (DUMMY_TARGET, DUMMY_SETTINGS),
    )

    async def unavailable(_target, task_id):
        calls.append(task_id)
        request = httpx.Request("GET", f"https://api.example/tasks/{task_id}")
        response = httpx.Response(503, request=request, json={"error": "unavailable"})
        raise httpx.HTTPStatusError(
            "service unavailable",
            request=request,
            response=response,
        )

    monkeypatch.setattr(vp, "_provider_status", unavailable)
    job_id = await _insert_job(
        age_seconds=600,
        request_data={
            "provider_route_fingerprint": provider_route_fingerprint(DUMMY_TARGET),
            "provider_wire_format": "tokenspace_contents",
        },
    )
    job_recovery._failed_once.discard(job_id)
    warnings: list[str] = []
    monkeypatch.setattr(job_recovery.log, "warning", warnings.append)

    try:
        assert await job_recovery.sweep() == 0
        assert await job_recovery.sweep() == 0
        assert calls == [(await _fetch(job_id)).provider_task_id] * 2
        assert len([message for message in warnings if job_id in message]) == 1
        job = await _fetch(job_id)
        assert job.status == "in_progress"
        assert job.error is None
    finally:
        await _retire_test_job(job_id)


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


async def test_recovery_settles_a_paid_job_with_no_production_attached(monkeypatch):
    """Generation is standalone now, so a stranded job has nothing to mirror.

    The point of the sweep is unchanged and is the only thing that matters:
    provider output that was already paid for must never be lost because the
    process that submitted it went away.
    """
    _patch_provider(monkeypatch, {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"})
    user_id = "u_" + uuid.uuid4().hex[:8]
    job_id = await _insert_job(age_seconds=600, user_id=user_id)

    advanced = await job_recovery.sweep()

    assert advanced == 1
    job = await _fetch(job_id)
    assert job.status == "completed"
    assert job.output_asset_id
    assert job.production_id is None and job.segment_id is None


