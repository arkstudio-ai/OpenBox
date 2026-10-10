"""Credits for media jobs billed by output duration, starting with IMS composition.

LLM calls are metered per token by ``UsageMeter``; a composition is one job
with one price, known up front from duration and output size. Three moments:

- ``quote_compose`` — before anything is submitted: what the cut will cost, so
  the person can be shown the number (the video skill's confirmation card).
- ``precheck_compose`` — in ``enforce`` mode, refuse a submit when the
  workspace has no credits, the same rule ``UsageMeter.start`` applies to LLMs.
- ``settle_compose`` — when IMS reports Success: one ``UsageEvent`` per job
  (idempotent on the job id), posted to the ledger in ``enforce``, recorded
  only in ``shadow``. A failed composition costs nothing, matching IMS.

Prices live in ``rates.json`` under ``media`` so a price change is a data
change with a ``source`` next to it, like every model rate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from billing.pricing import PRECISION, catalogue, cost_fx, cost_snapshot
from billing.service import BillingError, billing_mode, lock_balance, post_ledger

USAGE_KIND = "video_compose"


def billing_status_lines() -> list[str]:
    """Lines that tell the model whether a ``credits=`` figure was really deducted.

    Outside ``enforce`` the number is a metered estimate; without this the
    model reports "已扣 0.03 积分" while the balance never moves.
    """
    mode = billing_mode()
    if mode == "enforce":
        return ["billing_mode=enforce"]
    return [f"billing_mode={mode}",
            "billing_note=当前为影子计费：credits 只是统计值，积分未实际扣减，向用户汇报时说“统计消耗”，不要说“已扣积分”"]
GENERATION_KIND = "video_generate"
VOICE_KIND = "voice_call"
IMAGE_KIND = "image_gen"
STT_KIND = "video_transcribe"
TRENDS_KIND = "hot_trends"


@dataclass(frozen=True)
class MediaQuote:
    model_id: str            # rates.json media key, e.g. ims-compose-720p
    tier: str                # 720p
    minutes_billed: int
    credits: Decimal | None  # None = no verified price for this output size
    snapshot: dict[str, Any]
    cost: Decimal | None = None  # what it cost us at the recorded basis; None = no basis


def _unit_cost(entry: dict | None, unit: str, quantity: Decimal | int, data: dict, *,
               resolution: str | None = None, seconds: float | None = None) -> tuple[Decimal | None, dict | None]:
    """(cost credits, cost snapshot) for ``quantity`` units of an entry's ``cost`` block.

    Video costs are per resolution and may carry ``duration_bands`` (a vendor
    whose per-second price doubles past 15 s); other media are one price.
    """
    cost = (entry or {}).get("cost")
    if not isinstance(cost, dict):
        return None, None
    price = cost.get(unit)
    rule_id = cost.get("rule_id")
    if unit == "per_second":
        price = (price or {}).get(resolution) if isinstance(price, dict) else None
        rule_id = (cost.get("rule_ids") or {}).get(resolution)
    if price is None:
        return None, None
    per = Decimal(str(price))
    if not per.is_finite() or per < 0:
        raise ValueError("Invalid media cost")
    multiplier = Decimal(1)
    if seconds is not None:
        for band in cost.get("duration_bands") or []:
            if seconds > int(band["above_seconds"]):
                multiplier = max(multiplier, Decimal(str(band["multiplier"])))
    fx = cost_fx(data, str(cost.get("currency", "CNY")).upper())
    total = (per * multiplier * fx * Decimal(quantity)).quantize(PRECISION)
    extra = {unit: str(per), "multiplier": str(multiplier)} if multiplier != 1 else {unit: str(per)}
    if rule_id:
        extra["rule_id"] = rule_id
    return total, cost_snapshot(cost, extra=extra)


def _with_cost(quote: MediaQuote, cost: Decimal | None, snapshot: dict | None, rule_id: str | None = None) -> MediaQuote:
    extra: dict[str, Any] = {}
    if rule_id:
        extra["rule_id"] = rule_id
    if cost is not None:
        extra["cost"] = snapshot
        extra["cost_credits"] = str(cost)
    if not extra:
        return quote
    return MediaQuote(quote.model_id, quote.tier, quote.minutes_billed, quote.credits, {**quote.snapshot, **extra}, cost)


def compose_tier(width: int, height: int, rates: dict | None = None) -> tuple[str, dict] | None:
    """IMS bills by the output's resolution class; the short side decides it
    (720x1280 vertical is 720P). Returns (media key, rate) or None if unpriced."""
    media = (rates if rates is not None else catalogue()).get("media", {})
    short = min(width, height)
    candidates = [
        (key, rate) for key, rate in media.items()
        if key.startswith("ims-compose-") and isinstance(rate, dict) and short <= int(rate["short_side_max"])
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda kv: int(kv[1]["short_side_max"]))


def quote_compose(width: int, height: int, duration_sec: float | None, *, rates: dict | None = None) -> MediaQuote:
    """Credits for one composition: per second of output at the tier's price, billed in whole minutes.

    IMS charges per output minute (不足 1 分钟按 1 分钟计); the sale price is
    per second but rounds the same way (``min_seconds`` / ``round_seconds``,
    both 60 by default) so a 61-second cut never slips under its two-minute
    cost. A legacy ``per_minute`` entry (an older ``BILLING_RATES_FILE``)
    still bills by the minute.
    """
    data = rates if rates is not None else catalogue()
    picked = compose_tier(width, height, data)
    base = {"version": data["version"], "verified_at": data["verified_at"], "kind": USAGE_KIND}
    if picked is None:
        return MediaQuote("ims-compose", "unpriced", 0, None, {**base, "reason": "No verified price for this output size"})
    key, rate = picked
    tier = key.rsplit("-", 1)[-1]
    if duration_sec is None:
        return MediaQuote(key, tier, 0, None, {**base, "model": key, "reason": "Duration unknown until the shots are trimmed"})
    seconds = math.ceil(float(duration_sec) - 1e-9)
    cost_minutes = max(int((rate.get("cost") or {}).get("min_minutes", 1)), math.ceil(float(duration_sec) / 60 - 1e-9))
    cost, cost_snap = _unit_cost(rate, "per_minute", cost_minutes, data)
    if rate.get("per_second") is not None:
        per_second = Decimal(str(rate["per_second"]))
        if not per_second.is_finite() or per_second < 0:
            raise ValueError("Invalid media rate")
        step = max(1, int(rate.get("round_seconds", 1)))
        billed = max(int(rate.get("min_seconds", 1)), math.ceil(seconds / step) * step)
        credits = (per_second * billed).quantize(PRECISION)
        return _with_cost(MediaQuote(key, tier, billed, credits, {
            **base, "model": key, "currency": rate["currency"], "per_second": str(per_second),
            "min_seconds": int(rate.get("min_seconds", 1)), "round_seconds": step,
            "seconds_billed": billed, "duration_sec": float(duration_sec), "source": rate.get("source"),
        }), cost, cost_snap, rate.get("rule_id"))
    minutes = max(int(rate.get("min_minutes", 1)), math.ceil(float(duration_sec) / 60 - 1e-9))
    per_minute = Decimal(str(rate["per_minute"]))
    if not per_minute.is_finite() or per_minute < 0:
        raise ValueError("Invalid media rate")
    credits = (per_minute * minutes).quantize(PRECISION)
    return _with_cost(MediaQuote(key, tier, minutes, credits, {
        **base, "model": key, "currency": rate["currency"], "per_minute": str(per_minute),
        "minutes_billed": minutes, "duration_sec": float(duration_sec), "source": rate.get("source"),
    }), cost, cost_snap, rate.get("rule_id"))


def quote_generation(model_id: str, resolution: str | None, duration_sec: float | None,
                     *, rates: dict | None = None) -> MediaQuote:
    """Credits for one generated shot: requested seconds × the model/tier per-second price.

    Billing uses the *requested* duration because that is what the person
    approved on the card and what the gateway charges for; the tool refuses
    smart duration (-1), so it is always explicit.
    """
    data = rates if rates is not None else catalogue()
    base = {"version": data["version"], "verified_at": data["verified_at"], "kind": GENERATION_KIND}
    model = (model_id or "").rsplit("/", 1)[-1]
    key = f"video-gen:{model}:{resolution or '?'}"
    table = (data.get("media", {}).get("video-gen") or {}).get(model)
    per = None
    if isinstance(table, dict) and resolution:
        per = (table.get("per_second") or {}).get(resolution)
    if per is None:
        return MediaQuote(key, resolution or "unpriced", 0, None,
                          {**base, "model": key, "reason": f"No verified price for {model} at {resolution or 'unknown resolution'}"})
    if duration_sec is None or duration_sec <= 0:
        return MediaQuote(key, resolution, 0, None, {**base, "model": key, "reason": "Duration not explicit (smart duration is not priced)"})
    per_second = Decimal(str(per))
    if not per_second.is_finite() or per_second < 0:
        raise ValueError("Invalid media rate")
    seconds = math.ceil(float(duration_sec) - 1e-9)
    credits = (per_second * seconds).quantize(PRECISION)
    cost, cost_snap = _unit_cost(table, "per_second", seconds, data, resolution=resolution, seconds=seconds)
    return _with_cost(MediaQuote(key, resolution, seconds, credits, {
        **base, "model": key, "currency": table["currency"], "per_second": str(per_second),
        "verified_at": table.get("verified_at", base["verified_at"]),
        "seconds_billed": seconds, "duration_sec": float(duration_sec), "source": table.get("source"),
    }), cost, cost_snap, (table.get("rule_ids") or {}).get(resolution))


def quote_image(model_id: str, count: int, *, rates: dict | None = None) -> MediaQuote:
    """Credits for generated images: count × the model's per-image price (or the default)."""
    data = rates if rates is not None else catalogue()
    base = {"version": data["version"], "verified_at": data["verified_at"], "kind": IMAGE_KIND}
    model = (model_id or "").rsplit("/", 1)[-1]
    table = data.get("media", {}).get("image-gen") or {}
    rate = table.get(model) or table.get("default")
    key = f"image-gen:{model or 'unknown'}"
    if not isinstance(rate, dict) or count <= 0:
        return MediaQuote(key, "image", 0, None, {**base, "model": key, "reason": "No verified price for this image model"})
    per_image = Decimal(str(rate["per_image"]))
    if not per_image.is_finite() or per_image < 0:
        raise ValueError("Invalid media rate")
    credits = (per_image * int(count)).quantize(PRECISION)
    cost, cost_snap = _unit_cost(rate, "per_image", int(count), data)
    return _with_cost(MediaQuote(key, "image", int(count), credits, {
        **base, "model": key, "currency": rate["currency"], "per_image": str(per_image), "images": int(count),
        "source": rate.get("source"), "priced_as": model if model in table else "default",
    }), cost, cost_snap, rate.get("rule_id"))


