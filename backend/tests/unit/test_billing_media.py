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


@pytest.mark.parametrize("seconds,billed,credits,cost", [
    (9.5, 60, "0.045", "0.03"), (60, 60, "0.045", "0.03"), (60.5, 120, "0.09", "0.06"), (125, 180, "0.135", "0.09"),
])
def test_quote_is_per_second_billed_in_whole_minutes_at_a_50_percent_margin(seconds, billed, credits, cost):
    # 720p: IMS costs 0.03/min; sale 0.00075/s rounds to the minute like the cost, so every job keeps the margin.
    q = media.quote_compose(720, 1280, seconds)
    assert (q.tier, q.minutes_billed, q.credits, q.cost) == ("720p", billed, Decimal(credits), Decimal(cost))
    assert q.snapshot["seconds_billed"] == billed and q.snapshot["cost"]["basis"] == "aliyun-ims-list"


def test_legacy_per_minute_compose_entry_still_bills_by_the_minute():
    from billing.pricing import base_catalogue
    import copy
    data = copy.deepcopy(base_catalogue())
    entry = data["media"]["ims-compose-720p"]
    entry.pop("per_second"); entry["per_minute"] = "0.03"; entry["min_minutes"] = 1
    q = media.quote_compose(720, 1280, 60.5, rates=data)
    assert (q.minutes_billed, q.credits) == (2, Decimal("0.06")) and q.snapshot["minutes_billed"] == 2


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
    assert first == second == Decimal("0.045")
    events, ledger, balance = await _events(wid)
    assert len(events) == 1 and events[0].status == "shadow" and events[0].kind == "video_compose"
    assert events[0].tokens["seconds_billed"] == 60 and events[0].model_id == "ims-compose-720p"
    assert events[0].cost_credits == Decimal("0.03")
    assert ledger == [] and balance == Decimal("5")


async def test_enforce_posts_to_the_ledger_once(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    job, asset, wid = await _fixtures(balance=Decimal("1"))
    await media.settle_compose(job, asset, width=1080, height=1920, duration_sec=61)
    await media.settle_compose(job, asset, width=1080, height=1920, duration_sec=61)
    events, ledger, balance = await _events(wid)
    # 1080p: 61 s rounds to 120 s at 0.0015/s = 0.18 (cost 2 min × 0.06 = 0.12, the 50% margin held).
    assert events[0].status == "charged" and events[0].credits == Decimal("0.18") and events[0].cost_credits == Decimal("0.12")
    assert len(ledger) == 1 and ledger[0].amount == Decimal("-0.18") and balance == Decimal("0.82")


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


# ── video generation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,res,seconds,credits", [
    ("wan3.0-video", "720p", 5, "3.00"), ("wan3.0-video", "480p", 5, "1.50"), ("wan3.0-video", "1080p", 4.5, "6.00"),
    # Seedance 2.0 / Fast were raised to the TokenHub cost line on 2026-10-10.
    ("doubao-seedance-2-0-260128", "720p", 15, "15.00"), ("doubao-seedance-2-0-fast-260128", "720p", 5, "4.00"), ("MiniMax-H3", "768p", 10, "5.00"), ("video-sd-720p-proⅠ", "720p", 12, "6.00"),
])
def test_generation_quote_is_requested_seconds_times_tier_rate(model, res, seconds, credits):
    q = media.quote_generation(model, res, seconds)
    assert q.credits == Decimal(credits) and q.model_id == f"video-gen:{model}:{res}"
    assert q.minutes_billed == __import__("math").ceil(seconds)


