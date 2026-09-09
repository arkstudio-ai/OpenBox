"""Third-party platform accounts bound to a workspace (the 授权中心 ledger).

One row per (workspace, platform, external account). Tokens are stored only
as AES-GCM ciphertext (core/crypto.py); the plaintext never leaves the
platforms/ package. `auth_kind` distinguishes open-platform OAuth (this
first cut, Douyin) from the desktop-cookie route the original A5 plan
described, so both can share one table and one page later.
"""
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
    )
    bound_by_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    #: Provider key from platforms/registry.py, e.g. "douyin".
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    #: "oauth" now; "desktop_cookie" reserved for the creator-centre route.
    auth_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'oauth'"))
    #: The platform's stable id for the person (Douyin open_id).
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    union_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: Scopes the person actually granted, comma separated.
    scopes: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    #: bound | expired | revoked
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="bound")
    access_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: desktop_cookie rows only: the cloud desktop whose Chrome profile holds
    #: the session. Changes when the workspace's desktop is rebuilt.
    desktop_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    #: desktop_cookie rows only: last probe, redacted (cookie names and expiry
    #: timestamps, probe status codes; never cookie values).
    probe_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Times renew_refresh_token has been used; Douyin allows five.
    renew_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_refresh_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Desktop auto-publish circuit breaker (plan §7 C3): set on any risk
    #: signal, cleared only by a person. While set, publishing degrades to the
    #: QR package route.
    auto_publish_disabled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    auto_publish_disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    bound_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        # Re-binding the same person replaces the live row; deleted rows stay
        # for history, so uniqueness applies to live rows only. Unit tests run
        # on SQLite, so both dialects need the predicate.
        Index(
            "uq_platform_accounts_live",
            "workspace_id",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        # One row per (desktop, site) for the cookie route; external_id is
        # only known after the first successful profile probe there.
        Index(
            "uq_platform_accounts_desktop",
            "workspace_id",
            "desktop_id",
            "platform",
            unique=True,
            postgresql_where=text("auth_kind = 'desktop_cookie' AND deleted_at IS NULL"),
            sqlite_where=text("auth_kind = 'desktop_cookie' AND deleted_at IS NULL"),
        ),
        Index("ix_platform_accounts_workspace", "workspace_id", "platform"),
        Index("ix_platform_accounts_access_due", "status", "access_expires_at"),
        Index("ix_platform_accounts_refresh_due", "status", "refresh_expires_at"),
    )
