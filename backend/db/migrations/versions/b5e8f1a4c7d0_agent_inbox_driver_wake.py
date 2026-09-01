"""Add durable main-Agent inbox and driver wake state.

Revision ID: b5e8f1a4c7d0
Revises: a4d7f0c2e9b1
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b5e8f1a4c7d0"
down_revision: Union[str, None] = "a4d7f0c2e9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "agent_inbox_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("delivery", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("attachments", _json_type(), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("video_model", sa.String(length=128), nullable=True),
        sa.Column("variant", sa.String(length=32), nullable=True),
        sa.Column("output_format", _json_type(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="accepted", nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=True),
        sa.Column("result_message_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("turn_id", sa.String(length=64), nullable=True),
        sa.Column("step_id", sa.String(length=128), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_owner", sa.String(length=160), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("error", _json_type(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "delivery IN ('followup', 'steer', 'inject')",
            name="ck_agent_inbox_delivery",
        ),
        sa.CheckConstraint(
            "target IN ('next-turn', 'next-step')",
            name="ck_agent_inbox_target",
        ),
        sa.CheckConstraint(
            "(delivery = 'followup' AND target = 'next-turn') OR "
            "(delivery IN ('steer', 'inject') AND target = 'next-step')",
            name="ck_agent_inbox_delivery_target",
        ),
        sa.CheckConstraint(
            "state IN ('accepted', 'claimed', 'canceled', 'settled')",
            name="ck_agent_inbox_state",
        ),
        sa.CheckConstraint(
            "length(prompt) BETWEEN 1 AND 65536",
            name="ck_agent_inbox_prompt_bounds",
        ),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name="ck_agent_inbox_request_digest",
        ),
        sa.CheckConstraint(
            "(run_id IS NULL AND generation IS NULL) OR "
            "(run_id IS NOT NULL AND generation IS NOT NULL AND generation > 0)",
            name="ck_agent_inbox_run_generation_pair",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["result_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "session_id", "client_id",
            name="uq_agent_inbox_client_id",
        ),
        sa.UniqueConstraint("message_id", name="uq_agent_inbox_message"),
    )
    op.create_index(
        "ix_agent_inbox_session_queue",
        "agent_inbox_items",
        ["session_id", "user_id", "state", "target", "created_at", "id"],
    )
    op.create_index(
        "ix_agent_inbox_claim_recovery",
        "agent_inbox_items",
        ["state", "claim_expires_at", "created_at", "id"],
    )
    op.create_index(
        "ix_agent_inbox_run",
        "agent_inbox_items",
        ["session_id", "run_id", "generation"],
    )
    op.create_index(
        "ix_agent_inbox_user_created",
        "agent_inbox_items",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM agent_inbox_items")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "agent inbox downgrade refused: durable input still exists"
        )
    op.drop_index("ix_agent_inbox_user_created", table_name="agent_inbox_items")
    op.drop_index("ix_agent_inbox_run", table_name="agent_inbox_items")
    op.drop_index("ix_agent_inbox_claim_recovery", table_name="agent_inbox_items")
    op.drop_index("ix_agent_inbox_session_queue", table_name="agent_inbox_items")
    op.drop_table("agent_inbox_items")
