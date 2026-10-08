"""Voice calls keep a short summary of what was said, for the next call's greeting.

Written after hang-up by the voice bridge (``voice.calls.save_summary``); read
by ``voice.calls.latest_call_summary``. Nullable: earlier calls have none and a
call whose summary could not be made keeps none. Downgrading drops the column.

Revision ID: pbd3e4f5a6b7
Revises: pbc2d3e4f5a6
"""
from alembic import op
import sqlalchemy as sa

revision = "pbd3e4f5a6b7"
down_revision = "pbc2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("voice_calls", sa.Column("summary", sa.Text(), nullable=True))


def downgrade():
    # batch: SQLite cannot drop a column in place.
    with op.batch_alter_table("voice_calls") as batch:
        batch.drop_column("summary")
