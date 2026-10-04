"""Physical-resource control; operation drainage belongs to ExternalEffect."""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class ResourceControlLease(Base):
    __tablename__ = "resource_control_leases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # Provider physical identity, never a Session ID or mutable tunnel URL.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    physical_id: Mapped[str] = mapped_column(String(160), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    desktop_record_id: Mapped[str | None] = mapped_column(ForeignKey("cloud_desktops.id"), nullable=True)
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    admission_state: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_observation_ref: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("uq_resource_control_physical", "provider", "resource_type", "physical_id", unique=True),
        Index("ix_resource_control_workspace", "workspace_id", "status"),
        CheckConstraint("epoch > 0", name="ck_resource_control_epoch"),
        CheckConstraint("owner_kind IN ('automation', 'human')", name="ck_resource_control_owner"),
        CheckConstraint("status IN ('active', 'draining', 'hold')", name="ck_resource_control_status"),
        CheckConstraint("admission_state IN ('open', 'closed')", name="ck_resource_control_admission"),
        CheckConstraint("status = 'active' OR admission_state = 'closed'", name="ck_resource_control_hold"),
        CheckConstraint("owner_kind != 'human' OR expires_at IS NOT NULL", name="ck_resource_control_human_expiry"),
    )
