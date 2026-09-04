"""add per-desktop execution channel state

Revision ID: c1d3e5f7a9b2
Revises: b2d4f6a8c0e2
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d3e5f7a9b2"
down_revision: Union[str, None] = "b2d4f6a8c0e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cloud_desktops", sa.Column("channel_kind", sa.String(16), nullable=True))
    op.add_column("cloud_desktops", sa.Column("private_ip", sa.String(64), nullable=True))
    op.add_column("cloud_desktops", sa.Column("tunnel_port", sa.Integer(), nullable=True))
    op.add_column("cloud_desktops", sa.Column("tunnel_bind", sa.String(64), nullable=True))
    op.add_column("cloud_desktops", sa.Column("tunnel_pubkey", sa.Text(), nullable=True))
    op.add_column("cloud_desktops", sa.Column("tunnel_fingerprint", sa.String(64), nullable=True))
    op.add_column("cloud_desktops", sa.Column("action_api_key_hash", sa.String(64), nullable=True))
    op.add_column("cloud_desktops", sa.Column("action_api_key_ciphertext", sa.Text(), nullable=True))
    op.add_column(
        "cloud_desktops",
        sa.Column("tunnel_state", sa.String(16), server_default="pending", nullable=False),
    )
    op.add_column("cloud_desktops", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cloud_desktops", sa.Column("channel_error", sa.Text(), nullable=True))
    op.create_index("uq_cloud_desktops_tunnel_port", "cloud_desktops", ["tunnel_port"], unique=True)
    op.create_index(
        "uq_cloud_desktops_tunnel_fingerprint",
        "cloud_desktops",
        ["tunnel_fingerprint"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_cloud_desktops_tunnel_fingerprint", table_name="cloud_desktops")
    op.drop_index("uq_cloud_desktops_tunnel_port", table_name="cloud_desktops")
    for column in (
        "channel_error",
        "last_seen_at",
        "tunnel_state",
        "action_api_key_ciphertext",
        "action_api_key_hash",
        "tunnel_fingerprint",
        "tunnel_pubkey",
        "tunnel_bind",
        "tunnel_port",
        "private_ip",
        "channel_kind",
    ):
        op.drop_column("cloud_desktops", column)
