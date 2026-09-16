"""Drop the redundant trajectory_events indexes; index trajectory_gc_queue by storage key.

Revision ID: t0003_event_indexes_gc_key
Revises: t0002_recording_epoch_bigint
Create Date: 2026-09-15

``ix_trajectory_events_seq`` is a prefix of the primary key and
``ix_trajectory_events_call`` has no reader (``call_id`` is never queried);
together they were 45 % of the index bytes written per hot event and deleted
again by archival. Ingest probes ``trajectory_gc_queue`` by ``storage_key``
for every batch with uploads, and so does the orphan sweep, so far without an
index. On PostgreSQL ``trajectory_events`` is partitioned: dropping a
partitioned index drops the index of every partition with it, and creating
one on the parent indexes every partition. ``IF EXISTS`` / ``IF NOT EXISTS``
keep both directions repeatable on either dialect.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = "t0003_event_indexes_gc_key"
down_revision: Union[str, None] = "t0002_recording_epoch_bigint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVENT_INDEXES = (
    ("ix_trajectory_events_seq", ["trajectory_id", "seq"]),
    ("ix_trajectory_events_call", ["trajectory_id", "call_id", "seq"]),
)
GC_KEY_INDEX = "ix_trajectory_gc_queue_storage_key"


def upgrade() -> None:
    for name, _ in EVENT_INDEXES:
        op.drop_index(name, table_name="trajectory_events", if_exists=True)
    op.create_index(GC_KEY_INDEX, "trajectory_gc_queue", ["storage_key"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index(GC_KEY_INDEX, table_name="trajectory_gc_queue", if_exists=True)
    for name, columns in EVENT_INDEXES:
        op.create_index(name, "trajectory_events", columns, if_not_exists=True)
