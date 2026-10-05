"""Pinned finite browser identity and the Sessions that actually used it."""
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class BrowserResourceBinding(Base):
    __tablename__ = "browser_resource_bindings"

    resource_id: Mapped[str] = mapped_column(ForeignKey("resource_control_leases.id"), primary_key=True)
    private_runtime_id: Mapped[str] = mapped_column(ForeignKey("private_runtimes.id"), unique=True, nullable=False)
    runtime_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="private_docker_v1")
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    assistant_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    identity: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("actor_user_id", "workspace_id", "provider", name="uq_browser_resource_actor"),)


class BrowserResourceSession(Base):
    __tablename__ = "browser_resource_sessions"

    resource_id: Mapped[str] = mapped_column(ForeignKey("browser_resource_bindings.resource_id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
