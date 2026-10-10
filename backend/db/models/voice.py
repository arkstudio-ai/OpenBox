"""Voice calls with the personal assistant and the turns they handed to it.

A call is its own cost ledger (usage and estimate from the provider's
``response.done``) plus a short summary of what was said. A turn is one
``assistant_ask``: the user's words are a main-session message, so no audio
and no separate conversation is stored.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType

CALL_STATUSES = ("active", "ended", "failed", "limit")
TURN_OUTCOMES = ("pending", "delivered", "late", "timeout", "failed", "cancelled")


class VoiceCall(Base):
    __tablename__ = "voice_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    main_session_id: Mapped[str] = mapped_column(String(64), ForeignKey("sessions.id"), nullable=False)
    client: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # Enrolled voice IDs contain the model name, a prefix and a generated suffix.
    voice: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # The client's ended.reason vocabulary.
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # {"input_text": .., "input_audio": .., "output_text": .., "output_audio": ..} tokens.
    usage: Mapped[dict] = mapped_column(JSONType, nullable=False, server_default=text("'{}'"))
    # Decimal string, as the meter reports it.
    estimated_yuan: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'0'"))
    unreported_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    price_date: Mapped[str] = mapped_column(String(10), nullable=False)
    # What the call was about, written after hang-up; the next call's greeting reads it.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_voice_calls_owner", "user_id", "workspace_id", "started_at"),
        Index("ix_voice_calls_active", "user_id", "status"),
        CheckConstraint("status IN ('active','ended','failed','limit')", name="ck_voice_calls_status"),
    )


class VoiceTurn(Base):
    __tablename__ = "voice_turns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), ForeignKey("voice_calls.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    # The provider's call_id, unique within a call.
    provider_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inbox_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # What the user said; the main session's message is the source of truth.
    transcript: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))

    __table_args__ = (
        UniqueConstraint("call_id", "provider_call_id", name="uq_voice_turn_call"),
        Index("ix_voice_turns_call", "call_id", "requested_at"),
        CheckConstraint("outcome IN ('pending','delivered','late','timeout','failed','cancelled')",
                        name="ck_voice_turns_outcome"),
    )
