"""The Light reference example library (docs/LIGHT_MODE_IMPL_PLAN.md §11).

Revision ID: pl28e9f0a1b2
Revises: pk17d8e9f0a1
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pl28e9f0a1b2"
down_revision = "pk17d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "light_examples",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("entry_id", sa.String(32), nullable=False),
        sa.Column("industry", sa.String(32), nullable=False),
        sa.Column("shop_type", sa.String(32), nullable=False, server_default="other"),
        sa.Column("subtype", sa.String(64), nullable=True),
        sa.Column("tags", JSONType(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("recipe", sa.Text(), nullable=False, server_default=""),
        sa.Column("inputs", JSONType(), nullable=False),
        sa.Column("outputs", JSONType(), nullable=False),
        sa.Column("source", JSONType(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_light_examples_match", "light_examples", ["entry_id", "industry", "shop_type", "status"])
    op.create_index("ix_light_examples_created", "light_examples", ["created_at"])


def downgrade():
    op.drop_index("ix_light_examples_created", table_name="light_examples")
    op.drop_index("ix_light_examples_match", table_name="light_examples")
    op.drop_table("light_examples")