def quote_transcription(model_id: str, duration_sec: float | None, *, rates: dict | None = None) -> MediaQuote:
    """Credits for speech-to-text: whole minutes of audio × the engine's per-minute price."""
    data = rates if rates is not None else catalogue()
    base = {"version": data["version"], "verified_at": data["verified_at"], "kind": STT_KIND}
    model = (model_id or "").rsplit("/", 1)[-1]
    rate = (data.get("media", {}).get("stt") or {}).get(model)
    key = f"stt:{model or 'unknown'}"
    if not isinstance(rate, dict):
        return MediaQuote(key, "stt", 0, None, {**base, "model": key, "reason": f"No verified price for {model}"})
    if duration_sec is None or duration_sec <= 0:
        return MediaQuote(key, "stt", 0, None, {**base, "model": key, "reason": "Audio duration not reported"})
    minutes = max(int(rate.get("min_minutes", 1)), math.ceil(float(duration_sec) / 60 - 1e-9))
    per_minute = Decimal(str(rate["per_minute"]))
    if not per_minute.is_finite() or per_minute < 0:
        raise ValueError("Invalid media rate")
    credits = (per_minute * minutes).quantize(PRECISION)
    cost, cost_snap = _unit_cost(rate, "per_minute", minutes, data)
    return _with_cost(MediaQuote(key, "stt", minutes, credits, {
        **base, "model": key, "currency": rate["currency"], "per_minute": str(per_minute),
        "minutes_billed": minutes, "duration_sec": float(duration_sec), "source": rate.get("source"),
    }), cost, cost_snap, rate.get("rule_id"))


