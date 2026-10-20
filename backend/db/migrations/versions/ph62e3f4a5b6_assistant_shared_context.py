"""Keep assistant decisions across turns and describe shared attachments.

Revision ID: ph62e3f4a5b6
Revises: ph51d2e3f4a5
"""
from alembic import op
import sqlalchemy as sa

revision = "ph62e3f4a5b6"
down_revision = "ph51d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("question_checkpoints", sa.Column("detached", sa.Boolean(), nullable=False,
                                                   server_default=sa.false()))
    op.add_column("file_assets", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    op.add_column("file_assets", sa.Column("description_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    bind = op.get_bind()
    # Never silently throw away outstanding decisions or user-authored metadata.
    if bind.scalar(sa.text("SELECT count(*) FROM question_checkpoints WHERE detached = true")):
        raise RuntimeError("Export and resolve detached assistant decisions before downgrade")
    if bind.scalar(sa.text("SELECT count(*) FROM file_assets WHERE description <> ''")):
        raise RuntimeError("Export asset descriptions before downgrade")
    op.drop_column("file_assets", "description_updated_at")
    op.drop_column("file_assets", "description")
    op.drop_column("question_checkpoints", "detached")
