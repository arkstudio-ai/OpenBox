"""Index worker polling and session ordering; bound export crash retries.

Revision ID: t0004_worker_efficiency
Revises: t0003_event_indexes_gc_key

Checkpoint and budget thresholds are configurable, so their indexes use ranges
instead of baking the current settings into a partial-index predicate.
"""
from alembic import op
import sqlalchemy as sa

revision = "t0004_worker_efficiency"
down_revision = "t0003_event_indexes_gc_key"
branch_labels = None
depends_on = None

LIVE = "deleted_at IS NULL AND content_expired_at IS NULL"
WORKER_INDEXES = (
    ("ix_trajectories_projection_pending", ["id"], "projected_seq < committed_seq"),
    ("ix_trajectories_archive_pending", ["id"], "archived_seq < projected_seq"),
    ("ix_trajectories_hot_pending", ["id"], "archived_seq < committed_seq"),
    ("ix_trajectories_budget_events", ["event_count"], None),
    ("ix_trajectories_budget_bytes", ["stored_bytes"], None),
    ("ix_trajectories_budget_abnormal", ["id"], "budget_level <> 'normal'"),
)
ACTIVITY = sa.text("coalesce(projected_activity_at, updated_at)")
ACTIVITY_INDEXES = (
    ("ix_trajectory_meta_sessions_activity", [ACTIVITY, "id"]),
    ("ix_trajectory_meta_sessions_owner_activity", ["user_id", ACTIVITY, "id"]),
    ("ix_trajectory_meta_sessions_workspace_activity", ["workspace_id", ACTIVITY, "id"]),
)


def _add_column(table, column):
    # Embedded SQLite can adopt an unversioned create_all database by stamping
    # t0001 and upgrading. Such a database may already contain newer columns.
    if not op.get_context().as_sql:
        if column.name in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}:
            return
    op.add_column(table, column)


def upgrade():
    for name, columns, condition in WORKER_INDEXES:
        predicate = sa.text(LIVE + (" AND " + condition if condition else ""))
        op.create_index(name, "session_trajectories", columns,
                        postgresql_where=predicate, sqlite_where=predicate, if_not_exists=True)
    if op.get_context().dialect.name == "postgresql":
        op.create_index("ix_trajectories_checkpoint_backlog", "session_trajectories",
                        [sa.text("(projected_seq - checkpoint_seq)"), "id"], if_not_exists=True)
    _add_column("trajectory_exports", sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")))
    _add_column("trajectory_meta_sessions", sa.Column("projected_activity_at", sa.DateTime(timezone=True)))
    op.execute("""
        UPDATE trajectory_meta_sessions SET projected_activity_at = (
            SELECT summary.last_activity_at FROM trajectory_session_summaries AS summary
            JOIN session_trajectories AS trajectory ON trajectory.id = summary.trajectory_id
            WHERE trajectory.session_id = trajectory_meta_sessions.id
              AND trajectory.user_id = trajectory_meta_sessions.user_id
              AND trajectory.deleted_at IS NULL
        )
    """)
    for name, columns in ACTIVITY_INDEXES:
        op.create_index(name, "trajectory_meta_sessions", columns, if_not_exists=True)
    if op.get_context().dialect.name == "postgresql":
        # Existing idle tables might not trigger autoanalyze soon. In particular,
        # the checkpoint expression needs statistics before the first worker poll.
        op.execute("ANALYZE session_trajectories")
        op.execute("ANALYZE trajectory_meta_sessions")


def downgrade():
    for name, _ in reversed(ACTIVITY_INDEXES):
        op.drop_index(name, table_name="trajectory_meta_sessions")
    op.drop_column("trajectory_meta_sessions", "projected_activity_at")
    op.drop_column("trajectory_exports", "attempts")
    if op.get_context().dialect.name == "postgresql":
        op.drop_index("ix_trajectories_checkpoint_backlog", table_name="session_trajectories")
    for name, _, _ in reversed(WORKER_INDEXES):
        op.drop_index(name, table_name="session_trajectories")
