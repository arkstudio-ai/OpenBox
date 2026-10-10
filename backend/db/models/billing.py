"""Durable workspace accounts; usage and payment history survive session deletion."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType
from core.identifier import ascending

CREDITS = Numeric(28, 12)


class CreditBalance(Base):
    __tablename__ = "credit_balances"
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(CREDITS, nullable=False, default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_title: Mapped[str] = mapped_column(String(255), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tokens: Mapped[dict] = mapped_column(JSONType, nullable=False)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credits: Mapped[Decimal | None] = mapped_column(CREDITS, nullable=True)
    # charged | shadow | historical | unpriced. NULL price never means free.
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    pricing: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        Index("ix_usage_workspace_created", "workspace_id", "created_at", "id"),
        Index("ix_usage_message", "message_id"),
        CheckConstraint("total_tokens >= 0", name="ck_usage_tokens_nonnegative"),
        CheckConstraint("credits IS NULL OR credits >= 0", name="ck_usage_credits_nonnegative"),
    )


class CreditLedger(Base):
    __tablename__ = "credit_ledger"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(CREDITS, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(CREDITS, nullable=False)
    reference_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (Index("ix_ledger_workspace_created", "workspace_id", "created_at", "id"),)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="topup", server_default="topup")
    product: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    credits: Mapped[Decimal] = mapped_column(CREDITS, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    checkout_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # The provider's *payment* id is globally unique within that provider.
    provider_payment_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    __table_args__ = (
        UniqueConstraint("workspace_id", "request_key", name="uq_payment_request"),
        UniqueConstraint("provider", "provider_payment_id", name="uq_provider_payment"),
        CheckConstraint("amount_fen > 0 AND credits > 0", name="ck_payment_positive"),
        Index("ix_payment_workspace_created", "workspace_id", "created_at"),
    )


class PaymentOrderRequest(Base):
    """Every retry key keeps pointing to its order, including after settlement."""
    __tablename__ = "payment_order_requests"
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), ForeignKey("payment_orders.id"), nullable=False)


class BillingSubscription(Base):
    __tablename__ = "billing_subscriptions"
    # Paid terms keep their order ID as the primary key for existing consumers.
    # Operator grants have their own identity and never fabricate paid orders.
    id: Mapped[str] = mapped_column(String(64), primary_key=True,
        default=lambda context: context.get_current_parameters().get("order_id") or ascending("subscription"))
    order_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("payment_orders.id"), nullable=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(24), nullable=False)
    cycle: Mapped[str] = mapped_column(String(16), nullable=False)
    plan: Mapped[dict] = mapped_column(JSONType, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime] = mapped_column(nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_subscription_order"),
        CheckConstraint("ends_at > starts_at", name="ck_subscription_dates"),
        Index("ix_subscription_workspace_dates", "workspace_id", "starts_at", "ends_at"),
    )
