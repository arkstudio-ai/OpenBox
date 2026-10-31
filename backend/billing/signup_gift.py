"""The welcome credits behind the first video (docs/LIGHT_MODE_PLAN.md §3.4).

Two one-time ledger rows, both ``kind="signup_gift"`` so the bill reads
「新人首条视频」: a fixed grant when the workspace is created, and a top-up of
the shortfall when the first video is submitted with too little balance. The
second exists because chat can spend the first before the person reaches the
video; the promise is one video, not four credits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from core.log import create_logger

log = create_logger(__name__)

KIND = "signup_gift"
PRECISION = Decimal("0.0001")


def _grant_key(workspace_id: str) -> str:
    return f"signup_gift:{workspace_id}"


def _topup_key(workspace_id: str) -> str:
    return f"first_video_topup:{workspace_id}"


async def grant_signup_gift(workspace_id: str, user_id: str) -> Decimal | None:
    """Post the free plan's welcome credits once per workspace; None when nothing was posted."""
    from billing.plans import plan_catalog
    from billing.service import lock_balance, post_ledger
    from db.base import get_db_session
    from db.models.billing import CreditLedger

    credits = plan_catalog().plan("free").signup_gift_credits
    if credits is None or credits <= 0:
        return None
    key = _grant_key(workspace_id)
    async with get_db_session() as db:
        account = await lock_balance(db, workspace_id)
        if await db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == key)):
            return None
        post_ledger(db, account, amount=credits, kind=KIND, reference_id=user_id[:64], key=key)
        await db.flush()
    return credits


async def grant_signup_gift_safely(workspace_id: str | None, user_id: str) -> None:
    """Registration must never fail because of billing; a missed gift is logged, not raised."""
    if not workspace_id:
        return
    try:
        await grant_signup_gift(workspace_id, user_id)
    except Exception as exc:  # the account exists; the gift can be re-posted by an admin
        log.warning("signup gift not posted workspace=%s error=%s", workspace_id, type(exc).__name__)


async def ensure_first_video_credits(workspace_id: str, user_id: str, required: Decimal | None) -> Decimal | None:
    """Top the balance up to ``required`` once, so the first video is never refused for credits.

    Only under ``BILLING_MODE=enforce``: the other modes never refuse, and a
    shadow deployment must not hand out credits for a gate that does not run.
    """
    from billing.service import billing_mode, lock_balance, post_ledger
    from billing.subscriptions import ensure_period_allowance
    from db.base import get_db_session
    from db.models.billing import CreditLedger

    if billing_mode() != "enforce" or required is None or required <= 0:
        return None
    key = _topup_key(workspace_id)
    async with get_db_session() as db:
        account = await lock_balance(db, workspace_id)
        await ensure_period_allowance(db, account, datetime.now(timezone.utc))
        if account.balance >= required:
            return None
        if await db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == key)):
            return None
        shortfall = (Decimal(required) - account.balance).quantize(PRECISION)
        post_ledger(db, account, amount=shortfall, kind=KIND, reference_id=user_id[:64], key=key)
        await db.flush()
    return shortfall
