"""Per-user skill enable/disable and settings overlay.

No row means the manifest's defaultEnabled applies. Disabling hides the skill
from the agent and refuses new jobs; admitted jobs keep running unless the
user cancels them individually.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class UserSkillSetting(Base):
    __tablename__ = "user_skill_settings"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), primary_key=True
    )
    skill_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    settings_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
