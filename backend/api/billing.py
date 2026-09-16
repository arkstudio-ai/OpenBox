"""Workspace-authorized billing reads and provider-neutral payment entry points."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import case, func, select

from auth.middleware import get_current_user
from auth.workspace import get_workspace, require_workspace_role
from billing.payments import (apply_receipt, cancel_order, continue_order, create_app_checkout,
                              create_order, order_view, refresh_order)
from billing.providers import get_provider, providers
from billing.service import BillingError, billing_mode, lock_balance
from billing.plans import plan_catalog
from billing.subscriptions import ensure_period_allowance, subscription_view
from db.base import get_db_session
from db.models.billing import CreditLedger, PaymentOrder, UsageEvent
from db.models.session import Session

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _error(exc: BillingError) -> HTTPException:
    status = {"PAYMENT_UNAVAILABLE": 503, "PAYMENT_INVALID_SIGNATURE": 401,
              "PAYMENT_ORDER_NOT_FOUND": 404, "PAYMENT_INVALID_CHECKOUT": 502,
              "PAYMENT_INVALID_RECEIPT": 422, "PAYMENT_QUERY_FAILED": 502, "PAYMENT_CANCEL_FAILED": 502,
              "PAID_PLAN_REQUIRED": 403, "INVALID_PLAN": 422, "INVALID_TOPUP_AMOUNT": 422}.get(exc.code, 409)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@dataclass(frozen=True)
class UsageDateRange:
    start: datetime | None
    end: datetime | None


USAGE_KINDS = {"chat", "title", "suggestions", "compaction", "compaction_chunk", "bash_judge", "cron_summary",
               "video_compose", "video_generate", "image_gen", "video_transcribe", "video_analyze", "hot_trends"}


def usage_kind(kind: str | None = Query(None, min_length=1, max_length=32)) -> str | None:
    """Optional usage kind (video_compose, chat, ...); unknown kinds are rejected, not silently empty."""
    if kind is not None and kind not in USAGE_KINDS:
        raise HTTPException(422, detail={"code": "INVALID_USAGE_KIND", "message": "Unknown usage kind"})
    return kind


def usage_date_range(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    tz: str = Query("Asia/Shanghai", min_length=1, max_length=64),
) -> UsageDateRange:
    """Calendar dates are inclusive in the caller's IANA time zone."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail={"code": "INVALID_DATE_RANGE", "message": "Start date must not follow end date"})
    try:
        zone = ZoneInfo(tz)
        start = datetime.combine(date_from, time.min, zone).astimezone(timezone.utc) if date_from else None
        # Build the next local midnight before converting to UTC, including DST.
        end = datetime.combine(date_to + timedelta(days=1), time.min, zone).astimezone(timezone.utc) if date_to else None
    except (ZoneInfoNotFoundError, ValueError, OverflowError) as exc:
        raise HTTPException(422, detail={"code": "INVALID_DATE_RANGE", "message": "Invalid dates or time zone"}) from exc
    return UsageDateRange(start, end)


def usage_scope(workspace_id: str, dates: UsageDateRange, kind: str | None = None) -> list:
    filters = [UsageEvent.workspace_id == workspace_id]
    if kind is not None:
        filters.append(UsageEvent.kind == kind)
    if dates.start is not None:
        filters.append(UsageEvent.created_at >= dates.start)
    if dates.end is not None:
        filters.append(UsageEvent.created_at < dates.end)
    return filters


@router.get("/balance")
async def balance(workspace: dict = Depends(get_workspace)):
    async with get_db_session() as db:
        account = await lock_balance(db, workspace["id"])
        await ensure_period_allowance(db, account)
        return {"workspace_id": workspace["id"], "balance": str(account.balance),
                "mode": billing_mode()}


@router.get("/plans")
async def plans(workspace: dict = Depends(get_workspace)):
    return plan_catalog().model_dump(mode="json")


@router.get("/subscription")
async def subscription(workspace: dict = Depends(get_workspace)):
    async with get_db_session() as db:
        account = await lock_balance(db, workspace["id"])
        return await subscription_view(db, account, workspace["role"] in {"owner", "admin"})


