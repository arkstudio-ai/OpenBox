"""API-hidden provider transcript and tool reveal events.

These rows intentionally do not share the public ``parts`` table. Public
message loaders never import or join this model. They are a compatibility
read model for narrow ``session.internal_parts`` helpers; canonical provider
replay is projected from API-hidden Agent-event sidecars for model dispatch.
"""
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class InternalPart(Base):
    __tablename__ = "internal_parts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    capability_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_chain_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stream_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    origin_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # A per-session semantic idempotency key.  It is nullable for provider
    # blocks whose protocol does not expose a stable event identifier.
    dedupe_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "origin_seq", name="uq_internal_parts_session_origin"),
        UniqueConstraint("session_id", "dedupe_key", name="uq_internal_parts_session_dedupe"),
        Index("ix_internal_parts_message_stream", "message_id", "stream_seq"),
        Index("ix_internal_parts_session_kind_origin", "session_id", "kind", "origin_seq"),
        Index(
            "ix_internal_parts_replay_binding",
            "session_id",
            "capability_key_digest",
            "response_chain_id",
            "stream_seq",
        ),
        Index("ix_internal_parts_user", "user_id"),
    )
