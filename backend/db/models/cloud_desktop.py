"""Cloud desktops table ORM model — one ECD desktop per workspace."""
from datetime import datetime

from sqlalchemy import String, Boolean, Text, Index, ForeignKey, Integer, DateTime, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class CloudDesktop(Base):
    __tablename__ = "cloud_desktops"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
    )
    # The user who initiated provisioning. Ownership belongs to workspace_id.
    user_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("users.id"), nullable=True)
    # Null while CreateDesktops has not returned yet (status="creating").
    desktop_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    end_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # creating | running | starting | stopped | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    charge_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-desktop execution channel.  Secret material is never stored in
    # plaintext: the hash supports diagnostics and the ciphertext is decrypted
    # only while constructing a SandboxClient.
    channel_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    private_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tunnel_port: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    tunnel_bind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tunnel_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    tunnel_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    action_api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    tunnel_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    channel_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        # One live desktop per workspace; history rows keep is_deleted=true.
        Index(
            "ix_cloud_desktops_workspace_active",
            "workspace_id",
            unique=True,
            postgresql_where=text("is_deleted = false"),
            sqlite_where=text("is_deleted = false"),
        ),
        Index("ix_cloud_desktops_desktop_id", "desktop_id"),
    )
