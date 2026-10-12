"""Failed-turn alerts (docs/SESSION_TRAJECTORY_LITE_PLAN.md §14): when a turn's failure was announced.

Rows that are already ``failed`` when this runs are stamped as announced so the
first worker pass after the upgrade does not notify about history.
"""
from alembic import op
import sqlalchemy as sa

revision = "t0007_turn_alerts"
down_revision = "t0006_turn_summaries"
branch_labels = None
depends_on = None


def upgrade():
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(op.get_bind())
    if offline or "alerted_at" not in {column["name"] for column in inspector.get_columns("trajectory_turn_summaries")}:
        op.add_column("trajectory_turn_summaries", sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE trajectory_turn_summaries SET alerted_at = CURRENT_TIMESTAMP WHERE status = 'failed' AND alerted_at IS NULL"))
    pending = sa.text("status = 'failed' AND alerted_at IS NULL")
    op.create_index("ix_trajectory_turn_summaries_alert_pending", "trajectory_turn_summaries", ["started_at"],
                    postgresql_where=pending, sqlite_where=pending, if_not_exists=True)


def downgrade():
    op.drop_index("ix_trajectory_turn_summaries_alert_pending", table_name="trajectory_turn_summaries")
    with op.batch_alter_table("trajectory_turn_summaries") as batch:
        batch.drop_column("alerted_at")
