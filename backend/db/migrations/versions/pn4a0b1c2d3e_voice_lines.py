"""voice_lines: a call as it was said, for the dispatch metrics (docs/VOICE_DISPATCH_PLAN_20261112.md M0).

Written only while ``voice.line_log`` is on.

Revision ID: pn4a0b1c2d3e
Revises: pm39f0a1b2c3
"""
from alembic import op
import sqlalchemy as sa

revision = "pn4a0b1c2d3e"
down_revision = "pm39f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "voice_lines",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("call_id", sa.String(64), sa.ForeignKey("voice_calls.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("item_id", sa.String(64), nullable=False, server_default=""),
        sa.CheckConstraint("role IN ('user','assistant','note','event')", name="ck_voice_lines_role"),
    )
    op.create_index("ix_voice_lines_call", "voice_lines", ["call_id", "seq"])
    op.create_index("ix_voice_lines_at", "voice_lines", ["at"])


def downgrade():
    op.drop_index("ix_voice_lines_at", table_name="voice_lines")
    op.drop_index("ix_voice_lines_call", table_name="voice_lines")
    op.drop_table("voice_lines")
