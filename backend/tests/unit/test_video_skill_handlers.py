"""video-production builtin skill: paid submit/poll/finalize as skill job steps."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from skill_runtime import registry, repository as repo, service
from skill_runtime.types import JobStatus

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

DUMMY_TARGET = SimpleNamespace(
    provider="doubao", model="seedance-1", api_key="k",
    base_url="https://api.example", wire_format="tokenspace_contents",
    submit_timeout_seconds=30, status_timeout_seconds=30,
)
DUMMY_SETTINGS = SimpleNamespace(
    poll_interval_seconds=5, max_provider_output_bytes=10**9,
    provider_input_url_ttl_seconds=600,
)


async def _make_domain(user_id):
    from db.base import get_db_session
    from db.models.video_production import VideoProduction, VideoSegment

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
    return production_id, segment_id


class Providers:
    """Recorded provider seams; status payload is mutable per test."""

    def __init__(self):
        self.submits = []
        self.cancels = []
        self.approvals = []
        self.status_payload = {"status": "running"}

    def install(self, monkeypatch, production_id, segment_id):
        from tool import video_production as vp
        from tool import video_workflow as vw

        monkeypatch.setattr(vp, "_configured_target", lambda model_override=None: (DUMMY_TARGET, DUMMY_SETTINGS))
        monkeypatch.setattr(vp, "_validate_generation", lambda *a, **k: None)

        async def resolve_inputs(character_reference_asset, input_assets, ctx):
            return [], None

        monkeypatch.setattr(vp, "_resolve_generation_inputs", resolve_inputs)

        async def materialize(*a, **k):
            return [], []

        monkeypatch.setattr(vp, "_materialize_provider_inputs", materialize)

        approved = {
            "production_id": production_id,
            "segment_id": segment_id,
            "prompt": "a talking cat",
            "resolution": "720p",
            "ratio": "9:16",
            "duration": 5,
            "generate_audio": True,
            "watermark": False,
            "content_hash": "ch",
            "plan_hash": "ph",
            "character_reference_asset": None,
            "input_assets": [],
            "character_reference_type": "virtual",
            "character_identity_id": None,
            "spend_approval_id": "appr_1",
        }

        async def prepare(ctx, pid, sid):
            assert pid == production_id and sid == segment_id
            return dict(approved)

        monkeypatch.setattr(vw, "prepare_segment_submission", prepare)

        async def consume(approval_id):
            self.approvals.append(approval_id)

        monkeypatch.setattr(vw, "consume_spend_approval", consume)

        async def submit(target, payload):
            self.submits.append(payload)
            return {"id": "ptask_1", "status": "running"}

        monkeypatch.setattr(vp, "_provider_submit", submit)

        async def status(target, task_id):
            return dict(self.status_payload)

        monkeypatch.setattr(vp, "_provider_status", status)

        async def cancel(target, task_id):
            self.cancels.append(task_id)

        monkeypatch.setattr(vp, "_provider_cancel", cancel)

        async def copy(url, oss, key, max_bytes):
            return 777

        monkeypatch.setattr(vp, "_copy_provider_video_to_oss", copy)

        import core.oss
        monkeypatch.setattr(core.oss, "get_oss", lambda: None)
        return self


@pytest.fixture
def enable_write(monkeypatch):
    from core.config import get_config

    monkeypatch.setattr(get_config(), "skill_jobs_video_write", True)


@pytest.fixture
def fast_poll(monkeypatch):
    from builtin_skills.video_production import handlers

    monkeypatch.setattr(handlers, "_poll_seconds", lambda settings: 0.0)


def _worker():
    from skill_runtime.worker import SkillJobWorker

    registry.load_builtin_handlers()
    return SkillJobWorker(queues=("default",), concurrency=1, per_user_limit=0)


async def _start(user_id, production_id, segment_id):
    job, _ = await service.start_job(
        user_id=user_id,
        skill_key="builtin:video-production",
        operation="segment.generate",
        input_data={"production_id": production_id, "segment_id": segment_id},
        idempotency_key="tc-" + uuid.uuid4().hex[:8],
    )
    return job


async def _drive_until(worker, job, statuses, ticks=40):
    from skill_runtime.reconciler import requeue_due_external

    fresh = await repo.get_job(job.id, job.user_id)
    for _ in range(ticks):
        if fresh.status in statuses:
            return fresh
        await requeue_due_external()
        await worker.run_once()
        await worker.drain()
        fresh = await repo.get_job(job.id, job.user_id)
    return fresh


async def _video_job_for(segment_id):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return (
            await db.execute(select(VideoJob).where(VideoJob.segment_id == segment_id))
        ).scalar_one_or_none()


async def test_flag_off_blocks_admission():
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    with pytest.raises(service.SkillDisabled, match="skill_jobs_video_write"):
        await _start(user, production_id, segment_id)


async def test_generate_full_happy_path(monkeypatch, enable_write, fast_poll):
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    providers = Providers().install(monkeypatch, production_id, segment_id)
    worker = _worker()

    job = await _start(user, production_id, segment_id)
    waiting = await _drive_until(worker, job, {JobStatus.WAITING_EXTERNAL.value})
    assert waiting.status == JobStatus.WAITING_EXTERNAL.value
    assert waiting.checkpoint_data.get("video_job_id")
    assert waiting.phase == "provider_generate"
    assert len(providers.submits) == 1
    assert providers.approvals == ["appr_1"]

    video_job = await _video_job_for(segment_id)
    assert video_job.provider_task_id == "ptask_1"
    assert video_job.status == "in_progress"
    assert video_job.idempotency_key == f"{production_id}:{segment_id}:generate"

    providers.status_payload = {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"}
    done = await _drive_until(worker, job, {JobStatus.SUCCEEDED.value})
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result_data["video_job_id"] == video_job.id
    assert done.result_data["asset_id"] == video_job.output_asset_id

    video_job = await _video_job_for(segment_id)
    assert video_job.status == "completed"

    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_production import VideoSegment

    async with get_db_session() as db:
        asset = await db.get(FileAsset, video_job.output_asset_id)
        segment = await db.get(VideoSegment, segment_id)
    assert asset.status == "ready" and asset.size == 777
    assert segment.status == "generated"
    assert segment.output_asset_id == video_job.output_asset_id

    artifacts = await repo.list_artifacts(job.id, user)
    assert [a["assetId"] for a in artifacts] == [video_job.output_asset_id]

    # Only one paid submit across the whole lifecycle.
    assert len(providers.submits) == 1
    assert providers.approvals == ["appr_1"]


async def test_second_job_adopts_existing_without_resubmit(monkeypatch, enable_write, fast_poll):
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    providers = Providers().install(monkeypatch, production_id, segment_id)
    worker = _worker()

    first = await _start(user, production_id, segment_id)
    await _drive_until(worker, first, {JobStatus.WAITING_EXTERNAL.value})
    providers.status_payload = {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"}
    await _drive_until(worker, first, {JobStatus.SUCCEEDED.value})

    second = await _start(user, production_id, segment_id)
    done = await _drive_until(worker, second, {JobStatus.SUCCEEDED.value})
    assert done.status == JobStatus.SUCCEEDED.value
    assert len(providers.submits) == 1
    assert providers.approvals == ["appr_1"]


async def test_submit_unknown_parks_for_operator(monkeypatch, enable_write, fast_poll):
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    providers = Providers().install(monkeypatch, production_id, segment_id)

    from tool import video_production as vp

    async def exploding_submit(target, payload):
        providers.submits.append(payload)
        raise RuntimeError("socket dropped mid-flight")

    monkeypatch.setattr(vp, "_provider_submit", exploding_submit)
    worker = _worker()

    job = await _start(user, production_id, segment_id)
    parked = await _drive_until(worker, job, {JobStatus.WAITING_USER.value})
    assert parked.status == JobStatus.WAITING_USER.value
    assert "人工核实" in parked.progress_data["prompt"]
    assert len(providers.submits) == 1

    video_job = await _video_job_for(segment_id)
    assert video_job.status == "submitting"
    assert video_job.provider_task_id is None

    # An operator wake without resolution must not resubmit.
    await repo.add_input(job.id, user, kind="operator_resume", payload={}, idempotency_key="op-1")
    parked_again = await _drive_until(worker, job, {JobStatus.WAITING_USER.value})
    assert parked_again.status == JobStatus.WAITING_USER.value
    assert len(providers.submits) == 1


async def test_provider_failure_settles_segment(monkeypatch, enable_write, fast_poll):
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    providers = Providers().install(monkeypatch, production_id, segment_id)
    worker = _worker()

    job = await _start(user, production_id, segment_id)
    await _drive_until(worker, job, {JobStatus.WAITING_EXTERNAL.value})
    providers.status_payload = {"status": "failed", "error": {"message": "nsfw rejected"}}
    failed = await _drive_until(worker, job, {JobStatus.FAILED.value})
    assert failed.status == JobStatus.FAILED.value
    assert failed.error_code == "provider_failed"
    assert "nsfw" in failed.error_message

    video_job = await _video_job_for(segment_id)
    assert video_job.status == "failed"


async def test_cancel_during_wait_cancels_provider(monkeypatch, enable_write, fast_poll):
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    providers = Providers().install(monkeypatch, production_id, segment_id)
    worker = _worker()

    job = await _start(user, production_id, segment_id)
    await _drive_until(worker, job, {JobStatus.WAITING_EXTERNAL.value})

    await repo.request_cancel(job.id, user)
    cancelled = await _drive_until(worker, job, {JobStatus.CANCELLED.value})
    assert cancelled.status == JobStatus.CANCELLED.value
    assert providers.cancels == ["ptask_1"]

    video_job = await _video_job_for(segment_id)
    assert video_job.status == "cancelled"


async def test_cancel_race_with_provider_success_keeps_output(monkeypatch, enable_write, fast_poll):
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    providers = Providers().install(monkeypatch, production_id, segment_id)
    worker = _worker()

    job = await _start(user, production_id, segment_id)
    await _drive_until(worker, job, {JobStatus.WAITING_EXTERNAL.value})

    # The provider finishes before the cancel lands: success wins (§7.4).
    providers.status_payload = {"status": "succeeded", "video_url": "https://cdn.example/v.mp4"}
    await repo.request_cancel(job.id, user)
    done = await _drive_until(worker, job, {JobStatus.SUCCEEDED.value, JobStatus.CANCELLED.value})
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result_data.get("cancel_race") is True
    assert providers.cancels == []

    video_job = await _video_job_for(segment_id)
    assert video_job.status == "completed"


async def test_production_status_read_only(monkeypatch):
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, segment_id = await _make_domain(user)
    worker = _worker()

    job, _ = await service.start_job(
        user_id=user,
        skill_key="builtin:video-production",
        operation="production.status",
        input_data={"production_id": production_id},
        idempotency_key="st-" + uuid.uuid4().hex[:6],
    )
    done = await _drive_until(worker, job, {JobStatus.SUCCEEDED.value})
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result_data["production_id"] == production_id
    assert done.result_data["segments"][0]["segment_id"] == segment_id


async def test_production_status_foreign_user_denied():
    user = "u_" + uuid.uuid4().hex[:8]
    production_id, _ = await _make_domain(user)
    stranger = "u_" + uuid.uuid4().hex[:8]
    worker = _worker()

    job, _ = await service.start_job(
        user_id=stranger,
        skill_key="builtin:video-production",
        operation="production.status",
        input_data={"production_id": production_id},
        idempotency_key="st-" + uuid.uuid4().hex[:6],
    )
    done = await _drive_until(worker, job, {JobStatus.FAILED.value})
    assert done.status == JobStatus.FAILED.value
    assert done.error_code == "not_found"
