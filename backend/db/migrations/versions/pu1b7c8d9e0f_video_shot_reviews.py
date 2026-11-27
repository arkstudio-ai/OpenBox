"""Queue generated-shot reviews and pool their metered duration per production."""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pu1b7c8d9e0f"
down_revision = "pt0a6b7c8d9e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("video_shot_reviews",
        sa.Column("job_id", sa.String(64), sa.ForeignKey("video_jobs.id"), primary_key=True),
        sa.Column("asset_id", sa.String(64), sa.ForeignKey("file_assets.id"), nullable=False, unique=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("group_key", sa.String(180), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("claim", sa.String(32)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("report", JSONType()),
    )
    op.create_index("ix_shot_reviews_due", "video_shot_reviews", ["state", "available_at"])
    op.create_index("ix_shot_reviews_group", "video_shot_reviews", ["session_id", "group_key", "state"])


def downgrade():
    op.drop_table("video_shot_reviews")
