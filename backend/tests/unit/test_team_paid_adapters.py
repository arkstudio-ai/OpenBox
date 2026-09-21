"""Exercise real adapter entry points with a refused team reservation.

Provider spies sit after the adapter's admission boundary. Local job and asset
records are real; no network requests or billable effects are performed.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from billing.media import MediaQuote
from core.config import OpenBoxConfig, VideoGenerationConfig
from db.base import get_db_session
from db.models.video_job import VideoJob
from team import media_limits, paid_tools
from team.errors import TeamError
from tests.unit.test_video_analyze import FakeSandbox, _ctx, env as analysis_env
from tests.unit.test_video_compose import _timeline, _user_with_asset, env as compose_env
from tests.unit.test_video_submit_regression import legacy_entry, new_context, route
from tool import image_gen as image, video_analyze as analysis, video_compose as compose
from tool import video_production as video, video_providers


@pytest.fixture(params=["PERMISSION_REQUIRES_USER", "INSUFFICIENT_CREDITS"])
def refused(request, monkeypatch):
    calls = []

    async def reserve(ctx, tool, price, **kwargs):
        calls.append((tool, kwargs))
        raise TeamError(request.param, "Test reservation refused before provider submission.")

    monkeypatch.setattr(paid_tools, "reserve", reserve)
    monkeypatch.setattr(paid_tools, "is_member", lambda: True)
    return request.param, calls


async def assert_refused_job(ctx, calls, tool):
    assert len(calls) == 1 and calls[0][0] == tool
    async with get_db_session() as db:
        job = await db.get(VideoJob, calls[0][1]["external_id"])
        assert job.user_id == ctx.user_id and job.session_id == ctx.session_id
        assert job.status == "failed" and job.provider_task_id is None
        assert job.request_data["_team_not_dispatched"] is True


async def test_image_never_reaches_provider_on_refusal(monkeypatch, refused):
    target = image.ProviderTarget("test", "gpt-image-2", "test-only", "https://image.invalid", 5)
    monkeypatch.setattr(image, "_configured_target", lambda: (target,
        SimpleNamespace(default_size="auto", default_quality="medium", output_format="png")))
    monkeypatch.setattr("core.oss.get_oss", object)
    monkeypatch.setattr(image, "_load_inputs", AsyncMock(return_value=([], None)))
    provider = AsyncMock(side_effect=AssertionError("Image provider must not be called"))
    monkeypatch.setattr(image, "_call_provider", provider)
    ctx = await new_context()
    ctx.part_id = "refused-image"
    with pytest.raises(TeamError) as error:
        await image.execute(image.ImageGenArgs(prompt="A test cube"), ctx)
    assert error.value.code == refused[0]
    assert len(refused[1]) == 1 and refused[1][0][0] == "image_gen"
    provider.assert_not_called()


async def test_generation_never_reaches_provider_on_refusal(monkeypatch, refused):
    entry = legacy_entry()
    config = OpenBoxConfig(video_generation=VideoGenerationConfig(model=entry.id, models=[entry], dedupe=False))
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(video, "_configured_target", lambda _: (route(), config.video_generation))
    monkeypatch.setattr(video, "_session_video_model_id", AsyncMock(return_value=None))
    monkeypatch.setattr(video, "_session_video_resolution", AsyncMock(return_value=None))
    provider = AsyncMock(side_effect=AssertionError("Video provider must not be called"))
    monkeypatch.setattr(video_providers, "submit", provider)
    monkeypatch.setattr(video, "_provider_submit", provider)
    ctx = await new_context()
    with pytest.raises(TeamError) as error:
        await video.execute_generate(video.VideoGenerateArgs(action="submit", model=entry.id,
            prompt="A test cube", resolution="720p", ratio="9:16", duration=4, idempotency_key="refused"), ctx)
    assert error.value.code == refused[0]
    provider.assert_not_called()
    await assert_refused_job(ctx, refused[1], "video_generate")


async def test_compose_never_reaches_provider_on_refusal(compose_env, refused):
    _, ims = compose_env
    ctx, asset = await _user_with_asset()
    with pytest.raises(TeamError) as error:
        await compose.execute_compose(compose.VideoComposeArgs(action="submit",
            idempotency_key="refused", timeline=_timeline(asset)), ctx)
    assert error.value.code == refused[0] and ims.submits == []
    await assert_refused_job(ctx, refused[1], "video_compose")


async def test_transcription_never_reaches_provider_on_refusal(monkeypatch, analysis_env, refused):
    ctx, asset = await _user_with_asset(mime="audio/mp3")
    monkeypatch.setattr(video, "_configured_transcription_target", lambda: SimpleNamespace(model="fun-asr"))
    price = MediaQuote("fun-asr", "stt", 1, Decimal("0.05"), {"version": "test"})
    monkeypatch.setattr(media_limits, "transcription_input", AsyncMock(return_value=("https://audio.invalid", price)))
    provider = AsyncMock(side_effect=AssertionError("Transcription provider must not be called"))
    monkeypatch.setattr(video, "_provider_transcribe", provider)
    with pytest.raises(TeamError) as error:
        await video.execute_transcribe(video.VideoTranscribeArgs(action="submit",
            asset_id=asset, idempotency_key="refused"), ctx)
    assert error.value.code == refused[0]
    provider.assert_not_called()
    await assert_refused_job(ctx, refused[1], "video_transcribe")


async def test_analysis_never_reaches_either_provider_on_refusal(monkeypatch, analysis_env, refused):
    ctx, asset = await _ctx(FakeSandbox())
    monkeypatch.setattr(video, "_configured_transcription_target", lambda: SimpleNamespace(model="fun-asr"))
    price = MediaQuote("vision", "analysis", 1, Decimal("1"), {"version": "test", "vision_rates": {}})
    monkeypatch.setattr(media_limits, "analysis_price", lambda *a, **kw: price)
    transcribe = AsyncMock(side_effect=AssertionError("Analysis STT must not be called"))
    vision = AsyncMock(side_effect=AssertionError("Analysis vision must not be called"))
    monkeypatch.setattr(analysis, "_transcribe", transcribe)
    monkeypatch.setattr(analysis, "_complete", vision)
    with pytest.raises(TeamError) as error:
        await analysis.execute(analysis.VideoAnalyzeArgs(source=asset), ctx)
    assert error.value.code == refused[0]
    transcribe.assert_not_called()
    vision.assert_not_called()
    await assert_refused_job(ctx, refused[1], "video_analyze")
