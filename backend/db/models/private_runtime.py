"""Durable private execution identity, including guest actors on Wuying."""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class PrivateRuntimeBinding(Base):
    __tablename__ = "private_runtimes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="sandbox")
    isolation_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="process_uid")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="private_docker_v1")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provision_phase: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    container_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    workspace_volume: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    data_volume: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    volume_identities: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    image: Mapped[str] = mapped_column(String(256), nullable=False)
    image_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    api_key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    physical_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_identity: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "actor_user_id", "kind", "provider", name="uq_private_runtime_actor_kind"),
        CheckConstraint("kind IN ('sandbox','browser_profile')", name="ck_private_runtime_kind"),
        CheckConstraint("(provider = 'private_docker_v1' AND ((kind = 'sandbox' AND isolation_mode = 'process_uid') OR (kind = 'browser_profile' AND isolation_mode IN ('chromium_sandbox','container_uid')))) OR (provider = 'private_wuying_v1' AND ((kind = 'sandbox' AND isolation_mode = 'guest_uid_mount') OR (kind = 'browser_profile' AND isolation_mode = 'wuying_guest_uid')))", name="ck_private_runtime_isolation"),
        CheckConstraint("(provider = 'private_docker_v1' AND data_volume IS NOT NULL AND ((kind = 'sandbox' AND workspace_volume IS NOT NULL) OR (kind = 'browser_profile' AND workspace_volume IS NULL))) OR (provider = 'private_wuying_v1' AND workspace_volume IS NULL AND data_volume IS NULL)", name="ck_private_runtime_volumes"),
        CheckConstraint("status IN ('reserved','provisioning','ready','blocked')", name="ck_private_runtime_status"),
        CheckConstraint("revision > 0", name="ck_private_runtime_revision"),
        CheckConstraint("host_port IS NULL OR (host_port > 0 AND host_port < 65536)", name="ck_private_runtime_port"),
    )
