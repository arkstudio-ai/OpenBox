"""Add memory clocks, atomic receipts and private validated experience metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "ph84a5b6c7d8"
down_revision = "ph73f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table("memory_change_clocks",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("memory_write_receipts",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("operation_id", sa.String(160), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("result", json_type, nullable=False),
        sa.Column("source_refs", json_type, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("memory_experiences",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64)),
        sa.Column("operation_id", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(24), nullable=False, server_default="candidate"),
        sa.Column("content", json_type, nullable=False),
        sa.Column("source_refs", json_type, nullable=False),
        sa.Column("validation", json_type, nullable=False),
        sa.Column("skill_id", sa.String(64)),
        sa.Column("skill_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "operation_id", name="uq_memory_experience_operation"),
        sa.CheckConstraint("state IN ('candidate', 'validated', 'enabled', 'retired')", name="ck_memory_experience_state"))
    op.create_index("ix_memory_experience_scope", "memory_experiences", ["user_id", "workspace_id", "state"])


def downgrade():
    # Application rollback preserves schema. Never discard new receipts or
    # experience versions just to run old code; only an empty rehearsal is reversible.
    for table in ("memory_change_clocks", "memory_write_receipts", "memory_experiences"):
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError("Keep memory protocol schema: committed clocks/receipts/experiences exist")
    op.drop_index("ix_memory_experience_scope", table_name="memory_experiences")
    op.drop_table("memory_experiences")
    op.drop_table("memory_write_receipts")
    op.drop_table("memory_change_clocks")
