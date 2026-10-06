"""Per-user project briefs: standing notes about one project.

One brief per (user, project), written by the user or their personal
assistant and carried in the system prompt of the owner's ordinary sessions in
that project. See project/brief.py.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ProjectBrief(Base):
    __tablename__ = "project_briefs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # "user" | "assistant": who wrote the current revision.
    updated_by: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_project_briefs_user_project"),
        CheckConstraint("revision >= 1", name="ck_project_briefs_revision"),
        CheckConstraint("updated_by IN ('user', 'assistant')", name="ck_project_briefs_updated_by"),
        CheckConstraint("length(content) <= 6000", name="ck_project_briefs_content_length"),
    )
