"""Durable personal skills and immutable snapshots published to the store."""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class UserSkill(Base):
    """A user-authored skill package.

    ``archive_*`` and the unprefixed listing fields are the owner's current
    draft, refreshed from their sandbox.  ``published_*`` is a separate,
    immutable release copied only by an explicit publish action.  Keeping the
    two snapshots apart means an export/download cannot silently replace or
    withdraw the package already visible in the community store.
    """

    __tablename__ = "user_skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    install_dir: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'unpublished'")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    archive_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_data: Mapped[dict] = mapped_column(JSONType, default=dict)

    # Public release snapshot.  These columns are nullable while the skill is
    # a private draft and are updated together by publish_personal_skill().
    published_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_install_dir: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    published_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_icon: Mapped[str | None] = mapped_column(String(16), nullable=True)
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_archive_data: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    published_archive_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    published_archive_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    published_metadata_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_user_skills_owner_name"),
        Index("ix_user_skills_owner_updated", "owner_id", "updated_at"),
        Index("ix_user_skills_status_published", "status", "published_at"),
    )
