"""Remove per-approval video generation call counters.

Revision ID: e9a1c3d5f7b2
Revises: d8f0a2c4e6b9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9a1c3d5f7b2"
down_revision: Union[str, None] = "d8f0a2c4e6b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("video_approvals") as batch:
        batch.drop_column("max_calls")
        batch.drop_column("used_calls")


def downgrade() -> None:
    with op.batch_alter_table("video_approvals") as batch:
        batch.add_column(sa.Column("max_calls", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "used_calls",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
