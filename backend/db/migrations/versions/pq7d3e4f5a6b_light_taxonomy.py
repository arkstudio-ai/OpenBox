"""light_taxonomy: the console's edit of the example library's scenes, industries and shop types.

One row laid over light/taxonomy.json (light/taxonomy.py); no row means the file is in force.

Revision ID: pq7d3e4f5a6b
Revises: pp6c2d3e4f5a
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pq7d3e4f5a6b"
down_revision = "pp6c2d3e4f5a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "light_taxonomy",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("data", JSONType(), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("light_taxonomy")
