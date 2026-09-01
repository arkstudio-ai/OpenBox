"""Bound durable Inbox attachment-delivery recovery.

Revision ID: d0a2c4e6f8b1
Revises: c6f9a1d3e5b7
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d0a2c4e6f8b1"
down_revision: Union[str, None] = "c6f9a1d3e5b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.add_column(
        "agent_inbox_items",
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_inbox_items",
        sa.Column("delivery_last_error", _json_type(), nullable=True),
    )
    with op.batch_alter_table(
        "agent_inbox_items",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.create_check_constraint(
            "ck_agent_inbox_delivery_attempts",
            "delivery_attempts BETWEEN 0 AND 1000",
        )


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM agent_inbox_items "
            "WHERE delivery_attempts != 0 OR delivery_last_error IS NOT NULL"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(
            "inbox delivery-attempt downgrade refused: durable delivery state exists"
        )
    with op.batch_alter_table(
        "agent_inbox_items",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.drop_constraint(
            "ck_agent_inbox_delivery_attempts",
            type_="check",
        )
        batch.drop_column("delivery_last_error")
        batch.drop_column("delivery_attempts")
