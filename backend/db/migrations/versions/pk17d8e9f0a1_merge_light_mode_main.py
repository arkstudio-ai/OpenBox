"""Merge the Light-mode migration chain with main.

Both branches descend from ph84a5b6c7d8: main added ph95b6c7d8e9
(assistant_tasks.superseded_by_task_id) while light-mode added
pi95b6c7d8e9 -> pj06c7d8e9f0 (sessions.light_entry, file asset meta).
A merge revision lets a database that is at either head upgrade to the
same single head without rewriting applied revisions.

Revision ID: pk17d8e9f0a1
Revises: pj06c7d8e9f0, ph95b6c7d8e9
"""

revision = "pk17d8e9f0a1"
down_revision = ("pj06c7d8e9f0", "ph95b6c7d8e9")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
