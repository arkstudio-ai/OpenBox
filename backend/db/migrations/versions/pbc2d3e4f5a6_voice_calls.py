"""Add voice calls with the personal assistant and their assistant turns.

``voice_calls`` is the call's own cost ledger; ``voice_turns`` records each
``assistant_ask`` handed to the main session. Timestamps carry their time zone
from the start (see pbb1c2d3e4f5). No audio is stored. Downgrading drops both.

Revision ID: pbc2d3e4f5a6
Revises: pbb1c2d3e4f5
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pbc2d3e4f5a6"
down_revision = "pbb1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("voice_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("main_session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("client", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("voice", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("end_reason", sa.String(24), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("turns", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("usage", postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("estimated_yuan", sa.String(16), nullable=False, server_default=sa.text("'0'")),
        sa.Column("unreported_rounds", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("price_date", sa.String(10), nullable=False),
        sa.CheckConstraint("status IN ('active','ended','failed','limit')", name="ck_voice_calls_status"))
    op.create_index("ix_voice_calls_owner", "voice_calls", ["user_id", "workspace_id", "started_at"])
    op.create_index("ix_voice_calls_active", "voice_calls", ["user_id", "status"])
    op.create_table("voice_turns",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("call_id", sa.String(64), sa.ForeignKey("voice_calls.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider_call_id", sa.String(64), nullable=False),
        sa.Column("inbox_id", sa.String(64), nullable=True),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("result_message_id", sa.String(64), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.UniqueConstraint("call_id", "provider_call_id", name="uq_voice_turn_call"),
        sa.CheckConstraint("outcome IN ('pending','delivered','late','timeout','failed','cancelled')",
                           name="ck_voice_turns_outcome"))
    op.create_index("ix_voice_turns_call", "voice_turns", ["call_id", "requested_at"])


def downgrade():
    op.drop_index("ix_voice_turns_call", table_name="voice_turns")
    op.drop_table("voice_turns")
    op.drop_index("ix_voice_calls_active", table_name="voice_calls")
    op.drop_index("ix_voice_calls_owner", table_name="voice_calls")
    op.drop_table("voice_calls")