def quote_hot_trends(source: str, *, rates: dict | None = None) -> MediaQuote:
    """Credits for one live hot-list collection (cache hits are never billed)."""
    data = rates if rates is not None else catalogue()
    base = {"version": data["version"], "verified_at": data["verified_at"], "kind": TRENDS_KIND}
    table = data.get("media", {}).get("hot-trends") or {}
    rate = table.get(source) if isinstance(table.get(source), dict) else table.get("default")
    key = f"hot-trends:{source}"
    if not isinstance(rate, dict):
        return MediaQuote(key, "fetch", 0, None, {**base, "model": key, "reason": f"No verified price for {source}"})
    per_fetch = Decimal(str(rate["per_fetch"]))
    if not per_fetch.is_finite() or per_fetch < 0:
        raise ValueError("Invalid media rate")
    cost, cost_snap = _unit_cost(rate, "per_fetch", 1, data)
    return _with_cost(MediaQuote(key, "fetch", 1, per_fetch.quantize(PRECISION), {
        **base, "model": key, "currency": rate["currency"], "per_fetch": str(per_fetch), "source": rate.get("source"),
    }), cost, cost_snap, rate.get("rule_id"))


def quote_voice_call(model_id: str, snapshot: dict, duration_sec: float) -> MediaQuote:
    """A voice call's price: its tokens by modality at ``media.voice-realtime`` prices.

    ``snapshot`` is the call meter's final one (``voice.meter.CallMeter``): the
    provider's own usage, plus an estimate for replies whose usage never
    arrived (spoken audio is billed whether or not it was heard).
    """
    from voice.meter import call_prices
    prices = call_prices(model_id)
    minutes = math.ceil(duration_sec / 60) if duration_sec > 0 else 0
    base = {"model": model_id, "currency": "CNY", "tokens": snapshot.get("tokens", {}),
            "provisional_yuan": snapshot.get("provisional_yuan"), "unreported_rounds": snapshot.get("unreported_rounds")}
    if prices.rates is None:
        return MediaQuote(model_id, "", minutes, None, {**base, "reason": "No verified price for this voice model"})
    credits = Decimal(snapshot["total_yuan"]).quantize(PRECISION)
    quote = MediaQuote(model_id, "", minutes, credits, {
        **base, "per_million": {key: str(value) for key, value in prices.rates.items()},
        "verified_at": prices.date, "source": prices.source})
    data = catalogue()
    entry = (data.get("media", {}).get("voice-realtime") or {}).get(model_id) or {}
    cost = (entry.get("cost") or {}) if isinstance(entry, dict) else {}
    per = cost.get("per_million") if isinstance(cost, dict) else None
    if isinstance(per, dict) and all(m in per for m in ("input_text", "input_audio", "output_text", "output_audio")):
        fx = cost_fx(data, str(cost.get("currency", "CNY")).upper())
        tokens = snapshot.get("tokens") or {}
        total = sum((Decimal(int(tokens.get(m, 0))) * Decimal(str(per[m])) * fx for m in per), Decimal(0)) / Decimal(1_000_000)
        return _with_cost(quote, total.quantize(PRECISION),
                          cost_snapshot(cost, extra={"per_million": {m: str(v) for m, v in per.items()}}), entry.get("rule_id"))
    return _with_cost(quote, None, None, entry.get("rule_id"))


