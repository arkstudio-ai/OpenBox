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

from billing.pricing import PRECISION, catalogue
from billing.service import BillingError, billing_mode, lock_balance, post_ledger

USAGE_KIND = "video_compose"
GENERATION_KIND = "video_generate"


@dataclass(frozen=True)
class MediaQuote:
    model_id: str            # rates.json media key, e.g. ims-compose-720p
    tier: str                # 720p
    minutes_billed: int
    credits: Decimal | None  # None = no verified price for this output size
    snapshot: dict[str, Any]


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
    data = rates if rates is not None else catalogue()
    picked = compose_tier(width, height, data)
    base = {"version": data["version"], "verified_at": data["verified_at"], "kind": USAGE_KIND}
    if picked is None:
        return MediaQuote("ims-compose", "unpriced", 0, None, {**base, "reason": "No verified price for this output size"})
    key, rate = picked
    tier = key.rsplit("-", 1)[-1]
    if duration_sec is None:
        return MediaQuote(key, tier, 0, None, {**base, "model": key, "reason": "Duration unknown until the shots are trimmed"})
    minutes = max(int(rate.get("min_minutes", 1)), math.ceil(float(duration_sec) / 60 - 1e-9))
    per_minute = Decimal(str(rate["per_minute"]))
    if not per_minute.is_finite() or per_minute < 0:
        raise ValueError("Invalid media rate")
    credits = (per_minute * minutes).quantize(PRECISION)
    return MediaQuote(key, tier, minutes, credits, {
        **base, "model": key, "currency": rate["currency"], "per_minute": str(per_minute),
        "minutes_billed": minutes, "duration_sec": float(duration_sec), "source": rate.get("source"),
    })


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
    return MediaQuote(key, resolution, seconds, credits, {
        **base, "model": key, "currency": table["currency"], "per_second": str(per_second),
        "seconds_billed": seconds, "duration_sec": float(duration_sec), "source": table.get("source"),
    })


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
    """Record (and in enforce, charge) one finished composition. Idempotent on job id.

    Returns the credits recorded, or None when billing is off, the size is
    unpriced, or the duration never arrived (recorded as ``unreported`` so the
    gap is visible rather than silently free).
    """
    price = quote_compose(width, height, duration_sec)
    return await _settle(job, asset, price, kind=USAGE_KIND, key=f"compose:{job.id}", duration_known=duration_sec is not None,
                         tokens={"duration_sec": duration_sec, "minutes_billed": price.minutes_billed, "tier": price.tier},
                         default_title="视频合成")


async def settle_generation(job, asset, *, model_id: str, resolution: str | None, duration_sec: float | None) -> Decimal | None:
    """Record (and in enforce, charge) one completed generated shot. Idempotent on job id."""
    price = quote_generation(model_id, resolution, duration_sec)
    return await _settle(job, asset, price, kind=GENERATION_KIND, key=f"generate:{job.id}",
                         duration_known=bool(duration_sec and duration_sec > 0),
                         tokens={"duration_sec": duration_sec, "seconds_billed": price.minutes_billed, "resolution": resolution, "model": model_id},
                         default_title="视频生成")


async def _settle(job, asset, price: MediaQuote, *, kind: str, key: str, duration_known: bool,
                  tokens: dict[str, Any], default_title: str) -> Decimal | None:
    from sqlalchemy import select

    from core.identifier import ascending as generate_id
    from db.base import get_db_session
    from db.models.billing import UsageEvent
    from db.models.session import Session

    mode = billing_mode()
    if mode == "off" or asset is None:
        return None
    async with get_db_session() as db:
        account = await lock_balance(db, asset.workspace_id)
        existing = (await db.execute(select(UsageEvent).where(UsageEvent.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            return existing.credits
        session = await db.get(Session, job.session_id) if job.session_id else None
        if not duration_known:
            status = "unreported"
        elif price.credits is None:
            status = "unpriced"
        else:
            status = "charged" if mode == "enforce" else "shadow"
        event = UsageEvent(
            id=generate_id("usage"), idempotency_key=key, workspace_id=asset.workspace_id,
            user_id=job.user_id, session_id=job.session_id or "", message_id=None,
            session_title=(session.title if session and session.title else default_title),
            model_id=price.model_id, kind=kind, tokens=tokens, total_tokens=0,
            credits=price.credits, status=status, pricing=price.snapshot,
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        if status == "charged":
            post_ledger(db, account, amount=-price.credits, kind="usage", reference_id=event.id, key=f"usage:{event.id}")
    return price.credits if status in ("charged", "shadow") else None
