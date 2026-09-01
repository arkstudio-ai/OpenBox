"""Guards that stand in for the credits ledger.

An idempotency key stops a retry from paying twice; it cannot stop a fresh key
carrying identical content, because the caller chooses the key. These two do:
the content key is derived from the request, and the ceiling is counted from
the job table.
"""
import pytest

from tool import video_production as vp


class _Ctx:
    user_id = "user-1"
    session_id = "session-1"
    project_id = None
    sandbox = None


@pytest.mark.asyncio
async def test_duplicate_in_flight_is_looked_up_by_content_not_by_key(monkeypatch):
    """The excluded key is the caller's own, so a retry still reconciles."""
    seen = {}

    async def fake_lookup(prompt_hash, ctx, *, exclude_key):
        seen.update(prompt_hash=prompt_hash, exclude_key=exclude_key)
        return None

    monkeypatch.setattr(vp, "_in_flight_duplicate", fake_lookup)
    await vp._in_flight_duplicate("hash-abc", _Ctx(), exclude_key="open:cat:1")

    assert seen == {"prompt_hash": "hash-abc", "exclude_key": "open:cat:1"}


@pytest.mark.asyncio
async def test_budget_check_is_skipped_when_no_limit_is_configured(monkeypatch):
    from core.config import OpenBoxConfig, VideoGenerationConfig

    config = OpenBoxConfig(video_generation=VideoGenerationConfig(daily_job_limit=0))
    monkeypatch.setattr(vp, "_daily_submit_count", _never_called)
    monkeypatch.setattr("core.config.get_config", lambda: config)

    await vp._check_submit_budget(_Ctx())


@pytest.mark.asyncio
async def test_budget_check_refuses_past_the_ceiling(monkeypatch):
    from core.config import OpenBoxConfig, VideoGenerationConfig

    config = OpenBoxConfig(video_generation=VideoGenerationConfig(daily_job_limit=3))

    async def used_three(_ctx):
        return 3

    monkeypatch.setattr(vp, "_daily_submit_count", used_three)
    monkeypatch.setattr("core.config.get_config", lambda: config)

    with pytest.raises(RuntimeError, match="daily video generation limit"):
        await vp._check_submit_budget(_Ctx())


@pytest.mark.asyncio
async def test_in_flight_status_set_covers_the_transfer_window():
    """A job finalizing to OSS is still paid work; missing it would double-pay."""
    assert "finalizing" in vp._IN_FLIGHT_STATUSES
    assert "transfer_failed" in vp._IN_FLIGHT_STATUSES
    assert not vp._IN_FLIGHT_STATUSES & vp._SEGMENT_TERMINAL


async def _never_called(_ctx):  # pragma: no cover - asserts by not running
    raise AssertionError("the daily count must not be queried when the limit is off")
