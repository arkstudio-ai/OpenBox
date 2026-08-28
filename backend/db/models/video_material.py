"""Durable TokenSpace material groups and provider-side asset bindings."""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class VideoMaterialGroup(Base):
    """One user-owned provider material group.

    ``AIGC`` groups are created silently for ordinary references. A
    ``LivenessFace`` group begins as a short-lived authorization session and
    becomes active only after the person completes the provider H5 flow.
    Provider session tokens never leave the backend and are cleared once the
    session reaches a terminal state.
    """

    __tablename__ = "video_material_groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    project_name: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=text("'default'")
    )
    group_type: Mapped[str] = mapped_column(String(24), nullable=False)  # AIGC | LivenessFace
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_group_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    qr_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "group_type",
            "label",
            name="uq_video_material_groups_user_label",
        ),
        UniqueConstraint(
            "provider", "provider_group_id", name="uq_video_material_groups_provider_id"
        ),
        Index("ix_video_material_groups_user_updated", "user_id", "updated_at"),
        Index("ix_video_material_groups_status", "status", "updated_at"),
    )


class VideoMaterialAsset(Base):
    """A user OSS asset materialized inside one provider material group."""

    __tablename__ = "video_material_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    group_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("video_material_groups.id"), nullable=False
    )
    source_asset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("file_assets.id"), nullable=False
    )
    provider_asset_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "source_asset_id", name="uq_video_material_asset_source"),
        UniqueConstraint(
            "provider_asset_id", name="uq_video_material_assets_provider_id"
        ),
        Index("ix_video_material_assets_user_updated", "user_id", "updated_at"),
        Index("ix_video_material_assets_source", "source_asset_id"),
    )
