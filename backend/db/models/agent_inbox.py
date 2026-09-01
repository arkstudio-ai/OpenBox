"""Durable user input queued at main-Agent turn and step boundaries."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class AgentInboxItem(Base):
    """One tenant-owned prompt awaiting an exact Agent boundary.

    ``messages`` remains the public transcript.  Inbox rows are private kernel
    state: acceptance is durable before a driver is reserved, while claiming
    atomically materializes the public Message/Parts and binds the exact driver
    generation that is allowed to consume them.
    """

    __tablename__ = "agent_inbox_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list] = mapped_column(JSONType, nullable=False)
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    video_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    variant: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output_format: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="accepted",
    )
    message_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    result_message_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    delivery_last_error: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    accepted_at: Mapped[datetime] = mapped_column(nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "delivery IN ('followup', 'steer', 'inject')",
            name="ck_agent_inbox_delivery",
        ),
        CheckConstraint(
            "target IN ('next-turn', 'next-step')",
            name="ck_agent_inbox_target",
        ),
        CheckConstraint(
            "(delivery = 'followup' AND target = 'next-turn') OR "
            "(delivery IN ('steer', 'inject') AND target = 'next-step')",
            name="ck_agent_inbox_delivery_target",
        ),
        CheckConstraint(
            "state IN ('accepted', 'claimed', 'canceled', 'settled')",
            name="ck_agent_inbox_state",
        ),
        CheckConstraint(
            "length(prompt) BETWEEN 1 AND 65536",
            name="ck_agent_inbox_prompt_bounds",
        ),
        CheckConstraint(
            "length(request_digest) = 64",
            name="ck_agent_inbox_request_digest",
        ),
        CheckConstraint(
            "delivery_attempts BETWEEN 0 AND 1000",
            name="ck_agent_inbox_delivery_attempts",
        ),
        CheckConstraint(
            "(run_id IS NULL AND generation IS NULL) OR "
            "(run_id IS NOT NULL AND generation IS NOT NULL AND generation > 0)",
            name="ck_agent_inbox_run_generation_pair",
        ),
        CheckConstraint(
            "(state = 'accepted' AND message_id IS NULL AND run_id IS NULL "
            "AND generation IS NULL AND claim_token IS NULL AND claimed_at IS NULL) OR "
            "(state = 'canceled' AND message_id IS NULL AND run_id IS NULL "
            "AND generation IS NULL AND canceled_at IS NOT NULL) OR "
            "(state = 'claimed' AND message_id IS NOT NULL AND run_id IS NOT NULL "
            "AND generation IS NOT NULL AND turn_id IS NOT NULL AND step_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND claim_owner IS NOT NULL "
            "AND claim_expires_at IS NOT NULL AND claimed_at IS NOT NULL) OR "
            "(state = 'settled' AND run_id IS NOT NULL AND generation IS NOT NULL "
            "AND turn_id IS NOT NULL AND step_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND claim_owner IS NOT NULL "
            "AND claim_expires_at IS NULL AND claimed_at IS NOT NULL "
            "AND settled_at IS NOT NULL)",
            name="ck_agent_inbox_state_shape",
        ),
        UniqueConstraint(
            "user_id",
            "session_id",
            "client_id",
            name="uq_agent_inbox_client_id",
        ),
        UniqueConstraint("message_id", name="uq_agent_inbox_message"),
        Index(
            "ix_agent_inbox_session_queue",
            "session_id",
            "user_id",
            "state",
            "target",
            "created_at",
            "id",
        ),
        Index(
            "ix_agent_inbox_claim_recovery",
            "state",
            "claim_expires_at",
            "created_at",
            "id",
        ),
        Index("ix_agent_inbox_run", "session_id", "run_id", "generation"),
        Index("ix_agent_inbox_user_created", "user_id", "created_at"),
    )
