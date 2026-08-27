"""Provenance for community skills installed by a user."""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class SkillInstall(Base):
    __tablename__ = "skill_installs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    user_skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("user_skills.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    install_dir: Mapped[str] = mapped_column(String(64), nullable=False)
    installed_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "install_dir", name="uq_skill_installs_user_dir"),
        Index("ix_skill_installs_user", "user_id", "installed_at"),
    )
