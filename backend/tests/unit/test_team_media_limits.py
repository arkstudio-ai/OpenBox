from decimal import Decimal
from types import SimpleNamespace

import pytest

from team import media_limits, paid_tools
from team.errors import TeamError
from tool.tool import ToolContext


@pytest.mark.parametrize("duration", ["", "nan", "inf", "-1", "601"])
async def test_unverifiable_or_oversized_audio_never_reaches_paid_input_staging(duration, monkeypatch):
    from core import config
    monkeypatch.setattr(paid_tools, "is_member", lambda: True)
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(team_max_media_seconds=600))
    commands = []
    async def execute(command, **_):
        commands.append(command)
        return SimpleNamespace(stdout=duration, exit_code=0)
    ctx = ToolContext(sandbox=SimpleNamespace(execute=execute))
    with pytest.raises(TeamError) as error:
        await media_limits.transcription_input(ctx, SimpleNamespace(id="job"), SimpleNamespace(model="fun-asr"), "https://owned.invalid/audio", object())
    assert error.value.code == "PERMISSION_REQUIRES_USER"
    assert len(commands) == 1 and commands[0].startswith("ffprobe")


async def test_audio_sent_to_provider_is_bounded_copy_with_rounding_headroom(monkeypatch):
    from core import config
    from tool import video_analyze
    monkeypatch.setattr(paid_tools, "is_member", lambda: True)
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(team_max_media_seconds=600))
    commands = []
    async def execute(command, **_):
        commands.append(command)
        return SimpleNamespace(stdout="59.9" if command.startswith("ffprobe") else "", exit_code=0)
    async def stage(*_, **kwargs):
        assert kwargs["frame_files"] == [] and kwargs["audio"]
        return {"audio_url": "https://owned.invalid/bounded-copy"}
    monkeypatch.setattr(video_analyze, "_stage", stage)
    seen = []
    monkeypatch.setattr(media_limits, "quote_transcription", lambda model, seconds: seen.append((model, seconds)) or SimpleNamespace(credits=Decimal("1")))
    ctx = ToolContext(sandbox=SimpleNamespace(execute=execute))
    url, price = await media_limits.transcription_input(ctx, SimpleNamespace(id="job"), SimpleNamespace(model="fun-asr"), "https://owned.invalid/original", object())
    assert url.endswith("bounded-copy") and price.credits == 1
    assert seen == [("fun-asr", 62)] and "-t 60 " in commands[1]


def test_analysis_requires_explicit_model_limit_and_includes_peak_cache_and_audio(monkeypatch):
    from billing import pricing
    from core import config
    configured = SimpleNamespace(id="fixture/vision", context_limit=None)
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(models=[configured]))
    rates = {"version": "test-v1", "verified_at": "2026-09-21", "credits_per_cny": "1", "usd_cny": "7",
        "sources": {"fixture": "https://pricing.invalid"},
        "models": {"vision": {"vendor": "fixture", "currency": "CNY", "input": "1", "output": "2", "cache_write": "3", "peak_multiplier": "2"}},
        "media": {"stt": {"asr": {"currency": "CNY", "per_minute": "0.5", "min_minutes": 1}}}}
    monkeypatch.setattr(pricing, "catalogue", lambda: rates)
    with pytest.raises(TeamError, match="context_limit"):
        media_limits.analysis_price("fixture/vision", transcribe_model=None, max_audio_seconds=60)
    configured.context_limit = 10000
    bound = media_limits.analysis_price("fixture/vision", transcribe_model="asr", max_audio_seconds=60)
    assert bound.credits == Decimal("1.068")  # 2 STT minutes + worst input cache tier and peak output
    assert bound.snapshot["vision_rates"]["models"] == rates["models"]
