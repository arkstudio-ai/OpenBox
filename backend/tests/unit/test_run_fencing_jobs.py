"""A paid submit refused by the run fence leaves no job waiting for a provider.

The refusal happens in capture_service_dispatch, before any provider call. A job
reserved for that call must not stay in its pre-submit state: an unfinished
"submitting" row is reported as an ambiguous paid submit forever, and a
"dispatching" composition never progresses.
"""
from uuid import uuid4

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from question import runtime
from tests.unit.test_run_fencing_api import acting_as


def revoked_run(ctx):
    """A run that another request already superseded."""
    ticket = runtime.RunTicket(ctx.session_id, ctx.user_id, 0, uuid4().hex)
    runtime.revoke(ticket.run_id, "superseded")
    return ticket


async def job_for(user_id: str, key: str) -> VideoJob:
    async with get_db_session() as db:
        return await db.scalar(select(VideoJob).where(VideoJob.user_id == user_id,
                                                      VideoJob.idempotency_key == key))


async def asset_status(asset_id: str | None) -> str | None:
    if not asset_id:
        return None
    async with get_db_session() as db:
        asset = await db.get(FileAsset, asset_id)
    return asset.status if asset else None


async def dispatched(submits: list, body, **capture):
    """Stand in for a provider adapter: the real ones submit inside the dispatch capture."""
    from agent.trajectory import capture_service_dispatch
    async with capture_service_dispatch(body=body, **capture):
        submits.append(body)


async def test_refused_video_submit_is_closed_instead_of_left_ambiguous(monkeypatch):
    from core.config import OpenBoxConfig, VideoGenerationConfig
    from tests.unit.test_video_submit_regression import legacy_entry, new_context, route
    from tool.media import video_production as vp
    from tool.media import video_providers as providers
    entry = legacy_entry()
    config = OpenBoxConfig(video_generation=VideoGenerationConfig(model=entry.id, models=[entry], dedupe=False))
    target = route()
    ctx = await new_context()
    submits = []

    async def no_selection(_ctx):
        return None

    async def submit(_target, path, body):
        await dispatched(submits, body, purpose="video_generation", provider=_target.provider,
                         model=_target.model, operation="POST " + path, profile="video_generation")
        return {"id": "task-1", "status": "queued"}
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(vp, "_configured_target", lambda _model: (target, config.video_generation))
    monkeypatch.setattr(vp, "_session_video_model_id", no_selection)
    monkeypatch.setattr(vp, "_session_video_resolution", no_selection)
    monkeypatch.setattr(providers, "submit", submit)

    with acting_as(revoked_run(ctx)), pytest.raises(runtime.RunRevoked):
        await vp.execute_generate(vp.VideoGenerateArgs(action="submit", model=entry.id, prompt="cat",
            resolution="720p", ratio="9:16", duration=4, idempotency_key="refused"), ctx)
    assert submits == []
    job = await job_for(ctx.user_id, "refused")
    assert job.status == "cancelled" and job.provider_task_id is None
    assert "ambiguous" not in "\n".join(vp._job_lines(job))
    assert await asset_status(job.output_asset_id) == "failed"
    # Nothing is left in flight to block the same request from a current run.
    assert await vp._in_flight_duplicate(job.prompt_hash, ctx, exclude_key="") is None


async def test_refused_composition_submit_does_not_stay_dispatching(monkeypatch):
    import tool.media.video_compose as vc
    from tests.unit.test_video_compose import FakeOss, _noop_false, _noop_none, _timeline, _user_with_asset
    submits = []

    async def submit(**kwargs):
        await dispatched(submits, kwargs, purpose="media_composition", provider="aliyun_ims", model="ims",
                         operation="SubmitMediaProducingJob", profile="media_composition",
                         capture_level="adapter_input")
        return "ims-1"
    monkeypatch.setattr("core.oss.get_oss", lambda: FakeOss())
    monkeypatch.setattr(vc.ims_client, "submit_media_producing_job", submit)
    monkeypatch.setattr("tool.media.video_production._attach_completed", _noop_false)
    monkeypatch.setattr("tool.media.video_production._try_materialize", _noop_none)
    ctx, asset_id = await _user_with_asset()

    with acting_as(revoked_run(ctx)), pytest.raises(runtime.RunRevoked):
        await vc.execute_compose(vc.VideoComposeArgs(action="submit", idempotency_key="refused",
                                                     timeline=_timeline(asset_id)), ctx)
    assert submits == []
    job = await job_for(ctx.user_id, "refused")
    assert job.status == "cancelled" and job.provider_task_id is None
    assert await asset_status(job.output_asset_id) == "failed"


async def test_refused_transcription_submit_does_not_stay_transcribing(monkeypatch):
    from tests.unit.test_video_compose import _user_with_asset
    from tool.media import video_production as vp
    target = vp.VideoTranscriptionTarget(engine="dashscope", model="fun-asr", api_key="test-only",
                                         base_url="https://dashscope.invalid", timeout_seconds=30,
                                         poll_interval_seconds=0.25, similarity_threshold=0.9)
    submits = []

    class Oss:
        def presign_get(self, key, expires_sec):
            return f"https://oss.invalid/{key}"

    async def transcribe(_target, audio_url):
        await dispatched(submits, {"model": _target.model, "input": {"file_urls": [audio_url]}},
                         purpose="audio_transcription", provider="dashscope", model=_target.model,
                         operation="POST /api/v1/services/audio/asr/transcription", profile="audio_transcription")
        return {"text": "never transcribed"}
    monkeypatch.setattr(vp, "_configured_transcription_target", lambda: target)
    monkeypatch.setattr("core.oss.get_oss", lambda: Oss())
    monkeypatch.setattr(vp, "_provider_transcribe", transcribe)
    ctx, asset_id = await _user_with_asset(mime="audio/mpeg")

    with acting_as(revoked_run(ctx)), pytest.raises(runtime.RunRevoked):
        await vp.execute_transcribe(vp.VideoTranscribeArgs(action="submit", asset_id=asset_id,
                                                           idempotency_key="refused"), ctx)
    assert submits == []
    job = await job_for(ctx.user_id, "refused")
    assert job.status == "cancelled" and job.provider_task_id is None
