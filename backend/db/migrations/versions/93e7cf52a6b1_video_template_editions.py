"""Versioned professional video templates, reviewed by the existing Skill store."""
from alembic import op
import sqlalchemy as sa
from db.base import JSONType

revision = "93e7cf52a6b1"
down_revision = "pt0a6b7c8d9e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("video_template_editions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("skill_id", sa.String(64), sa.ForeignKey("user_skills.id"), nullable=False, unique=True),
        sa.Column("source_asset_id", sa.String(64), sa.ForeignKey("file_assets.id"), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("manifest", JSONType(), nullable=False),
        sa.Column("preview_key", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("template_id", "version", name="uq_video_template_edition"),
    )


def downgrade():
    op.drop_table("video_template_editions")
