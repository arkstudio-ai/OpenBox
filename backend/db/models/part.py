"""Parts table ORM model (single-table polymorphic)."""
from datetime import datetime

from sqlalchemy import String, Index, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


PRIVATE_TOOL_PART_FIELDS = frozenset({
    "canonical_tool_id",
    "wire_tool_name",
    "provider_binding_digest",
    "provider_dialect",
    "stream_seq",
})


def public_part_data(data: dict) -> dict:
    """Drop reserved replay identity keys from public JSON projections."""
    return {key: value for key, value in data.items() if key not in PRIVATE_TOOL_PART_FIELDS}


class Part(Base):
    __tablename__ = "parts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), ForeignKey("messages.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    # Ordered together with API-hidden provider transcript parts.  Existing
    # rows stay nullable; adapters assign it only when exact replay ordering is
    # required.
    stream_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Security identity and provider wire identity are deliberately separate.
    # They live outside ``data`` so ordinary transcript/API reads cannot leak
    # account binding or accidentally authorize by a provider-visible alias.
    canonical_tool_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wire_tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_binding_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_dialect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_parts_message", "message_id"),
        Index("ix_parts_message_created", "message_id", "created_at"),
        Index("ix_parts_message_stream", "message_id", "stream_seq"),
        Index("ix_parts_canonical_tool", "session_id", "canonical_tool_id"),
        Index("ix_parts_session_type", "session_id", "type"),
        Index("ix_parts_session_type_created", "session_id", "type", "created_at"),
        Index("ix_parts_user", "user_id"),
    )