def test_generation_quote_carries_the_channel_cost_beside_the_sale_price():
    # MiniMax-H3 via metaso: 0.09/s up to 15 s, doubled beyond; the sale price stays flat.
    short = media.quote_generation("MiniMax-H3", "768p", 10)
    assert (short.credits, short.cost) == (Decimal("5.00"), Decimal("0.90"))
    assert short.snapshot["cost"]["basis"] == "metaso" and short.snapshot["cost_credits"] == "0.900000000000"
    long = media.quote_generation("MiniMax-H3", "768p", 20)
    assert (long.credits, long.cost) == (Decimal("10.00"), Decimal("3.60"))
    assert long.snapshot["cost"]["multiplier"] == "2"
    # A model without a cost block is priced but has no cost (None, never 0).
    legacy = media.quote_generation("video-sd-720p-proⅠ", "720p", 12)
    assert legacy.credits == Decimal("6.00") and legacy.cost is None and "cost" not in legacy.snapshot


def test_generation_quote_is_unpriced_for_unknown_model_tier_or_smart_duration():
    assert media.quote_generation("doubao-seedance-2-0-unpriced-000000", "720p", 5).credits is None
    assert media.quote_generation("wan3.0-video", "2k", 5).credits is None
    assert media.quote_generation("wan3.0-video", "720p", None).credits is None
    assert media.quote_generation("wan3.0-video", "720p", -1).credits is None
    assert media.quote_generation("bossip/wan3.0-video", "720p", 5).credits == Decimal("3.00")  # provider prefix stripped


