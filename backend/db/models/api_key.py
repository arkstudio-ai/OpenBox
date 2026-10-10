"""API keys: machine credentials for the public ``/v1`` harness API.

A key is bound to one user *and* one workspace, so a request carrying it
never needs ``X-Workspace-Id``. Only the SHA-256 of the secret is stored; the
prefix is kept so an operator can tell keys apart in a listing without ever
seeing the secret again.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: ``obx_sk_`` plus the first characters of the secret, for listings.
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Hex SHA-256 of the full secret. Unique so a lookup is one indexed read.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: ``sessions:read`` ``sessions:write`` ``files:read`` ``files:write``.
    scopes: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: Unattended-interaction policy plus per-key limits; see
    #: ``auth.api_key.DEFAULT_POLICY`` for the keys and their defaults.
    policy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: ``"60/minute"`` style; NULL uses ``config.rate_limit_api``.
    rate_limit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_api_keys_user_active", "user_id", "revoked_at"),
        Index("ix_api_keys_workspace", "workspace_id"),
    )
