"""add_desktop_events

Operator timeline for cloud desktops: browser bring-ups, launches, runtime
checks/repairs, channel verifies, slow leases and diagnostic snapshots.

Revision ID: b6d1e2f3a4b5
Revises: a5c0d1e2f3a4
Create Date: 2026-09-07 22:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = 'b6d1e2f3a4b5'
down_revision: Union[str, None] = 'a5c0d1e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("desktop_id", sa.String(96), nullable=True),
        sa.Column("container_key", sa.String(128), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("detail", sa.Text().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("diag_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_desktop_events_ts", "desktop_events", ["ts"])
    op.create_index("ix_desktop_events_desktop_ts", "desktop_events", ["desktop_id", "ts"])
    op.create_index("ix_desktop_events_container_ts", "desktop_events", ["container_key", "ts"])
    op.create_index("ix_desktop_events_session_ts", "desktop_events", ["session_id", "ts"])
    op.create_index("ix_desktop_events_kind_ts", "desktop_events", ["kind", "ts"])


def downgrade() -> None:
    for name in (
        "ix_desktop_events_kind_ts", "ix_desktop_events_session_ts",
        "ix_desktop_events_container_ts", "ix_desktop_events_desktop_ts", "ix_desktop_events_ts",
    ):
        op.drop_index(name, table_name="desktop_events")
    op.drop_table("desktop_events")
