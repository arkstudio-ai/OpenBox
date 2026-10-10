"""Allow the full provider ID of an enrolled realtime voice.

Revision ID: pc06b7c8d9e0
Revises: pbf5a6b7c8d9
"""
from alembic import op
import sqlalchemy as sa

revision = "pc06b7c8d9e0"
down_revision = "pbf5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("voice_calls") as batch:
        batch.alter_column("voice", existing_type=sa.String(32), type_=sa.String(255), existing_nullable=False)


def downgrade():
    # Never truncate existing enrolled IDs to make a rollback fit the older schema.
    if op.get_bind().execute(sa.text("SELECT count(*) FROM voice_calls WHERE length(voice) > 32")).scalar():
        raise RuntimeError("Cannot narrow voice_calls.voice while enrolled voice IDs are stored")
    with op.batch_alter_table("voice_calls") as batch:
        batch.alter_column("voice", existing_type=sa.String(255), type_=sa.String(32), existing_nullable=False)
