"""Database-backed state for process-independent internal maintenance tasks."""
from datetime import datetime

from sqlalchemy import Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class InternalTaskState(Base):
    __tablename__ = "internal_task_state"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    running_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    backoff_until: Mapped[datetime | None] = mapped_column(nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
