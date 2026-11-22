"""Credit accounting for paid calls made outside the guided Agent turn."""
from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager

from sqlalchemy import select, text

from billing import media, pricing
from billing.service import BillingError, billing_mode
from db.base import get_db_session
from db.models.billing import UsageEvent
from light.mode import LightError


def billing_error(exc: BillingError) -> LightError:
    return LightError(exc.code, str(exc), 402 if exc.code == "INSUFFICIENT_CREDITS" else 503)


@asynccontextmanager
async def once(key: str):
    """Share one paid content request across concurrent API workers."""
    async with media._workspace_lock(key), get_db_session() as db:
        if db.bind.dialect.name == "postgresql":
            number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})
        yield


async def metered(key: str) -> bool:
    if billing_mode() == "off":
        return True
    async with get_db_session() as db:
        status = await db.scalar(select(UsageEvent.status).where(UsageEvent.idempotency_key == key))
        return status == "charged" if billing_mode() == "enforce" else status in {"charged", "shadow"}


@asynccontextmanager
async def admit(user_workspace: str, session: str | None, price: media.MediaQuote, step: str):
    if billing_mode() == "enforce" and price.credits is None:
        raise LightError("MODEL_UNPRICED", "This service has no configured credit price.", 503)
    try:
        async with media.hold_for_submit(session, workspace_id=user_workspace, credits=price.credits, step=step):
            yield
    except BillingError as exc:
        raise billing_error(exc) from exc


def tts_quote(model: str, usage: dict) -> media.MediaQuote:
    price = pricing.quote(model, pricing.normalize_usage(usage))
    return media.MediaQuote(model, "voice_synthesis", 0, price.credits, price.snapshot, price.cost)
