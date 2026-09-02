"""Carry the composer's video resolution on the accepted Inbox item.

The resolution is picked beside the video model, so it has to travel the same
durable route: recorded on the accepted item and written back to the Session
inside the claim transaction, never as a pre-acceptance mutation of a Session
a live generation still owns.

Revision ID: c1e3a5d7f9b2
Revises: b2d4f6a8c0e2
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1e3a5d7f9b2"
down_revision: Union[str, None] = "b2d4f6a8c0e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_inbox_items",
        sa.Column("video_resolution", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_inbox_items", "video_resolution")
