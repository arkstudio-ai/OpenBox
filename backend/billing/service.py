"""Transactional ledger. The balance row serializes all writes for a workspace."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.pricing import catalogue, normalize_usage, quote
from core.identifier import ascending as generate_id
from db.base import get_db_session
from db.models.billing import CreditBalance, CreditLedger, UsageEvent
from db.models.session import Session


class BillingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def billing_mode() -> str:
    mode = os.environ.get("BILLING_MODE", "shadow").lower()
    if mode not in {"off", "shadow", "enforce"}:
        raise ValueError("BILLING_MODE must be off, shadow or enforce")
    return mode


def now() -> datetime:
    return datetime.now(timezone.utc)


async def lock_balance(db: AsyncSession, workspace_id: str) -> CreditBalance:
    # Upsert handles first-use races; locking also serializes idempotency checks.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await db.execute(insert(CreditBalance).values(
        workspace_id=workspace_id, balance=Decimal(0), updated_at=now(),
    ).on_conflict_do_nothing(index_elements=["workspace_id"]))
    return (await db.scalars(select(CreditBalance).where(
        CreditBalance.workspace_id == workspace_id,
    ).with_for_update().execution_options(populate_existing=True))).one()


def post_ledger(db: AsyncSession, account: CreditBalance, *, amount: Decimal,
                kind: str, reference_id: str, key: str) -> None:
    """Caller owns the balance lock and idempotency check; commit together."""
    account.balance += amount
    account.updated_at = now()
    db.add(CreditLedger(id=generate_id("ledger"), workspace_id=account.workspace_id,
                        idempotency_key=key, kind=kind, amount=amount,
                        balance_after=account.balance, reference_id=reference_id, created_at=now()))


@dataclass
class UsageMeter:
    event_id: str
    workspace_id: str
    model_id: str
    started_at: datetime
    rates: dict
    mode: str
    finished: bool = False

    @classmethod
    async def start(cls, *, model_id: str, session_id: str, user_id: str = "",
                    message_id: str = "", kind: str = "chat", pricing_rates: dict | None = None) -> UsageMeter | None:
        mode = billing_mode()
        if mode == "off":
            return None
        rates, started = pricing_rates if pricing_rates is not None else catalogue(), now()
        from agent.driver import current_run_fence
        fence = current_run_fence()
        request_fence = ({"run_id": fence[1], "generation": fence[2]}
                         if fence is not None and fence[0] == session_id else None)
        async with get_db_session() as db:
            session = await db.get(Session, session_id)
            if session is None:
                # Unscoped desktop utilities do not have a billable workspace.
                if mode == "enforce":
                    raise BillingError("BILLING_SESSION_REQUIRED", "A billable session is required")
                return None
            workspace_id = session.workspace_id  # never an HTTP workspace header
            account = await lock_balance(db, workspace_id)
            from billing.subscriptions import ensure_period_allowance
            await ensure_period_allowance(db, account, started)
            preview = quote(model_id, {"input": 0, "output": 0}, at=started, rates=rates)
            if mode == "enforce":
                if preview.credits is None:
                    raise BillingError("MODEL_UNPRICED", "This model has no verified credit price")
                if account.balance <= 0:
                    raise BillingError("INSUFFICIENT_CREDITS", "积分不足，请先充值后继续")
            event_id = generate_id("usage")
            from team.usage import request_attribution
            team_attribution = await request_attribution(db, session)
            db.add(UsageEvent(id=event_id, idempotency_key=f"llm:{event_id}",
                workspace_id=workspace_id, user_id=user_id or session.user_id,
                session_id=session_id, message_id=message_id or None,
                session_title=session.title or session_id, model_id=model_id, kind=kind,
                tokens={}, total_tokens=0, credits=None, status="pending",
                pricing={**preview.snapshot, **({"request_fence": request_fence} if request_fence else {}),
                    **({"team_attribution": team_attribution} if team_attribution else {})},
                created_at=started))
        return cls(event_id, workspace_id, model_id, started, rates, mode)

    async def finish(self, usage: dict | None) -> Decimal | None:
        """Exactly one settlement, including cancellation after provider usage arrived.

        Missing provider usage remains visibly unreported, never a zero-priced call.
        Enforce is post-call settlement: in-flight calls can leave a negative balance;
        the next call is blocked. No token consumption is silently forgiven.
        """
        normalized = normalize_usage(usage) if usage else None
        price = quote(self.model_id, normalized, at=self.started_at, rates=self.rates) if normalized else None
        async with get_db_session() as db:
            account = await lock_balance(db, self.workspace_id)
            event = await db.get(UsageEvent, self.event_id)
            if event.status != "pending":
                self.finished = True
                return event.credits
            if normalized is None:
                event.status = "unreported"
            else:
                event.tokens = normalized
                event.total_tokens = normalized["total"]
                event.credits = price.credits
                metadata = {key: value for key, value in (event.pricing or {}).items()
                    if key in {"request_fence", "team_attribution"}}
                event.pricing = {**price.snapshot, **metadata}
                event.status = "unpriced" if price.credits is None else "charged" if self.mode == "enforce" else "shadow"
                if event.status == "charged":
                    post_ledger(db, account, amount=-price.credits, kind="usage",
                                reference_id=event.id, key=f"usage:{event.id}")
        self.finished = True
        return price.credits if price else None
