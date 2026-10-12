"""Partner (API-key) tariffs and their monthly ladder rebates.

A partner workspace is billed from a *tariff*: per-tier base prices at a
default resolution, resolution multipliers, and cumulative monthly ladders.
Runtime charges ladder 1; the ladder discount is settled once a month as a
rebate credit, so every call still pays for its own upstream cost first.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class PartnerTariff(Base):
    __tablename__ = "partner_tariffs"
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), primary_key=True)
    tariff: Mapped[dict] = mapped_column(JSONType, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class PartnerRebate(Base):
    __tablename__ = "partner_rebates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)          # YYYY-MM
    status: Mapped[str] = mapped_column(String(16), nullable=False)        # settled
    charged: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    ladder_credits: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    rebate: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONType, nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ledger_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (UniqueConstraint("workspace_id", "month", name="uq_partner_rebate_month"),)