async def test_generation_settles_once_in_shadow_and_records_seconds(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    job, asset, wid = await _fixtures()
    first = await media.settle_generation(job, asset, model_id="wan3.0-video", resolution="720p", duration_sec=5)
    again = await media.settle_generation(job, asset, model_id="wan3.0-video", resolution="720p", duration_sec=5)
    assert first == again == Decimal("3.00")
    events, ledger, balance = await _events(wid)
    assert len(events) == 1 and events[0].kind == "video_generate" and events[0].status == "shadow"
    assert events[0].tokens["seconds_billed"] == 5 and events[0].tokens["resolution"] == "720p"
    assert ledger == [] and balance == Decimal("5")


async def test_generation_enforce_charges_and_unpriced_tier_is_recorded_not_charged(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    job, asset, wid = await _fixtures(balance=Decimal("10"))
    await media.settle_generation(job, asset, model_id="wan3.0-video", resolution="720p", duration_sec=5)
    events, ledger, balance = await _events(wid)
    assert events[0].status == "charged" and ledger[0].amount == Decimal("-3.00") and balance == Decimal("7.00")
    job2, asset2, wid2 = await _fixtures(balance=Decimal("10"))
    assert await media.settle_generation(job2, asset2, model_id="doubao-seedance-2-0-unpriced-000000", resolution="720p", duration_sec=5) is None
    events, ledger, balance = await _events(wid2)
    assert events[0].status == "unpriced" and ledger == [] and balance == Decimal("10")


# ── images and transcription ─────────────────────────────────────────────────

def test_image_quote_uses_model_price_or_default_per_image():
    assert media.quote_image("gpt-image-2", 1).credits == Decimal("0.30")
    q = media.quote_image("some-new-image-model", 3)
    assert q.credits == Decimal("0.90") and q.snapshot["priced_as"] == "default" and q.minutes_billed == 3
    assert media.quote_image("gpt-image-2", 0).credits is None


def test_transcription_quote_rounds_up_minutes_and_is_unpriced_for_unknown_engine():
    assert media.quote_transcription("fun-asr", 6.123).credits == Decimal("0.05")
    assert media.quote_transcription("fun-asr", 61).credits == Decimal("0.10")
    assert media.quote_transcription("whisper-1", 10).credits is None
    assert media.quote_transcription("fun-asr", None).credits is None


async def test_image_settlement_is_idempotent_per_call_key(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    job, asset, wid = await _fixtures()
    first = await media.settle_image(key="image:part_1", workspace_id=wid, user_id=job.user_id, session_id=None, model_id="gpt-image-2", count=2)
    again = await media.settle_image(key="image:part_1", workspace_id=wid, user_id=job.user_id, session_id=None, model_id="gpt-image-2", count=2)
    assert first == again == Decimal("0.60")
    events, ledger, _ = await _events(wid)
    assert len(events) == 1 and events[0].kind == "image_gen" and events[0].tokens["images"] == 2


async def test_transcription_settles_in_enforce_and_unreported_without_duration(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    job, asset, wid = await _fixtures(balance=Decimal("1"))
    assert await media.settle_transcription(job, workspace_id=wid, model_id="fun-asr", duration_sec=6.123) == Decimal("0.05")
    events, ledger, balance = await _events(wid)
    assert events[0].kind == "video_transcribe" and events[0].status == "charged" and balance == Decimal("0.95")
    job2, _, wid2 = await _fixtures()
    assert await media.settle_transcription(job2, workspace_id=wid2, model_id="fun-asr", duration_sec=None) is None
    events, _, _ = await _events(wid2)
    assert events[0].status == "unreported"


async def test_settle_without_workspace_records_nothing(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    price = media.quote_image("gpt-image-2", 1)
    assert await media.settle(key="image:x", workspace_id="", user_id="u", session_id=None, price=price,
                              kind="image_gen", quantity_known=True, tokens={}, default_title="x") is None


def test_billing_status_lines_only_claim_a_deduction_in_enforce(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    shadow = media.billing_status_lines()
    assert shadow[0] == "billing_mode=shadow" and "未实际扣减" in shadow[1]
    monkeypatch.setenv("BILLING_MODE", "enforce")
    assert media.billing_status_lines() == ["billing_mode=enforce"]


@pytest.mark.parametrize('resolution,seconds,amount', [
    ('480p', 5, '1.10'), ('480p', 15, '3.30'), ('768p', 5, '1.70'), ('768p', 15, '5.10'),
])
def test_runninghub_quote_matches_live_official_price_preview(resolution, seconds, amount):
    price = media.quote_generation('MiniMax-H3-Max-Turbo', resolution, seconds)
    assert price.credits == Decimal(amount)
    assert price.snapshot['verified_at'] == '2026-09-24'


@pytest.mark.parametrize('mode', ['shadow', 'enforce'])
async def test_runninghub_saved_quote_survives_rate_changes_and_charges_once(monkeypatch, mode):
    monkeypatch.setenv('BILLING_MODE', mode)
    job, asset, wid = await _fixtures()
    price = media.quote_generation('MiniMax-H3-Max-Turbo', '768p', 5)
    job.request_data['billing_quote'] = {
        'model_id': price.model_id, 'tier': price.tier, 'quantity': price.minutes_billed,
        'credits': str(price.credits), 'snapshot': price.snapshot,
    }
    # Even removing the model from a newer catalogue must not lose its quote.
    monkeypatch.setattr(media, 'catalogue', lambda: {'version': 'changed', 'verified_at': 'later', 'media': {}})
    for _ in range(2):
        assert await media.settle_generation(job, asset, model_id='MiniMax-H3-Max-Turbo',
                                             resolution='768p', duration_sec=5) == Decimal('1.70')
    events, ledger, balance = await _events(wid)
    assert len(events) == 1 and events[0].tokens['seconds_billed'] == 5
    assert len(ledger) == (1 if mode == 'enforce' else 0)
    assert balance == Decimal('3.30' if mode == 'enforce' else '5')


async def test_invalid_saved_quote_cannot_charge_or_fall_back_to_current_price(monkeypatch):
    monkeypatch.setenv('BILLING_MODE', 'enforce')
    job, asset, wid = await _fixtures()
    price = media.quote_generation('MiniMax-H3-Max-Turbo', '768p', 5)
    job.request_data['billing_quote'] = {
        'model_id': price.model_id, 'tier': price.tier, 'quantity': 5,
        'credits': '2.70', 'snapshot': price.snapshot,
    }
    with pytest.raises(ValueError, match='stored video generation quote'):
        await media.settle_generation(job, asset, model_id='MiniMax-H3-Max-Turbo', resolution='768p', duration_sec=5)
    events, ledger, balance = await _events(wid)
    assert events == [] and ledger == [] and balance == Decimal('5')


async def test_runninghub_without_delivered_asset_is_not_charged(monkeypatch):
    monkeypatch.setenv('BILLING_MODE', 'enforce')
    job, asset, wid = await _fixtures()
    assert await media.settle_generation(job, None, model_id='MiniMax-H3-Max-Turbo', resolution='768p', duration_sec=5) is None
    events, ledger, balance = await _events(wid)
    assert events == [] and ledger == [] and balance == Decimal('5')


# ── voice calls ──────────────────────────────────────────────────────────────

def _call_snapshot():
    from voice.meter import CallMeter
    meter = CallMeter()
    meter.settle("r1", "completed", {
        "input_tokens": 12000, "output_tokens": 600,
        "input_tokens_details": {"text_tokens": 10000, "audio_tokens": 2000},
        "output_tokens_details": {"text_tokens": 100, "audio_tokens": 500}})
    meter.finish()
    return meter.snapshot()


def test_a_voice_call_is_priced_by_modality_from_the_catalogue():
    snapshot = _call_snapshot()
    q = media.quote_voice_call("qwen3.8-omni-flash-realtime", snapshot, 95)
    # 10000×1.5 + 2000×6 + 100×4.5 + 500×12 per million yuan; 1 credit = 1 yuan.
    assert q.credits == Decimal("0.03345") and q.minutes_billed == 2
    assert q.snapshot["per_million"] == {"input_text": "1.5", "input_audio": "6", "output_text": "4.5",
                                         "output_audio": "12"}
    assert q.snapshot["source"].startswith("https://help.aliyun.com/")
    assert Decimal(snapshot["total_yuan"]) == q.credits  # what the call showed is what it is charged
    assert media.quote_voice_call("some-other-omni", snapshot, 95).credits is None


async def test_a_voice_call_settles_once_as_its_own_usage_row(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    job, asset, wid = await _fixtures(balance=Decimal("1"))
    args = dict(call_id="call-1", workspace_id=wid, user_id=job.user_id, session_id=None,
                model_id="qwen3.8-omni-flash-realtime", snapshot=_call_snapshot(), duration_sec=95)
    assert await media.settle_voice_call(**args) == await media.settle_voice_call(**args) == Decimal("0.03345")
    events, ledger, balance = await _events(wid)
    assert [(e.kind, e.status, e.session_title, e.tokens["input_audio"]) for e in events] == [
        ("voice_call", "charged", "语音通话", 2000)]
    assert [entry.amount for entry in ledger] == [Decimal("-0.03345")] and balance == Decimal("0.96655")


async def test_a_silent_call_costs_nothing(monkeypatch):
    from voice.meter import CallMeter
    monkeypatch.setenv("BILLING_MODE", "enforce")
    job, asset, wid = await _fixtures()
    assert await media.settle_voice_call(call_id="call-0", workspace_id=wid, user_id=job.user_id, session_id=None,
                                         model_id="qwen3.8-omni-flash-realtime", snapshot=CallMeter().snapshot(),
                                         duration_sec=0) is None
    assert (await _events(wid))[0] == []


async def test_a_call_may_spend_the_balance_only_in_enforce(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    job, asset, wid = await _fixtures(balance=Decimal("-100"))
    assert await media.voice_credit_room(wid) is None  # shadow never refuses or caps
    monkeypatch.setenv("BILLING_MODE", "enforce")
    with pytest.raises(BillingError) as info:
        await media.voice_credit_room(wid)  # the free allowance does not cover a deep debt
    assert info.value.code == "INSUFFICIENT_CREDITS"
    job, asset, rich = await _fixtures(balance=Decimal("5"))
    assert await media.voice_credit_room(rich) >= Decimal("5")
