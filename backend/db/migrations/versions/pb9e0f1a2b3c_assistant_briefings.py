"""Add the personal assistant's daily briefing setting (V2, P5).

One row per (user, workspace): enabled, local time and time zone, and the
local date of the last briefing requested. Downgrading drops the table.

Revision ID: pb9e0f1a2b3c
Revises: pb8d9e0f1a2b
"""
from alembic import op
import sqlalchemy as sa

revision = "pb9e0f1a2b3c"
down_revision = "pb8d9e0f1a2b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assistant_briefings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("local_time", sa.String(5), nullable=False),
        sa.Column("time_zone", sa.String(64), nullable=False),
        sa.Column("last_sent_on", sa.String(10), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "workspace_id", name="uq_assistant_briefings_user_workspace"),
        sa.CheckConstraint("revision >= 1", name="ck_assistant_briefings_revision"),
        sa.CheckConstraint("length(local_time) = 5", name="ck_assistant_briefings_local_time"))


def downgrade():
    op.drop_table("assistant_briefings")
