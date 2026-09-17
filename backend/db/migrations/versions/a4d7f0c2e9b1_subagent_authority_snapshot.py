"""Persist monotonic Task child authority snapshots.

Revision ID: a4d7f0c2e9b1
Revises: fe6a8c0e2b4d
Create Date: 2026-08-31

Existing descriptors receive an intentionally unsupported empty snapshot.  A
running legacy child must fail loudly instead of guessing a wider boundary.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a4d7f0c2e9b1"
down_revision: Union[str, None] = "fe6a8c0e2b4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    # SQLite supports ADD COLUMN directly. Avoid batch-table reflection here:
    # narrow migration smoke fixtures intentionally omit unrelated FK targets.
    op.add_column(
        "subagent_descriptors",
        sa.Column(
            "authority_snapshot",
            _json_type(),
            server_default=sa.text("'{}'"),
            nullable=False,
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    live = bind.execute(
        sa.text("SELECT COUNT(*) FROM subagent_descriptors")
    ).scalar_one()
    if live:
        raise RuntimeError(
            "subagent authority downgrade refused: descriptors still exist"
        )
    op.drop_column("subagent_descriptors", "authority_snapshot")