async def voice_credit_room(workspace_id: str) -> Decimal | None:
    """What a call may spend: the workspace balance in ``enforce``, None (no cap) otherwise.

    Raises ``INSUFFICIENT_CREDITS`` when there is nothing to spend, the rule
    ``UsageMeter.start`` applies to a model call.
    """
    if billing_mode() != "enforce":
        return None
    from db.base import get_db_session

    async with get_db_session() as db:
        account = await lock_balance(db, workspace_id)
        from billing.subscriptions import ensure_period_allowance
        await ensure_period_allowance(db, account, datetime.now(timezone.utc))
        if account.balance <= 0:
            raise BillingError("INSUFFICIENT_CREDITS", "积分不足，请先充值后继续")
        return account.balance


async def settle_voice_call(*, call_id: str, workspace_id: str, user_id: str, session_id: str | None,
                            model_id: str, snapshot: dict, duration_sec: float) -> Decimal | None:
    """Record (and in enforce, charge) one finished call, once (keyed on the call). Nothing said, nothing billed."""
    price = quote_voice_call(model_id, snapshot, duration_sec)
    if price.credits is not None and price.credits <= 0:
        return None
    return await settle(key=f"voice:{call_id}", workspace_id=workspace_id, user_id=user_id, session_id=session_id,
                        price=price, kind=VOICE_KIND, quantity_known=True,
                        tokens={**snapshot.get("tokens", {}), "duration_sec": round(duration_sec, 1),
                                "call_id": call_id, "model": model_id},
                        default_title="语音通话", title="语音通话")