@router.get("/summary")
async def summary(workspace: dict = Depends(get_workspace), dates: UsageDateRange = Depends(usage_date_range),
                  kind: str | None = Depends(usage_kind)):
    async with get_db_session() as db:
        totals = (await db.execute(select(
            func.coalesce(func.sum(UsageEvent.total_tokens), 0),
            func.coalesce(func.sum(UsageEvent.credits), 0),
            func.coalesce(func.sum(case((UsageEvent.status == "charged", UsageEvent.credits), else_=0)), 0),
            func.count(case((UsageEvent.status == "historical", 1))),
            func.count(case((UsageEvent.credits.is_(None), 1))),
        ).where(*usage_scope(workspace["id"], dates, kind)))).one()
        return {"total_tokens": totals[0], "total_credits": str(totals[1]),
                "charged_credits": str(totals[2]), "historical_count": totals[3], "unpriced_count": totals[4]}


@router.get("/usage")
async def usage(page: int = Query(1, ge=1, le=1_000_000), page_size: int = Query(20, ge=1, le=100),
                workspace: dict = Depends(get_workspace), dates: UsageDateRange = Depends(usage_date_range),
                kind: str | None = Depends(usage_kind)):
    async with get_db_session() as db:
        scope = usage_scope(workspace["id"], dates, kind)
        total = await db.scalar(select(func.count()).select_from(UsageEvent).where(*scope)) or 0
        rows = (await db.execute(select(UsageEvent, Session.title, Session.is_deleted).outerjoin(
            Session, (Session.id == UsageEvent.session_id) & (Session.workspace_id == workspace["id"])
        ).where(*scope).order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
         .offset((page - 1) * page_size).limit(page_size))).all()
        return {"page": page, "page_size": page_size, "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size), "items": [
                    {"id": event.id, "session_id": event.session_id,
                     "session_title": title or event.session_title, "session_available": deleted is False,
                     "model_id": event.model_id, "kind": event.kind, "tokens": event.tokens,
                     "total_tokens": event.total_tokens,
                     "credits": str(event.credits) if event.credits is not None else None,
                     "status": event.status, "created_at": event.created_at,
                     "pricing_version": event.pricing.get("version")}
                    for event, title, deleted in rows]}


@router.get("/ledger")
async def ledger(page: int = Query(1, ge=1, le=1_000_000), page_size: int = Query(20, ge=1, le=100),
                 workspace: dict = Depends(get_workspace)):
    async with get_db_session() as db:
        scope = CreditLedger.workspace_id == workspace["id"]
        total = await db.scalar(select(func.count()).select_from(CreditLedger).where(scope)) or 0
        rows = (await db.scalars(select(CreditLedger).where(scope)
            .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
            .offset((page - 1) * page_size).limit(page_size))).all()
        return {"page": page, "page_size": page_size, "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size), "items": [
                    {"id": row.id, "kind": row.kind, "amount": str(row.amount),
                     "balance_after": str(row.balance_after), "created_at": row.created_at} for row in rows]}


@router.get("/providers")
async def payment_providers(workspace: dict = Depends(get_workspace)):
    return {"items": [{"id": name, "name": provider.display_name,
        "confirmation_mode": getattr(provider, "confirmation_mode", "callback"),
        "supports_status_query": callable(getattr(provider, "query_payment", None)),
        "supports_cancel": callable(getattr(provider, "close_payment", None)),
        "supports_app_checkout": callable(getattr(provider, "create_app_checkout", None)),
        "refresh_checkout": bool(getattr(provider, "refresh_checkout", False))}
        for name, provider in providers().items()]}


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    kind: Literal["topup"] = "topup"
    amount_fen: int = Field(ge=100, le=10_000_000)
    request_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class SubscriptionOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    kind: Literal["subscription"]
    plan_id: Literal["pro", "max"]
    cycle: Literal["monthly", "yearly"]
    request_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/orders")
async def new_order(body: OrderBody | SubscriptionOrderBody,
                    workspace: dict = Depends(require_workspace_role("owner", "admin")),
                    user: dict = Depends(get_current_user)):
    try:
        return await create_order(workspace_id=workspace["id"], user_id=user["user_id"],
            provider_name=body.provider, **body.model_dump(exclude={"provider"}))
    except BillingError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        # Do not return provider payloads, API keys or network internals to browsers.
        raise HTTPException(502, detail={"code": "PAYMENT_CHECKOUT_FAILED",
            "message": "Unable to create checkout; retry with the same request key"}) from exc


@router.get("/orders/{order_id}")
async def get_order(order_id: str, workspace: dict = Depends(get_workspace), user: dict = Depends(get_current_user)):
    async with get_db_session() as db:
        order = await db.get(PaymentOrder, order_id)
        if order is None or order.workspace_id != workspace["id"] or order.user_id != user["user_id"]:
            raise HTTPException(404, "Order not found")
        return order_view(order)


