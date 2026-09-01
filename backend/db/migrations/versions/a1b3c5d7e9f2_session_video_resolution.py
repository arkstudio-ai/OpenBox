"""Remember the composer's video resolution beside its model.

One model offers several tiers, and the pair decides both price and whether
the shot is usable at all. Without this the deployment default applied to
every conversation, which made 1080p — the most expensive tier — the silent
choice for everyone.

Revision ID: a1b3c5d7e9f2
Revises: f0b2d4e6a8c1
"""
import sqlalchemy as sa
from alembic import op

revision = "a1b3c5d7e9f2"
down_revision = "f0b2d4e6a8c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("video_resolution", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "video_resolution")
