"""Media facts of an uploaded video (duration, size, rotation, codec, audio) — 你拍我剪 F1.

Revision ID: pj06c7d8e9f0
Revises: pi95b6c7d8e9
"""
from alembic import op
import sqlalchemy as sa

revision = "pj06c7d8e9f0"
down_revision = "pi95b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("file_assets") as batch:
        batch.add_column(sa.Column("meta", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("file_assets") as batch:
        batch.drop_column("meta")
