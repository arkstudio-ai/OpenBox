"""Read-only operator view of every workspace's subscription, orders and usage.

Deliberately no `get_workspace` dependency: this is a global view across tenants,
so an `X-Workspace-Id` header would scope nothing and only invite the false
impression that these reads are workspace-bound. Money is never written here —
§3-Q5 keeps grants, refunds and expiry changes out of this round.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select

from audit import record
from auth.middleware import require_admin
from billing.plans import plan_catalog
from billing.service import now
from billing.subscriptions import active_subscription, utc
from db.base import get_db_session
from db.models.billing import (
    BillingSubscription,
    CreditBalance,
    CreditLedger,
    PaymentOrder,
    UsageEvent,
)
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember


router = APIRouter(
    prefix="/api/admin/billing",
    tags=["admin-billing"],
    dependencies=[Depends(require_admin)],
)

USAGE_WINDOW_DAYS = 30
DETAIL_ORDER_LIMIT = 100
DETAIL_LEDGER_LIMIT = 50
ORDER_STATUSES = "pending|paid|cancelled"
ORDER_KINDS = "topup|subscription"


def _at(value: datetime | None) -> str | None:
    """Datetimes come back naive from PostgreSQL; clients only ever see UTC ISO."""
    return utc(value).isoformat() if value is not None else None


def _amount(value: Decimal | int | None) -> str | None:
    """Credits are Numeric(28, 12); a float round-trip would quietly lose cents."""
    return None if value is None else str(value)


def _workspace_brief(workspace: Workspace) -> dict:
    return {"id": workspace.id, "name": workspace.name, "kind": workspace.kind}


def _user_brief(user: User | None) -> dict | None:
    # Orders outlive the accounts that placed them, so the join may come back empty.
    if user is None:
        return None
    return {"id": user.id, "username": user.username, "email": user.email}


def _subscription_view(entry: BillingSubscription) -> dict:
    return {
        "order_id": entry.order_id,
        "plan_id": entry.plan_id,
        "cycle": entry.cycle,
        "starts_at": _at(entry.starts_at),
        "ends_at": _at(entry.ends_at),
    }


def _order_view(order: PaymentOrder) -> dict:
    """Mirrors `billing.payments.order_view` minus `checkout_url`.

    An operator list must never carry a payable link, so this cannot reuse that
    helper. `provider_order_id` stays: reconciling against the provider's
    console is the reason this list exists.
    """
    product = order.product or {}
    plan = product.get("plan") or {}
    return {
        "id": order.id,
        "workspace_id": order.workspace_id,
        "user_id": order.user_id,
        "provider": order.provider,
        "kind": order.kind,
        "amount_fen": order.amount_fen,
        "currency": order.currency,
        "credits": _amount(order.credits),
        "status": order.status,
        "plan_id": plan.get("id"),
        "cycle": product.get("cycle"),
        "provider_order_id": order.provider_order_id,
        "created_at": _at(order.created_at),
        "paid_at": _at(order.paid_at),
        "cancelled_at": _at(order.cancelled_at),
        "cancellation_reason": order.cancellation_reason,
    }


def _ledger_view(entry: CreditLedger) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "amount": _amount(entry.amount),
        "balance_after": _amount(entry.balance_after),
        "reference_id": entry.reference_id,
        "created_at": _at(entry.created_at),
    }


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, timezone.utc)


@router.get("/subscriptions")
async def list_subscriptions(
    request: Request,
    plan: str | None = None,
    state: str | None = Query(None, pattern="^(active|expired|free)$"),
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_admin),
):
    catalog = plan_catalog()
    if plan and plan not in {entry.id for entry in catalog.plans}:
        raise HTTPException(422, detail=f"Unknown plan {plan}")
    at = now()
    # Correlated against the outer Workspace so state can be filtered before
    # pagination — a page computed in Python would report the wrong total.
    current = select(BillingSubscription.workspace_id).where(
        BillingSubscription.workspace_id == Workspace.id,
        BillingSubscription.starts_at <= at,
        BillingSubscription.ends_at > at,
    )
    ever = select(BillingSubscription.workspace_id).where(
        BillingSubscription.workspace_id == Workspace.id
    )
    stmt = (
        select(Workspace, User)
        .join(User, User.id == Workspace.owner_user_id)
        .where(Workspace.is_deleted.is_(False))
    )
    needle = q.strip()
    if needle:
        stmt = stmt.where(or_(
            Workspace.name.ilike(f"%{needle}%"),
            User.username.ilike(f"%{needle}%"),
            User.email.ilike(f"%{needle}%"),
        ))
    if plan:
        # "free" is the absence of a paid term, not a row with plan_id='free'.
        stmt = stmt.where(
            ~current.exists() if plan == "free"
            else current.where(BillingSubscription.plan_id == plan).exists()
        )
    if state == "active":
        stmt = stmt.where(current.exists())
    elif state == "expired":
        stmt = stmt.where(~current.exists(), ever.exists())
    elif state == "free":
        stmt = stmt.where(~ever.exists())

    async with get_db_session() as db:
        total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (await db.execute(
            stmt.order_by(Workspace.created_at.desc(), Workspace.id)
            .offset(offset).limit(limit)
        )).all()
        workspace_ids = [workspace.id for workspace, _ in rows]
        balances: dict[str, Decimal] = {}
        last_paid: dict[str, datetime] = {}
        queued: dict[str, int] = {}
        subscribed: set[str] = set()
        active: dict[str, BillingSubscription] = {}
        if workspace_ids:
            # The same predicate and ordering `active_subscription` applies to
            # one workspace, asked once for the page: a per-row call would be
            # `limit` sequential round-trips for data one grouped read covers.
            for term in (await db.scalars(
                select(BillingSubscription)
                .where(BillingSubscription.workspace_id.in_(workspace_ids),
                       BillingSubscription.starts_at <= at,
                       BillingSubscription.ends_at > at)
                .order_by(BillingSubscription.workspace_id,
                          BillingSubscription.starts_at.desc())
            )):
                active.setdefault(term.workspace_id, term)
            balances = dict((await db.execute(
                select(CreditBalance.workspace_id, CreditBalance.balance)
                .where(CreditBalance.workspace_id.in_(workspace_ids))
            )).all())
            last_paid = dict((await db.execute(
                select(PaymentOrder.workspace_id, func.max(PaymentOrder.paid_at))
                .where(PaymentOrder.workspace_id.in_(workspace_ids),
                       PaymentOrder.status == "paid")
                .group_by(PaymentOrder.workspace_id)
            )).all())
            queued = dict((await db.execute(
                select(BillingSubscription.workspace_id, func.count())
                .where(BillingSubscription.workspace_id.in_(workspace_ids),
                       BillingSubscription.starts_at > at)
                .group_by(BillingSubscription.workspace_id)
            )).all())
            subscribed = set((await db.execute(
                select(BillingSubscription.workspace_id).distinct()
                .where(BillingSubscription.workspace_id.in_(workspace_ids))
            )).scalars())
        items = []
        for workspace, owner in rows:
            # Same predicate the workspace-facing /api/billing/subscription
            # answers with, so the two pages can never disagree (AC-11).
            entry = active.get(workspace.id)
            items.append({
                "workspace": _workspace_brief(workspace),
                "owner": _user_brief(owner),
                "plan_id": entry.plan_id if entry else catalog.plan("free").id,
                "cycle": entry.cycle if entry else None,
                "starts_at": _at(entry.starts_at) if entry else None,
                "ends_at": _at(entry.ends_at) if entry else None,
                # A lapsed term leaves its rows behind; a workspace that never
                # bought one has been on the free plan all along.
                "state": (
                    "active" if entry
                    else "expired" if workspace.id in subscribed
                    else "free"
                ),
                "queued_count": queued.get(workspace.id, 0),
                # Unlike /api/billing/balance this never posts the period
                # allowance — a read-only view must not move money.
                "balance": _amount(balances.get(workspace.id, Decimal(0))),
                "last_paid_at": _at(last_paid.get(workspace.id)),
            })
    await record(
        admin["user_id"], None, "admin.view_billing", "billing_subscription", None,
        {"plan": plan, "state": state, "q": q, "offset": offset, "limit": limit}, request,
    )
    return {"items": items, "total": total or 0, "offset": offset, "limit": limit}


@router.get("/orders")
async def list_orders(
    request: Request,
    provider: str | None = None,
    status: str | None = Query(None, pattern=f"^({ORDER_STATUSES})$"),
    kind: str | None = Query(None, pattern=f"^({ORDER_KINDS})$"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_admin),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="Start date must not follow end date")
    # Outer joins: payment history outlives the workspace and the account that
    # placed the order, and an operator list must not silently drop those rows.
    stmt = (
        select(PaymentOrder, Workspace.name, User)
        .outerjoin(Workspace, Workspace.id == PaymentOrder.workspace_id)
        .outerjoin(User, User.id == PaymentOrder.user_id)
    )
    if provider:
        stmt = stmt.where(PaymentOrder.provider == provider)
    if status:
        stmt = stmt.where(PaymentOrder.status == status)
    if kind:
        stmt = stmt.where(PaymentOrder.kind == kind)
    if date_from:
        stmt = stmt.where(PaymentOrder.created_at >= _day_start(date_from))
    if date_to:
        # Both bounds name whole UTC days, so "to" ends at the next midnight.
        stmt = stmt.where(PaymentOrder.created_at < _day_start(date_to + timedelta(days=1)))
    needle = q.strip()
    if needle:
        stmt = stmt.where(or_(
            PaymentOrder.id.ilike(f"%{needle}%"),
            PaymentOrder.provider_order_id.ilike(f"%{needle}%"),
            Workspace.name.ilike(f"%{needle}%"),
            User.username.ilike(f"%{needle}%"),
            User.email.ilike(f"%{needle}%"),
        ))
    async with get_db_session() as db:
        total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (await db.execute(
            stmt.order_by(PaymentOrder.created_at.desc(), PaymentOrder.id.desc())
            .offset(offset).limit(limit)
        )).all()
        items = [
            {**_order_view(order), "workspace_name": name, "user": _user_brief(user)}
            for order, name, user in rows
        ]
    await record(
        admin["user_id"], None, "admin.view_billing", "payment_order", None,
        {"provider": provider, "status": status, "kind": kind,
         "from": date_from.isoformat() if date_from else None,
         "to": date_to.isoformat() if date_to else None,
         "q": q, "offset": offset, "limit": limit}, request,
    )
    return {"items": items, "total": total or 0, "offset": offset, "limit": limit}


@router.get("/workspaces/{workspace_id}")
async def get_workspace_billing(
    workspace_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    at = now()
    since = at - timedelta(days=USAGE_WINDOW_DAYS)
    async with get_db_session() as db:
        # Soft-deleted workspaces stay readable here: their payment history
        # survives deletion and reconciliation still has to reach it.
        row = (await db.execute(
            select(Workspace, User)
            .outerjoin(User, User.id == Workspace.owner_user_id)
            .where(Workspace.id == workspace_id)
        )).first()
        if row is None:
            raise HTTPException(404, detail="Workspace not found")
        workspace, owner = row
        member_count = await db.scalar(
            select(func.count()).select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id,
                   WorkspaceMember.status == "active")
        )
        balance = await db.scalar(
            select(CreditBalance.balance).where(CreditBalance.workspace_id == workspace_id)
        )
        current = await active_subscription(db, workspace_id, at)
        history = (await db.scalars(
            select(BillingSubscription)
            .where(BillingSubscription.workspace_id == workspace_id)
            .order_by(BillingSubscription.starts_at.desc())
        )).all()
        orders = (await db.execute(
            select(PaymentOrder, User)
            .outerjoin(User, User.id == PaymentOrder.user_id)
            .where(PaymentOrder.workspace_id == workspace_id)
            .order_by(PaymentOrder.created_at.desc(), PaymentOrder.id.desc())
            .limit(DETAIL_ORDER_LIMIT)
        )).all()
        ledger = (await db.scalars(
            select(CreditLedger)
            .where(CreditLedger.workspace_id == workspace_id)
            .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
            .limit(DETAIL_LEDGER_LIMIT)
        )).all()
        usage = (await db.execute(
            select(
                UsageEvent.status,
                func.count(),
                func.coalesce(func.sum(UsageEvent.total_tokens), 0),
                func.coalesce(func.sum(UsageEvent.credits), 0),
            )
            .where(UsageEvent.workspace_id == workspace_id, UsageEvent.created_at >= since)
            .group_by(UsageEvent.status)
            .order_by(UsageEvent.status)
        )).all()
    catalog = plan_catalog()
    result = {
        "workspace": {
            **_workspace_brief(workspace),
            "owner_user_id": workspace.owner_user_id,
            "plan_id": workspace.plan_id,
            "created_at": _at(workspace.created_at),
            "is_deleted": workspace.is_deleted,
            "deleted_at": _at(workspace.deleted_at),
        },
        "owner": _user_brief(owner),
        "member_count": member_count or 0,
        "balance": _amount(balance if balance is not None else Decimal(0)),
        "plan_id": current.plan_id if current else catalog.plan("free").id,
        "subscription": _subscription_view(current) if current else None,
        # Queued terms are already in `history`; filtering it avoids a second query.
        "queued": [
            _subscription_view(entry) for entry in reversed(history)
            if utc(entry.starts_at) > at
        ],
        "history": [_subscription_view(entry) for entry in history],
        "orders": [
            {**_order_view(order), "user": _user_brief(user)} for order, user in orders
        ],
        "ledger": [_ledger_view(entry) for entry in ledger],
        "usage": {
            "since": _at(since),
            "days": USAGE_WINDOW_DAYS,
            "items": [
                {
                    "status": status,
                    "events": events,
                    "total_tokens": int(tokens or 0),
                    "credits": _amount(credits or 0),
                }
                for status, events, tokens, credits in usage
            ],
        },
    }
    await record(
        admin["user_id"], workspace_id, "admin.view_billing", "workspace", workspace_id,
        {"orders": len(result["orders"]), "ledger": len(result["ledger"])}, request,
    )
    return result
