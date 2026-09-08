"""platform_accounts_desktop

授权中心二期: desktop-cookie rows carry the desktop id and a redacted probe
detail; one live row per (workspace, desktop, site).

Revision ID: b8e3f5a7c9d1
Revises: b6d1e2f3a4b5
Create Date: 2026-09-08 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e3f5a7c9d1'
down_revision: Union[str, None] = 'b6d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("platform_accounts", sa.Column("desktop_id", sa.String(96), nullable=True))
    op.add_column("platform_accounts", sa.Column("probe_detail", sa.JSON(), nullable=True))
    op.create_index(
        "uq_platform_accounts_desktop",
        "platform_accounts",
        ["workspace_id", "desktop_id", "platform"],
        unique=True,
        postgresql_where=sa.text("auth_kind = 'desktop_cookie' AND deleted_at IS NULL"),
        sqlite_where=sa.text("auth_kind = 'desktop_cookie' AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_platform_accounts_desktop", table_name="platform_accounts")
    op.drop_column("platform_accounts", "probe_detail")
    op.drop_column("platform_accounts", "desktop_id")
