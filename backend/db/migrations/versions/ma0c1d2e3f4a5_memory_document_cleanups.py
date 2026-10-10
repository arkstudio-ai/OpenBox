"""Durable removal of deleted documents' original files from storage."""
from alembic import op
import sqlalchemy as sa

revision = "ma0c1d2e3f4a5"
down_revision = "m9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("memory_document_cleanups",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(80), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_memory_document_cleanup_due", "memory_document_cleanups", ["status", "available_at"])
    op.create_index("ix_memory_document_cleanup_scope", "memory_document_cleanups", ["user_id", "workspace_id"])


def downgrade():
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM memory_document_cleanups WHERE status IN ('PENDING', 'UPLOADING', 'ABANDONED') LIMIT 1")).first():
        raise RuntimeError("Finish removing deleted documents' original files before dropping their cleanup records")
    op.drop_table("memory_document_cleanups")
