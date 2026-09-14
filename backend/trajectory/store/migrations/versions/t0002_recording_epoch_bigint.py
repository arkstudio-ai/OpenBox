"""Widen session_trajectories.recording_epoch to BIGINT.

Revision ID: t0002_recording_epoch_bigint
Revises: t0001_initial
Create Date: 2026-09-15

Producers name recording periods by millisecond epochs (trajectory.producers),
and the worker keeps the epoch of the last applied resume in this column
(wave-3 contract 6). A SQLite INTEGER already holds 64 bits, so only
PostgreSQL changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "t0002_recording_epoch_bigint"
down_revision: Union[str, None] = "t0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.alter_column("session_trajectories", "recording_epoch", type_=sa.BigInteger(), existing_type=sa.Integer(),
                        existing_nullable=False, existing_server_default=sa.text("0"))


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        # An epoch beyond the integer range cannot be kept; it becomes the largest integer.
        op.alter_column("session_trajectories", "recording_epoch", type_=sa.Integer(), existing_type=sa.BigInteger(),
                        existing_nullable=False, existing_server_default=sa.text("0"),
                        postgresql_using="LEAST(recording_epoch, 2147483647)::integer")