async def precheck_compose(session_id: str | None) -> None:
    """Enforce-mode gate before a paid submit. Shadow/off never refuse."""
    if billing_mode() != "enforce":
        return
    from db.base import get_db_session
    from db.models.session import Session

    async with get_db_session() as db:
        session = await db.get(Session, session_id or "")
        if session is None:
            raise BillingError("BILLING_SESSION_REQUIRED", "A billable session is required")
        account = await lock_balance(db, session.workspace_id)
        from billing.subscriptions import ensure_period_allowance
        await ensure_period_allowance(db, account, datetime.now(timezone.utc))
        if account.balance <= 0:
            raise BillingError("INSUFFICIENT_CREDITS", "积分不足，请先充值后继续")


async def settle_compose(job, asset, *, width: int, height: int, duration_sec: float | None) -> Decimal | None:
    """Record (and in enforce, charge) one finished composition. Idempotent on job id."""
    if asset is None:
        return None
    price = quote_compose(width, height, duration_sec)
    unit = "seconds_billed" if "per_second" in price.snapshot else "minutes_billed"
    return await settle(key=f"compose:{job.id}", workspace_id=asset.workspace_id, user_id=job.user_id,
                        session_id=job.session_id, price=price, kind=USAGE_KIND, quantity_known=duration_sec is not None,
                        tokens={"duration_sec": duration_sec, unit: price.minutes_billed, "tier": price.tier},
                        default_title="视频合成")


async def settle_generation(job, asset, *, model_id: str, resolution: str | None, duration_sec: float | None) -> Decimal | None:
    """Record (and in enforce, charge) one completed generated shot. Idempotent on job id."""
    if asset is None:
        return None
    saved = (getattr(job, "request_data", None) or {}).get("billing_quote")
    if saved is not None:
        # Only server-created, complete quotes are persisted at submission.
        # A corrupt snapshot must not silently fall back to today's price.
        snapshot = saved["snapshot"]
        amount = Decimal(saved["credits"])
        per_second = Decimal(snapshot["per_second"])
        quantity = saved["quantity"]
        expected_id = f"video-gen:{model_id.rsplit('/', 1)[-1]}:{resolution or '?'}"
        if (saved["model_id"] != expected_id or saved["tier"] != resolution
                or not duration_sec or quantity != math.ceil(duration_sec)
                or not amount.is_finite() or amount < 0
                or not per_second.is_finite() or per_second < 0
                or (per_second * quantity).quantize(PRECISION) != amount):
            raise ValueError("Invalid stored video generation quote")
        price = MediaQuote(saved["model_id"], saved["tier"], quantity, amount, snapshot)
    else:
        price = quote_generation(model_id, resolution, duration_sec)
    return await settle(key=f"generate:{job.id}", workspace_id=asset.workspace_id, user_id=job.user_id,
                        session_id=job.session_id, price=price, kind=GENERATION_KIND,
                        quantity_known=bool(duration_sec and duration_sec > 0),
                        tokens={"duration_sec": duration_sec, "seconds_billed": price.minutes_billed, "resolution": resolution, "model": model_id},
                        default_title="视频生成")


