"""Add per-user cloud desktops (Wuying ECD per_user mode).

Revision ID: b2d4f6a8c0e2
Revises: a1b3c5d7e9f2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d4f6a8c0e2"
down_revision: Union[str, None] = "a1b3c5d7e9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cloud_desktops",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("desktop_id", sa.String(length=96), nullable=True),
        sa.Column("end_user_id", sa.String(length=64), nullable=True),
        sa.Column("region_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cloud_desktops_user_active",
        "cloud_desktops",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
        sqlite_where=sa.text("is_deleted = false"),
    )
    op.create_index("ix_cloud_desktops_desktop_id", "cloud_desktops", ["desktop_id"])


def downgrade() -> None:
    op.drop_index("ix_cloud_desktops_desktop_id", table_name="cloud_desktops")
    op.drop_index("ix_cloud_desktops_user_active", table_name="cloud_desktops")
    op.drop_table("cloud_desktops")
