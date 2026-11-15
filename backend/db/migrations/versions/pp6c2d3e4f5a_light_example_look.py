"""light_examples.look: what the finished piece looks like, in words, with none of its content.

Written once by a vision model from the piece the example card shows
(light/example_look.py) and handed to the agent when an owner picks the example.

Revision ID: pp6c2d3e4f5a
Revises: po5b1c2d3e4f
"""
from alembic import op
import sqlalchemy as sa

revision = "pp6c2d3e4f5a"
down_revision = "po5b1c2d3e4f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("light_examples") as batch:
        batch.add_column(sa.Column("look", sa.Text(), nullable=False, server_default=sa.text("''")))


def downgrade():
    with op.batch_alter_table("light_examples") as batch:
        batch.drop_column("look")
