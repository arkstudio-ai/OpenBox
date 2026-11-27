"""One immutable, independently reviewed edition of a creator's video template."""
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class VideoTemplateEdition(Base):
    __tablename__ = "video_template_editions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    skill_id: Mapped[str] = mapped_column(ForeignKey("user_skills.id"), nullable=False, unique=True)
    source_asset_id: Mapped[str] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONType, nullable=False)
    preview_key: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_video_template_edition"),)
