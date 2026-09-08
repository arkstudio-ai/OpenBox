"""Durable personal skills and immutable snapshots published to the store."""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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

    Two axes decide whether the store shows the package, and they belong to
    different people on purpose:

    * ``status`` is the author's: ``unpublished`` -> ``published`` when they
      submit a release, and ``withdrawn`` when they take it back (the
      ``published_*`` snapshot survives a withdrawal so the same release can be
      re-submitted and so moderation history stays readable).
    * ``listing`` is the operator's: ``pending`` / ``listed`` / ``rejected`` /
      ``delisted``.

    They are deliberately orthogonal rather than one merged column.  A single
    column would make "publish a new version" overwrite an operator's decision,
    so an author could shake off a delisting simply by pushing an update.  The
    store shows a package only when ``status == 'published' and
    listing == 'listed'``.
    """

    __tablename__ = "user_skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
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

    # Moderation state, written only by the admin console (and by publish,
    # which computes the entry state from SKILL_STORE_REVIEW).  The default is
    # 'listed' so a deployment that never turns review on behaves exactly as it
    # did before this column existed.
    listing: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'listed'")
    )
    #: Reject / delist reason.  Shown to the author, so it is written for them.
    listing_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    listing_changed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listing_changed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: Published by an admin, or promoted by hand afterwards.
    is_official: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

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
        UniqueConstraint(
            "workspace_id", "owner_id", "name",
            name="uq_user_skills_workspace_owner_name",
        ),
        Index("ix_user_skills_owner_updated", "owner_id", "updated_at"),
        Index("ix_user_skills_workspace_updated", "workspace_id", "updated_at"),
        Index("ix_user_skills_status_published", "status", "published_at"),
        # The store browses by listing, the review queue by listing plus
        # submission order; both read this index instead of the status one.
        Index("ix_user_skills_listing_published", "listing", "published_at"),
    )
