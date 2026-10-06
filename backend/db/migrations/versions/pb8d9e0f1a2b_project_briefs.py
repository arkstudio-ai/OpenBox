"""Add per-user project briefs (personal assistant V2, P3).

One brief per (user, project): standing project notes the owner or their
personal assistant maintains, injected into the owner's ordinary sessions in
that project. At most 6,000 characters; ``revision`` drives optimistic writes.

Downgrading drops the table and the briefs written since.

Revision ID: pb8d9e0f1a2b
Revises: pb6b7c8d9e0f
"""
from alembic import op
import sqlalchemy as sa

revision = "pb8d9e0f1a2b"
down_revision = "pb6b7c8d9e0f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("project_briefs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_by", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "project_id", name="uq_project_briefs_user_project"),
        sa.CheckConstraint("revision >= 1", name="ck_project_briefs_revision"),
        sa.CheckConstraint("updated_by IN ('user', 'assistant')", name="ck_project_briefs_updated_by"),
        sa.CheckConstraint("length(content) <= 6000", name="ck_project_briefs_content_length"))


def downgrade():
    op.drop_table("project_briefs")
