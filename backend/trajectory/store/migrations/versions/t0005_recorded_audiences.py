"""Keep recorded source scopes independently of current session ancestry.

Existing recordings remain unverified until the worker reads their original
hot/archived events. Do not infer historical bindings from current metadata.
"""
from alembic import op
import sqlalchemy as sa

revision = "t0005_recorded_audiences"
down_revision = "t0004_worker_efficiency"
branch_labels = None
depends_on = None


def upgrade():
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(op.get_bind())
    if offline or "audience_seq" not in {column["name"] for column in inspector.get_columns("session_trajectories")}:
        op.add_column("session_trajectories", sa.Column("audience_seq", sa.BigInteger(), nullable=False,
                                                     server_default=sa.text("0")))
    if offline or not inspector.has_table("trajectory_session_sources"):
        op.create_table("trajectory_session_sources",
            sa.Column("trajectory_id", sa.String(64), sa.ForeignKey("session_trajectories.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("session_id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(64), primary_key=True),
            sa.Column("workspace_id", sa.String(64), primary_key=True))
    live = sa.text("deleted_at IS NULL AND content_expired_at IS NULL AND audience_seq < committed_seq")
    op.create_index("ix_trajectories_audience_pending", "session_trajectories", ["id"],
                    postgresql_where=live, sqlite_where=live, if_not_exists=True)


def downgrade():
    op.drop_index("ix_trajectories_audience_pending", table_name="session_trajectories")
    op.drop_table("trajectory_session_sources")
    op.drop_column("session_trajectories", "audience_seq")
