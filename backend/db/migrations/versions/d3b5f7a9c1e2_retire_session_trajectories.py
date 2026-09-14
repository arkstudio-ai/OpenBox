"""Retire the business-database trajectory tables.

Trajectory data moves to its own database (openbox_trace). The seven tables are
renamed, keeping their rows, indexes and constraints, until the legacy
converter has copied and verified them; its finalize step drops them.

Revision ID: d3b5f7a9c1e2
Revises: c7e9b1d3f5a7
"""
from alembic import op
import sqlalchemy as sa

revision = "d3b5f7a9c1e2"
down_revision = "c7e9b1d3f5a7"
branch_labels = None
depends_on = None

#: Original name -> retired name (db.models.trajectory.LEGACY_TABLE_NAMES).
TABLES = (
    ("session_trajectories", "legacy_trajectory_sessions"),
    ("trajectory_events", "legacy_trajectory_events"),
    ("trajectory_payloads", "legacy_trajectory_payloads"),
    ("trajectory_records", "legacy_trajectory_records"),
    ("trajectory_session_summaries", "legacy_trajectory_session_summaries"),
    ("trajectory_checkpoints", "legacy_trajectory_checkpoints"),
    ("trajectory_exports", "legacy_trajectory_exports"),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    existing = _tables()
    for original, legacy in TABLES:
        # A finalized conversion (or a database that never had them) leaves
        # nothing to rename.
        if original in existing and legacy not in existing:
            op.rename_table(original, legacy)


def downgrade():
    existing = _tables()
    for original, legacy in reversed(TABLES):
        if legacy in existing and original not in existing:
            op.rename_table(legacy, original)
