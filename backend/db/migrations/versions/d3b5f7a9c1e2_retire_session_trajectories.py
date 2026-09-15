"""Drop the business-database trajectory tables.

Trajectory data now lives in its own database (openbox_trace) and old recordings
are not kept, so the seven tables are dropped. The legacy_trajectory_* names are
the ones an earlier revision of this migration renamed them to; they are dropped
too. Downgrade recreates the seven tables empty, for business code that still
expects them.

Revision ID: d3b5f7a9c1e2
Revises: c7e9b1d3f5a7
"""
import importlib

from alembic import op

revision = "d3b5f7a9c1e2"
down_revision = "c7e9b1d3f5a7"
branch_labels = None
depends_on = None

#: Children before session_trajectories, so SQLite without CASCADE drops them in order.
TABLES = (
    "trajectory_exports", "legacy_trajectory_exports",
    "trajectory_checkpoints", "legacy_trajectory_checkpoints",
    "trajectory_session_summaries", "legacy_trajectory_session_summaries",
    "trajectory_records", "legacy_trajectory_records",
    "trajectory_payloads", "legacy_trajectory_payloads",
    "trajectory_events", "legacy_trajectory_events",
    "session_trajectories", "legacy_trajectory_sessions",
)


def upgrade():
    cascade = " CASCADE" if op.get_bind().dialect.name == "postgresql" else ""
    for table in TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}"{cascade}')


def downgrade():
    # Old recordings are not restored: the tables come back empty, because the
    # previous business code (readiness check, session deletion) expects them.
    importlib.import_module("db.migrations.versions.f6a8c0e2b4d6_session_trajectories").create_trajectory_tables()
