"""Persist the reasoning variant selected for future conversation turns.

The value remains model-owned. NULL means the selected model route should use
its advertised default, while a non-NULL value is validated before a prompt is
admitted.

Revision ID: f0b2d4e6a8c1
Revises: e2b4d6f8a0c3
Create Date: 2026-08-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0b2d4e6a8c1"
down_revision: Union[str, None] = "e2b4d6f8a0c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_sessions_table() -> bool:
    """Support the repository's deliberately minimal migration fixtures."""
    return bool(sa.inspect(op.get_bind()).has_table("sessions"))


def upgrade() -> None:
    if _has_sessions_table():
        op.add_column("sessions", sa.Column("variant", sa.String(32), nullable=True))


def downgrade() -> None:
    if _has_sessions_table():
        op.drop_column("sessions", "variant")
