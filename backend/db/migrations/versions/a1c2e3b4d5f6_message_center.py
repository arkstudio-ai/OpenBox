"""Message centre: inbox columns on notifications, announcements and topics.

Revision ID: a1c2e3b4d5f6
Revises: e4f6a8b0c2d4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1c2e3b4d5f6"
down_revision: Union[str, None] = "e4f6a8b0c2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return sa.Text().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("link", _json(), nullable=True),
        sa.Column("audience", _json(), nullable=False),
        sa.Column("push", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fanout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fanout_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("cover_url", sa.String(1024), nullable=True),
        sa.Column("content_md", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("cta_label", sa.String(40), nullable=True),
        sa.Column("cta_link", _json(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("slug", name="uq_topics_slug"),
    )
    with op.batch_alter_table("notifications") as batch:
        batch.alter_column("workspace_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("category", sa.String(16), nullable=False, server_default=sa.text("'system'")))
        batch.add_column(sa.Column("link", _json(), nullable=True))
        batch.add_column(sa.Column("source_key", sa.String(255), nullable=True))
        batch.add_column(sa.Column("announcement_id", sa.String(64), sa.ForeignKey("announcements.id"), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_notifications_user_created", ["user_id", "created_at"])
        batch.create_unique_constraint("uq_notifications_source", ["user_id", "source_key"])


def downgrade() -> None:
    with op.batch_alter_table("notifications") as batch:
        batch.drop_constraint("uq_notifications_source", type_="unique")
        batch.drop_index("ix_notifications_user_created")
        for column in ("expires_at", "resolved_at", "announcement_id", "source_key", "link", "category"):
            batch.drop_column(column)
        batch.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
    op.drop_table("topics")
    op.drop_table("announcements")
