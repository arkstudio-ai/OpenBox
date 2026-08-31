"""Persist the reasoning variant selected for future conversation turns.

The value remains model-owned. NULL means the selected model route should use
its advertised default, while a non-NULL value is validated before a prompt is
admitted.

Revision ID: f0b2d4e6a8c1
Revises: e9a1c3d5f7b2
Create Date: 2026-08-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0b2d4e6a8c1"
down_revision: Union[str, None] = "e9a1c3d5f7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("variant", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "variant")