async def settle_image(*, key: str, workspace_id: str, user_id: str, session_id: str | None,
                       model_id: str, count: int) -> Decimal | None:
    """Record (and in enforce, charge) one image_gen call that stored ``count`` images.

    ``key`` must be stable per call (the tool-call part id): a retried call
    that stores the same images is one charge.
    """
    price = quote_image(model_id, count)
    return await settle(key=key, workspace_id=workspace_id, user_id=user_id, session_id=session_id, price=price,
                        kind=IMAGE_KIND, quantity_known=count > 0,
                        tokens={"images": count, "model": model_id}, default_title="图片生成")


async def settle_transcription(job, *, workspace_id: str, model_id: str, duration_sec: float | None) -> Decimal | None:
    """Record (and in enforce, charge) one completed transcription. Idempotent on job id."""
    price = quote_transcription(model_id, duration_sec)
    return await settle(key=f"transcribe:{job.id}", workspace_id=workspace_id, user_id=job.user_id,
                        session_id=job.session_id, price=price, kind=STT_KIND,
                        quantity_known=bool(duration_sec and duration_sec > 0),
                        tokens={"duration_sec": duration_sec, "minutes_billed": price.minutes_billed, "model": model_id},
                        default_title="语音转写")


async def settle_hot_trends(*, snapshot_id: str, source: str, workspace_id: str, user_id: str,
                            session_id: str | None, item_count: int) -> Decimal | None:
    """One row per live collection, keyed on the snapshot; the cached readers pay nothing."""
    price = quote_hot_trends(source)
    return await settle(
        key=f"hot_trends:{snapshot_id}", workspace_id=workspace_id, user_id=user_id, session_id=session_id,
        price=price, kind=TRENDS_KIND, quantity_known=True,
        tokens={"source": source, "items": item_count, "fetches": 1}, default_title="热点采集",
    )


async def settle(*, key: str, workspace_id: str, user_id: str, session_id: str | None, price: MediaQuote,
                 kind: str, quantity_known: bool, tokens: dict[str, Any], default_title: str,
                 title: str | None = None) -> Decimal | None:
    """One ``usage_events`` row per key; ledger post only in enforce.

    Status: ``unreported`` when the billable quantity never arrived (visible,
    never free), ``unpriced`` when the size/model has no verified price,
    else ``charged`` (enforce) or ``shadow``. Returns credits for
    charged/shadow, None otherwise.
    """
    from sqlalchemy import select

    from core.identifier import ascending as generate_id
    from db.base import get_db_session
    from db.models.billing import UsageEvent
    from db.models.session import Session

    mode = billing_mode()
    if mode == "off" or not workspace_id:
        return None
    async with get_db_session() as db:
        account = await lock_balance(db, workspace_id)
        existing = (await db.execute(select(UsageEvent).where(UsageEvent.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            return existing.credits
        session = await db.get(Session, session_id) if session_id else None
        if not quantity_known:
            status = "unreported"
        elif price.credits is None:
            status = "unpriced"
        else:
            status = "charged" if mode == "enforce" else "shadow"
        event = UsageEvent(
            id=generate_id("usage"), idempotency_key=key, workspace_id=workspace_id,
            user_id=user_id, session_id=session_id or "", message_id=None,
            session_title=title or (session.title if session and session.title else default_title),
            model_id=price.model_id, kind=kind, tokens=tokens, total_tokens=0,
            credits=price.credits, cost_credits=price.cost, status=status, pricing=price.snapshot,
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        if status == "charged":
            post_ledger(db, account, amount=-price.credits, kind="usage", reference_id=event.id, key=f"usage:{event.id}")
    return price.credits if status in ("charged", "shadow") else None
