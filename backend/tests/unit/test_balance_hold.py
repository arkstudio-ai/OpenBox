"""Paid submits are admitted against balance minus what is already in flight, one at a time per workspace.

Regression for 11-11: a workspace with 252.8 credits submitted seven ~72-credit
Seedance shots in the same second; the old precheck only asked ``balance > 0``,
all seven passed, and the balance settled at -254.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from billing import media
from billing.service import BillingError, lock_balance
from billing.subscriptions import ensure_period_allowance
from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import CreditBalance, UsageEvent
from db.models.video_job import VideoJob
from tests.unit.test_v1_routes import default_config, seed_scope  # noqa: F401


async def _session(balance: str) -> tuple[str, str, str]:
    from session import session as session_mod

    uid, wid = await seed_scope()
    created = await session_mod.create_session(model="openai/gemini-3.8-flash", agent="build", title="hold",
                                               user_id=uid, workspace_id=wid)
    async with get_db_session() as db:
        row = await lock_balance(db, wid)
        await ensure_period_allowance(db, row)
        row.balance = Decimal(balance)
    return created.id, uid, wid


async def _job(session_id: str, uid: str, hold: str | None, *, kind: str = "segment", status: str = "submitting",
               age: timedelta = timedelta(0), key: str = "balance_hold") -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None) - age
    request_data = {}
    if hold is not None:
        request_data = ({"balance_hold": hold} if key == "balance_hold"
                        else {"billing_quote": {"credits": hold, "snapshot": {}}})
    async with get_db_session() as db:
        job = VideoJob(id=ascending("video"), user_id=uid, session_id=session_id, kind=kind, idempotency_key=uuid4().hex,
                       request_hash="h", status=status, model="m", request_data=request_data, result_data={},
                       attempt=0, created_at=now, updated_at=now)
        db.add(job)
        return job.id


async def _admit(session_id: str, uid: str, credits: str) -> str:
    async with media.hold_for_submit(session_id, credits=Decimal(credits), step="视频生成"):
        await asyncio.sleep(0)  # let a concurrent submit try to slip in before the row exists
        return await _job(session_id, uid, credits)


async def test_concurrent_shots_stop_at_the_balance(monkeypatch, default_config):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    sid, uid, _ = await _session("252.8")
    results = await asyncio.gather(*(_admit(sid, uid, "72.15") for _ in range(7)), return_exceptions=True)
    admitted = [r for r in results if isinstance(r, str)]
    refused = [r for r in results if isinstance(r, BillingError)]
    assert len(admitted) == 3 and len(refused) == 4, results
    assert all(r.code == "INSUFFICIENT_CREDITS" for r in refused)
    assert "进行中的任务还将扣 216.45" in str(refused[0])


async def test_settled_failed_finished_and_stale_jobs_hold_nothing(monkeypatch, default_config):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    sid, uid, wid = await _session("100")
    await _job(sid, uid, "60", status="failed")
    await _job(sid, uid, "60", status="completed")
    await _job(sid, uid, "60", age=timedelta(hours=25))
    settled = await _job(sid, uid, "60", status="in_progress")
    async with get_db_session() as db:
        db.add(UsageEvent(id=ascending("usage"), idempotency_key=f"generate:{settled}", workspace_id=wid, user_id=uid,
                          session_id=sid, message_id=None, session_title="t", model_id="m", kind="video_generate",
                          tokens={}, total_tokens=0, credits=Decimal("60"), status="charged", pricing={},
                          created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    async with media.hold_for_submit(sid, credits=Decimal("100"), step="视频生成"):
        pass
    # A compose quote (billing_quote) counts like a generation hold; the job being priced is left out.
    render = await _job(sid, uid, "30", kind="render", status="dispatching", key="billing_quote")
    async with media.hold_for_submit(sid, credits=Decimal("80"), step="视频合成", exclude_job_id=render):
        pass
    with pytest.raises(BillingError):
        async with media.hold_for_submit(sid, credits=Decimal("81"), step="视频生成"):
            pass


async def test_only_enforce_refuses_and_resubmits_are_not_charged_again(monkeypatch, default_config):
    sid, uid, _ = await _session("10")
    await _job(sid, uid, "50")
    monkeypatch.setenv("BILLING_MODE", "shadow")
    async with media.hold_for_submit(sid, credits=Decimal("50"), step="视频生成"):
        pass
    monkeypatch.setenv("BILLING_MODE", "enforce")
    async with media.hold_for_submit(sid, credits=Decimal("50"), step="视频生成", already_admitted=True):
        pass
    with pytest.raises(BillingError):
        async with media.hold_for_submit(sid, credits=Decimal("5"), step="视频生成"):
            pass
    # Unknown price keeps the old rule: a positive balance is enough.
    async with get_db_session() as db:
        (await db.get(CreditBalance, (await _wid(sid)))).balance = Decimal("60")
    async with media.hold_for_submit(sid, credits=None, step="视频生成"):
        pass


@pytest.mark.parametrize(("balance", "available", "shortfall"), [
    ("3.07", "2.07", "5.63"),
    ("0", "0", "7.70"),
])
async def test_fast_video_reports_credit_shortfall_before_creating_a_job(
    monkeypatch, default_config, balance, available, shortfall,
):
    from tool.tool import ToolContext
    from tool import video_production as video

    monkeypatch.setenv("BILLING_MODE", "enforce")
    sid, uid, wid = await _session(balance)
    await _job(sid, uid, "1", status="in_progress")
    target = SimpleNamespace(model="ComfyUI-MiniMax-H3", channel="comfyui", wire_format="comfyui_h3")
    settings = SimpleNamespace(provider_input_url_ttl_seconds=60, dedupe=False)
    monkeypatch.setattr(video, "_configured_target", lambda _: (target, settings))
    monkeypatch.setattr(video, "_resolve_open_submission", AsyncMock(return_value={
        "model": target.model, "prompt": "A quiet garden", "resolution": "768p", "ratio": "9:16",
        "duration": 10, "generate_audio": True, "watermark": False, "input_assets": [], "shot": 2,
    }))
    monkeypatch.setattr(video, "_prepare_provider_inputs", AsyncMock(return_value={}))
    create_job = AsyncMock()
    monkeypatch.setattr(video, "_create_pending_job", create_job)
    ctx = ToolContext(user_id=uid, workspace_id=wid, session_id=sid)

    result = await video.execute_generate(video.VideoGenerateArgs(
        action="submit", prompt="A quiet garden", idempotency_key="fast-credit-test",
    ), ctx)

    assert result.title == "积分不足，视频未提交", result.output
    assert result.metadata["failure_code"] == "INSUFFICIENT_CREDITS"
    assert result.metadata["submission_outcome"] == "not_sent"
    assert result.metadata["do_not_resubmit"] is True
    assert result.metadata["shot"] == 2
    assert Decimal(result.metadata["required_credits"]) == Decimal("7.70")
    assert Decimal(result.metadata["available_credits"]) == Decimal(available)
    assert Decimal(result.metadata["shortfall_credits"]) == Decimal(shortfall)
    assert f"还差 {shortfall} 积分" in result.output
    create_job.assert_not_called()


async def _wid(session_id: str) -> str:
    from db.models.session import Session

    async with get_db_session() as db:
        return (await db.get(Session, session_id)).workspace_id
