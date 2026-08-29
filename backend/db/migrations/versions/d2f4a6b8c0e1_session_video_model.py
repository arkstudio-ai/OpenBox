"""Per-conversation video model selection.

The chat model and the video model are picked independently: one is free and
instantaneous, the other costs real money and minutes. A segment snapshots this
value at submission time, so switching mid-production never disturbs work that
is already in flight.

Revision ID: d2f4a6b8c0e1
Revises: f14b351203e1
Create Date: 2026-08-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2f4a6b8c0e1"
down_revision: Union[str, None] = "f14b351203e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("video_model", sa.String(160), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "video_model")
