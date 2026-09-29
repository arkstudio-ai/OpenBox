"""Prepaid terms and one allowance per Beijing calendar week or month."""
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.plans import Plan, plan_catalog
from billing.service import BillingError, now, post_ledger
from db.models.billing import BillingSubscription, CreditBalance, CreditLedger, PaymentOrder

CHINA = ZoneInfo("Asia/Shanghai")


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def add_months(value: datetime, months: int) -> datetime:
    local = utc(value).astimezone(CHINA)
    year, month = divmod(local.year * 12 + local.month - 1 + months, 12)
    return local.replace(year=year, month=month + 1,
                         day=min(local.day, monthrange(year, month + 1)[1])).astimezone(timezone.utc)


def allowance_period(at: datetime, period: str) -> tuple[datetime, datetime]:
    local = utc(at).astimezone(CHINA).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        start = local - timedelta(days=local.weekday())
        end = start + timedelta(days=7)
    else:
        start = local.replace(day=1)
        end = add_months(start, 1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def active_subscription(db: AsyncSession, workspace_id: str, at: datetime) -> BillingSubscription | None:
    return (await db.scalars(select(BillingSubscription).where(
        BillingSubscription.workspace_id == workspace_id,
        BillingSubscription.starts_at <= at, BillingSubscription.ends_at > at,
        or_(BillingSubscription.cancelled_at.is_(None), BillingSubscription.cancelled_at > at),
    ).order_by(BillingSubscription.starts_at.desc()).limit(1))).first()


async def require_topup_allowed(db: AsyncSession, workspace_id: str, at: datetime) -> None:
    if await active_subscription(db, workspace_id, at) is None:
        raise BillingError("PAID_PLAN_REQUIRED", "积分充值需先订购付费套餐")


async def ensure_period_allowance(db: AsyncSession, account: CreditBalance, at: datetime | None = None) -> None:
    """Caller holds the account lock, shared by purchases, grants and model usage."""
    at = at or now()
    subscription = await active_subscription(db, account.workspace_id, at)
    plan = Plan.model_validate(subscription.plan) if subscription else plan_catalog().plan("free")
    start, _ = allowance_period(at, plan.credit_period)
    key = f"allowance:{account.workspace_id}:{plan.id}:{start.astimezone(CHINA).date()}"
    if await db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == key)):
        return
    post_ledger(db, account, amount=plan.credits, kind="allowance",
                reference_id=subscription.id if subscription else "free", key=key)
    # Repeated calls in this transaction must see the grant even with autoflush disabled.
    await db.flush()


async def apply_subscription(db: AsyncSession, account: CreditBalance, order: PaymentOrder) -> None:
    product = order.product
    plan = Plan.model_validate(product["plan"])
    at = order.paid_at
    latest_end = await db.scalar(select(func.max(BillingSubscription.ends_at)).where(
        BillingSubscription.workspace_id == account.workspace_id,
        BillingSubscription.cancelled_at.is_(None),
    ))
    # Prepaid time is never replaced by a later purchase, including a plan switch.
    start = max(utc(latest_end), at) if latest_end else at
    end = add_months(start, 12 if product["cycle"] == "yearly" else 1)
    db.add(BillingSubscription(order_id=order.id, workspace_id=account.workspace_id,
        plan_id=plan.id, cycle=product["cycle"], plan=plan.model_dump(mode="json"),
        starts_at=start, ends_at=end))
    order.product = {**product, "starts_at": start.isoformat(), "ends_at": end.isoformat()}
    await db.flush()
    if start <= at:
        await ensure_period_allowance(db, account, at)


async def subscription_view(db: AsyncSession, account: CreditBalance, can_manage: bool) -> dict:
    at = now()
    await ensure_period_allowance(db, account, at)
    active = await active_subscription(db, account.workspace_id, at)
    plan = Plan.model_validate(active.plan) if active else plan_catalog().plan("free")
    _, next_grant = allowance_period(at, plan.credit_period)
    queued = (await db.scalars(select(BillingSubscription).where(
        BillingSubscription.workspace_id == account.workspace_id, BillingSubscription.starts_at > at,
        BillingSubscription.cancelled_at.is_(None),
    ).order_by(BillingSubscription.starts_at))).all()

    def view(entry: BillingSubscription) -> dict:
        return {"plan_id": entry.plan_id, "cycle": entry.cycle,
                "starts_at": utc(entry.starts_at), "ends_at": utc(entry.ends_at)}

    return {"plan_id": plan.id, "cycle": active.cycle if active else None,
            "starts_at": utc(active.starts_at) if active else None,
            "ends_at": utc(active.ends_at) if active else None,
            "credits": str(plan.credits), "credit_period": plan.credit_period,
            "next_grant_at": min(next_grant, utc(active.ends_at)) if active else next_grant,
            "topup_allowed": active is not None, "can_manage": can_manage,
            "queued": [view(entry) for entry in queued]}
