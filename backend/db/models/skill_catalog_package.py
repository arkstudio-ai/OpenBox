"""Durable operator packages/metadata and deletion tombstones for the store."""

from datetime import datetime

from sqlalchemy import Boolean, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SkillCatalogPackage(Base):
    __tablename__ = "skill_catalog_packages"

    catalog_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    definition: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # Deferred: listing metadata must never read every ZIP into memory.
    archive_data: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
