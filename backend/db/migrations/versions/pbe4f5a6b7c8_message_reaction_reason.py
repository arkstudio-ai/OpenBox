"""A thumbs-down can say why: too long, too short, off topic, wrong, or the tone.

Written with the reaction (``session.set_message_reaction``); reasons that keep
coming back go into how the assistant talks to that user (assistant/style.py).
Nullable: a reaction without a reason, and every earlier one, has none.
Downgrading drops the column.

Revision ID: pbe4f5a6b7c8
Revises: pbd3e4f5a6b7
"""
from alembic import op
import sqlalchemy as sa

revision = "pbe4f5a6b7c8"
down_revision = "pbd3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("messages", sa.Column("reaction_reason", sa.String(16), nullable=True))


def downgrade():
    # batch: SQLite cannot drop a column in place.
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("reaction_reason")