@router.get("/orders")
async def orders(page: int = Query(1, ge=1, le=1_000_000), page_size: int = Query(10, ge=1, le=100),
                 status: Literal["pending", "paid", "cancelled"] | None = Query(None),
                 provider: str | None = Query(None, pattern=r"^[a-z0-9_-]{1,64}$"),
                 order_id: str | None = Query(None, min_length=1, max_length=64),
                 dates: UsageDateRange = Depends(usage_date_range),
                 workspace: dict = Depends(get_workspace), user: dict = Depends(get_current_user)):
    # The workspace shares credits, but each payer resumes only their own orders.
    async with get_db_session() as db:
        scope = [PaymentOrder.workspace_id == workspace["id"], PaymentOrder.user_id == user["user_id"]]
        if status:
            scope.append(PaymentOrder.status == status)
        if provider:
            scope.append(PaymentOrder.provider == provider)
        if order_id:
            scope.append(PaymentOrder.id.contains(order_id, autoescape=True))
        if dates.start is not None:
            scope.append(PaymentOrder.created_at >= dates.start)
        if dates.end is not None:
            scope.append(PaymentOrder.created_at < dates.end)
        total = await db.scalar(select(func.count()).select_from(PaymentOrder).where(*scope)) or 0
        rows = (await db.scalars(select(PaymentOrder).where(*scope)
            .order_by(PaymentOrder.created_at.desc(), PaymentOrder.id.desc())
            .offset((page - 1) * page_size).limit(page_size))).all()
        return {"page": page, "page_size": page_size, "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "items": [order_view(row) for row in rows]}


@router.post("/orders/{order_id}/checkout")
async def resume_checkout(order_id: str, workspace: dict = Depends(require_workspace_role("owner", "admin")),
                          user: dict = Depends(get_current_user)):
    try:
        return await continue_order(workspace_id=workspace["id"], user_id=user["user_id"], order_id=order_id)
    except BillingError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        raise HTTPException(502, detail={"code": "PAYMENT_CHECKOUT_FAILED",
            "message": "Unable to create checkout; retry this order"}) from exc


@router.post("/orders/{order_id}/app-checkout")
async def native_app_checkout(order_id: str,
                              workspace: dict = Depends(require_workspace_role("owner", "admin")),
                              user: dict = Depends(get_current_user)):
    try:
        return await create_app_checkout(workspace_id=workspace["id"], user_id=user["user_id"],
                                         order_id=order_id)
    except BillingError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        raise HTTPException(502, detail={"code": "PAYMENT_CHECKOUT_FAILED",
            "message": "Unable to create app checkout; retry this order"}) from exc


@router.post("/webhooks/{provider_name}")
async def webhook(provider_name: str, request: Request):
    # Public for the payment provider, authenticated by its adapter's signature.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65_536:
            raise HTTPException(413, "Webhook too large")
    try:
        provider = get_provider(provider_name)
        receipt = await provider.verify_webhook(bytes(body), request.headers)
        result = await apply_receipt(provider_name, receipt) if receipt is not None else {"accepted": True, "ignored": True}
        acknowledgement = getattr(provider, "webhook_acknowledgement", None)
        # Alipay requires exactly `success`, sent only after the ledger commit.
        return PlainTextResponse(acknowledgement) if acknowledgement else result
    except BillingError as exc:
        raise _error(exc) from exc
    except ValidationError as exc:
        raise HTTPException(422, "Invalid payment receipt") from exc


@router.post("/orders/{order_id}/refresh")
async def refresh_payment(order_id: str, workspace: dict = Depends(require_workspace_role("owner", "admin")),
                          user: dict = Depends(get_current_user)):
    try:
        return await refresh_order(workspace_id=workspace["id"], user_id=user["user_id"], order_id=order_id)
    except BillingError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        raise HTTPException(502, detail={"code": "PAYMENT_QUERY_FAILED", "message": "Unable to query payment status"}) from exc


@router.post("/orders/{order_id}/cancel")
async def cancel_payment(order_id: str, workspace: dict = Depends(require_workspace_role("owner", "admin")),
                         user: dict = Depends(get_current_user)):
    try:
        return await cancel_order(workspace_id=workspace["id"], user_id=user["user_id"], order_id=order_id)
    except BillingError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        raise HTTPException(502, detail={"code": "PAYMENT_CANCEL_FAILED", "message": "Unable to cancel payment"}) from exc
