"""Durable consumer document ingestion and immutable parsed revisions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "m9b0c1d2e3f4"
down_revision = "m8a9b0c1d2e3"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade():
    op.create_table("memory_documents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_ids", JSON, nullable=False),
        sa.Column("page_ids", JSON, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("domain", "file_hash", name="uq_memory_document_file"))
    op.create_index("ix_memory_document_claim", "memory_documents", ["status", "available_at"])
    op.create_index("ix_memory_document_scope", "memory_documents", ["user_id", "workspace_id", "project_id"])
    op.create_table("memory_document_revisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("sections", JSON, nullable=False),
        sa.Column("metadata", JSON, nullable=False),
        sa.Column("origin", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "revision", name="uq_memory_document_revision"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM memory_documents LIMIT 1")).first():
        raise RuntimeError("Preserve uploaded documents before removing their authority tables")
    op.drop_table("memory_document_revisions")
    op.drop_table("memory_documents")
