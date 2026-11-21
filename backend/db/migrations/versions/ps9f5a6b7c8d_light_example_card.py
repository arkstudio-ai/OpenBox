"""light_examples.card / card_meta: what to learn from the example, written once when it is collected.

A model looks at the finished piece itself (the picture, or frames and speech
of the clip) together with the recipe, and writes a reference card with none
of the shop's own content (light/example_card.py): ``card`` is that text,
``card_meta`` how and when it was read.

Revision ID: ps9f5a6b7c8d
Revises: pr8e4f5a6b7c
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "ps9f5a6b7c8d"
down_revision = "pr8e4f5a6b7c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("light_examples") as batch:
        batch.add_column(sa.Column("card", sa.Text(), nullable=False, server_default=sa.text("''")))
        batch.add_column(sa.Column("card_meta", JSONType(), nullable=True))


def downgrade():
    with op.batch_alter_table("light_examples") as batch:
        batch.drop_column("card_meta")
        batch.drop_column("card")
