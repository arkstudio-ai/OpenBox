"""Operator billing writes share the ledger lock and commit with their audit receipt."""
import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select

from billing.plans import plan_catalog
from billing.service import lock_balance, now, post_ledger
from billing.subscriptions import add_months, ensure_period_allowance, utc
from core.identifier import ascending
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.billing import BillingSubscription
from db.models.user import User
from db.models.workspace import Workspace


class AdminAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class CreditGrant(AdminAction):
    credits: Decimal = Field(gt=0, le=1_000_000, decimal_places=6, allow_inf_nan=False)

    @field_validator("credits", mode="before")
    @classmethod
    def exact_credits(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Send credits as a decimal string")
        return value


class SubscriptionGrant(AdminAction):
    plan_id: Literal["pro", "max"]
    cycle: Literal["monthly", "yearly"]


class SubscriptionChange(AdminAction):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_id: Literal["pro", "max"]
    ends_at: AwareDatetime


class SubscriptionCancel(AdminAction):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def subscription_record(entry: BillingSubscription) -> dict:
    state = {
        "id": entry.id, "order_id": entry.order_id,
        "source": "payment" if entry.order_id else "admin",
        "plan_id": entry.plan_id, "cycle": entry.cycle,
        "starts_at": utc(entry.starts_at).isoformat(),
        "ends_at": utc(entry.ends_at).isoformat(),
        "cancelled_at": utc(entry.cancelled_at).isoformat() if entry.cancelled_at else None,
    }
    state["revision"] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return state


def _reject(status: int, code: str) -> None:
    raise HTTPException(status, detail={"code": code})


async def manage_billing(
    workspace_id: str, actor_id: str, action: str, payload: AdminAction,
    request: Request, *, subscription_id: str | None = None,
) -> dict:
    receipt_id = "audit_" + hashlib.sha256(
        f"admin-billing:{actor_id}:{workspace_id}:{payload.request_key}".encode()
    ).hexdigest()[:58]
    intent = {"action": action, "subscription_id": subscription_id,
              **payload.model_dump(mode="json", exclude={"request_key"})}
    if isinstance(payload, CreditGrant):
        intent["credits"] = format(payload.credits.normalize(), "f")
    async with get_db_session() as db:
        # JWT roles can outlive a demotion; money writes always check current authority.
        actor = await db.get(User, actor_id)
        if actor is None or actor.role != "admin" or not actor.is_active or actor.is_deleted:
            _reject(403, "ADMIN_REQUIRED")
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None:
            _reject(404, "WORKSPACE_NOT_FOUND")
        owner = await db.get(User, workspace.owner_user_id)
        if workspace.is_deleted or owner is None or owner.is_deleted or not owner.is_active:
            _reject(409, "BILLING_ACCOUNT_UNAVAILABLE")
        account = await lock_balance(db, workspace_id)
        previous = await db.get(AuditLog, receipt_id)
        if previous is not None:
            if (previous.details or {}).get("request") != intent:
                _reject(409, "IDEMPOTENCY_CONFLICT")
            return {**previous.details["result"], "replayed": True}

        at = now()
        before = {"balance": str(account.balance)}
        term = None
        if isinstance(payload, CreditGrant):
            post_ledger(db, account, amount=payload.credits, kind="admin_grant",
                        reference_id=receipt_id, key=f"admin-credit:{receipt_id}")
        elif isinstance(payload, SubscriptionGrant):
            terms = list(await db.scalars(select(BillingSubscription).where(
                BillingSubscription.workspace_id == workspace_id,
                BillingSubscription.cancelled_at.is_(None),
            )))
            start = max([at, *(utc(row.ends_at) for row in terms)])
            plan = plan_catalog().plan(payload.plan_id)
            term = BillingSubscription(
                id=ascending("subscription"), workspace_id=workspace_id,
                plan_id=plan.id, cycle=payload.cycle, plan=plan.model_dump(mode="json"),
                starts_at=start, ends_at=add_months(start, 12 if payload.cycle == "yearly" else 1),
            )
            if utc(term.ends_at) > add_months(at, 120):
                _reject(422, "SUBSCRIPTION_TOO_LONG")
            db.add(term)
            await db.flush()
        else:
            term = await db.get(BillingSubscription, subscription_id)
            if term is None or term.workspace_id != workspace_id:
                _reject(404, "SUBSCRIPTION_NOT_FOUND")
            before["subscription"] = subscription_record(term)
            if before["subscription"]["revision"] != payload.expected_revision:
                _reject(409, "SUBSCRIPTION_CHANGED")
            if term.cancelled_at is not None or utc(term.ends_at) <= at:
                _reject(409, "SUBSCRIPTION_ENDED")
            if isinstance(payload, SubscriptionChange):
                end = utc(payload.ends_at)
                if end <= max(at, utc(term.starts_at)) or end > add_months(at, 120):
                    _reject(422, "INVALID_SUBSCRIPTION_END")
                overlap = await db.scalar(select(BillingSubscription.id).where(
                    BillingSubscription.workspace_id == workspace_id,
                    BillingSubscription.id != term.id,
                    BillingSubscription.cancelled_at.is_(None),
                    BillingSubscription.starts_at < end,
                    BillingSubscription.ends_at > term.starts_at,
                ).limit(1))
                if overlap:
                    _reject(409, "SUBSCRIPTION_OVERLAP")
                term.plan_id = payload.plan_id
                term.plan = plan_catalog().plan(payload.plan_id).model_dump(mode="json")
                term.ends_at = end
            elif isinstance(payload, SubscriptionCancel):
                term.cancelled_at = at
            else:
                raise ValueError("Unknown billing action")

        if term is not None and term.cancelled_at is None:
            await db.flush()
            if utc(term.starts_at) <= at:
                await ensure_period_allowance(db, account, at)
            # Same durable provisioning path as purchased subscriptions, without a fake payment.
            from sandbox.desktop_activation import enqueue_subscription_activation
            await enqueue_subscription_activation(db, term, owner.id)
        result = {"operation_id": receipt_id, "action": action, "workspace_id": workspace_id,
                  "balance": str(account.balance), "subscription": subscription_record(term) if term else None}
        db.add(AuditLog(
            id=receipt_id, user_id=actor_id, workspace_id=workspace_id,
            action=f"admin.{action}", resource_type="billing_subscription" if term else "credit_balance",
            resource_id=term.id if term else workspace_id,
            details={"request": intent, "before": before, "result": result},
            ip_address=request.client.host if request.client else None,
            user_agent=(request.headers.get("user-agent") or "")[:512] or None,
            created_at=at,
        ))
        await db.flush()
        return {**result, "replayed": False}
