"""Composition credits: quoted up front, settled once on success, free on failure."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from billing import media
from billing.service import BillingError


def test_tier_is_decided_by_the_short_side():
    assert media.compose_tier(720, 1280)[0] == "ims-compose-720p"
    assert media.compose_tier(1280, 720)[0] == "ims-compose-720p"
    assert media.compose_tier(1080, 1920)[0] == "ims-compose-1080p"
    assert media.compose_tier(480, 854)[0] == "ims-compose-480p"
    assert media.compose_tier(2160, 3840)[0] == "ims-compose-4k"
    assert media.compose_tier(4320, 7680) is None


@pytest.mark.parametrize("seconds,minutes,credits", [
    (9.5, 1, "0.03"), (60, 1, "0.03"), (60.5, 2, "0.06"), (125, 3, "0.09"),
])
def test_quote_rounds_up_to_whole_minutes_with_a_one_minute_floor(seconds, minutes, credits):
    q = media.quote_compose(720, 1280, seconds)
    assert (q.tier, q.minutes_billed, q.credits) == ("720p", minutes, Decimal(credits))
    assert q.snapshot["source"].startswith("https://help.aliyun.com/")


def test_quote_without_duration_or_price_is_explicitly_unpriced():
    assert media.quote_compose(720, 1280, None).credits is None
    assert media.quote_compose(4320, 7680, 10).credits is None


async def _fixtures(*, balance=Decimal("5")):
    from db.base import get_db_session
    from db.models.billing import CreditBalance
    from db.models.file_asset import FileAsset

    uid, wid = "u_" + uuid.uuid4().hex[:8], "w_" + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    asset = FileAsset(id="asset_" + uuid.uuid4().hex[:8], user_id=uid, workspace_id=wid, session_id=None, project_id=None,
                      name="final.mp4", oss_key=f"assets/{uid}/x/final.mp4", mime="video/mp4", size=1, status="ready",
                      source="agent", transient=False, created_at=now)
    async with get_db_session() as db:
        db.add(CreditBalance(workspace_id=wid, balance=balance, updated_at=now))
        db.add(asset)
    job = SimpleNamespace(id="video_" + uuid.uuid4().hex[:8], user_id=uid, session_id=None, request_data={})
    return job, asset, wid


async def _events(wid):
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.billing import CreditBalance, CreditLedger, UsageEvent
    async with get_db_session() as db:
        events = (await db.execute(select(UsageEvent).where(UsageEvent.workspace_id == wid))).scalars().all()
        ledger = (await db.execute(select(CreditLedger).where(CreditLedger.workspace_id == wid))).scalars().all()
        balance = await db.get(CreditBalance, wid)
    return events, ledger, balance.balance


async def test_shadow_records_without_charging_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    job, asset, wid = await _fixtures()
    first = await media.settle_compose(job, asset, width=720, height=1280, duration_sec=9.5)
    second = await media.settle_compose(job, asset, width=720, height=1280, duration_sec=9.5)
    assert first == second == Decimal("0.03")
    events, ledger, balance = await _events(wid)
    assert len(events) == 1 and events[0].status == "shadow" and events[0].kind == "video_compose"
    assert events[0].tokens["minutes_billed"] == 1 and events[0].model_id == "ims-compose-720p"
    assert ledger == [] and balance == Decimal("5")


async def test_enforce_posts_to_the_ledger_once(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    job, asset, wid = await _fixtures(balance=Decimal("1"))
    await media.settle_compose(job, asset, width=1080, height=1920, duration_sec=61)
    await media.settle_compose(job, asset, width=1080, height=1920, duration_sec=61)
    events, ledger, balance = await _events(wid)
    assert events[0].status == "charged" and events[0].credits == Decimal("0.12")
    assert len(ledger) == 1 and ledger[0].amount == Decimal("-0.12") and balance == Decimal("0.88")


async def test_missing_duration_is_unreported_not_free(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    job, asset, wid = await _fixtures()
    assert await media.settle_compose(job, asset, width=720, height=1280, duration_sec=None) is None
    events, ledger, _ = await _events(wid)
    assert events[0].status == "unreported" and ledger == []


async def test_off_mode_records_nothing(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "off")
    job, asset, wid = await _fixtures()
    assert await media.settle_compose(job, asset, width=720, height=1280, duration_sec=9.5) is None
    events, _, _ = await _events(wid)
    assert events == []


async def test_precheck_only_bites_in_enforce(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    await media.precheck_compose("no-such-session")  # shadow never refuses
    monkeypatch.setenv("BILLING_MODE", "enforce")
    with pytest.raises(BillingError) as info:
        await media.precheck_compose("no-such-session")
    assert info.value.code == "BILLING_SESSION_REQUIRED"
