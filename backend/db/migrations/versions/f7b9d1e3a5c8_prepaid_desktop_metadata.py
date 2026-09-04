"""Persist cloud desktop billing metadata.

Revision ID: f7b9d1e3a5c8
Revises: e6a8c0d2f4b7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7b9d1e3a5c8"
down_revision: Union[str, None] = "e6a8c0d2f4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cloud_desktops", sa.Column("charge_type", sa.String(16), nullable=True))
    op.add_column(
        "cloud_desktops", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cloud_desktops", "expires_at")
    op.drop_column("cloud_desktops", "charge_type")
