"""Index agent events by (session, kind, sequence) (personal assistant V2, P5).

The single long-lived assistant conversation reads events of one kind on
every turn (budget receipts, decision notes, queue history). Without this
index each such read scans the conversation's whole event range. PostgreSQL
builds it concurrently so writers are not blocked.

Revision ID: pbaf0a1b2c3d
Revises: pb9e0f1a2b3c
"""
from alembic import op

revision = "pbaf0a1b2c3d"
down_revision = "pb9e0f1a2b3c"
branch_labels = None
depends_on = None

INDEX = "ix_agent_events_session_kind"


def upgrade():
    if op.get_context().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.create_index(INDEX, "agent_events", ["session_id", "kind", "sequence"],
                            postgresql_concurrently=True, if_not_exists=True)
    else:
        op.create_index(INDEX, "agent_events", ["session_id", "kind", "sequence"], if_not_exists=True)


def downgrade():
    if op.get_context().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(INDEX, table_name="agent_events", postgresql_concurrently=True, if_exists=True)
    else:
        op.drop_index(INDEX, table_name="agent_events", if_exists=True)
