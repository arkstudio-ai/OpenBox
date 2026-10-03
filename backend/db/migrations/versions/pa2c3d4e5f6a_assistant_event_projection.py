"""Durable projection of execution changes into the private main event log."""
from alembic import op
import sqlalchemy as sa

revision = "pa2c3d4e5f6a"
down_revision = "pa1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assistant_event_projections",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("assistant_tasks.id"), primary_key=True),
        sa.Column("assistant_session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_sequence >= 0", name="ck_assistant_projection_sequence"))
    op.create_index("ix_assistant_projection_scan", "assistant_event_projections", ["updated_at", "task_id"])


def downgrade():
    op.drop_index("ix_assistant_projection_scan", table_name="assistant_event_projections")
    op.drop_table("assistant_event_projections")
