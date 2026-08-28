"""Content-addressed cache of completed image generations.

Kept separate from file_assets so the asset ledger stays a pure ownership
record; a cache row points at the producing user's asset and lets an
identical later request (any user) reuse the stored object via OSS
server-side copy instead of a paid provider call.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class ImageGenCache(Base):
    __tablename__ = "image_gen_cache"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Producer, for audit only — lookups are by fingerprint across users.
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(16), nullable=False)  # generate | edit
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    request_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    asset_id: Mapped[str] = mapped_column(String(64), ForeignKey("file_assets.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_image_gen_cache_fingerprint", "fingerprint", "created_at"),
    )
