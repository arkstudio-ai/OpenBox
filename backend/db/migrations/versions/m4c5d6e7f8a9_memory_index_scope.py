"""Repair index-state project scoping for early memory-authority deployments.

Revision ID: m4c5d6e7f8a9
Revises: m3b4c5d6e7f8
"""
from alembic import op
import sqlalchemy as sa

revision = 'm4c5d6e7f8a9'
down_revision = 'm3b4c5d6e7f8'
branch_labels = None
depends_on = None


def upgrade():
    # Fresh m1 contains the column; early implementations may already have
    # applied m1 before its project-scope correction. The repair is additive.
    bind = op.get_bind()
    if 'project_id' not in {column['name'] for column in sa.inspect(bind).get_columns('memory_index_state')}:
        op.add_column('memory_index_state', sa.Column('project_id', sa.String(64), nullable=True))
    op.execute("UPDATE memory_index_state SET project_id = (SELECT project_id FROM user_memories WHERE user_memories.id = memory_index_state.object_id) WHERE object_kind = 'memory'")
    op.execute("UPDATE memory_index_state SET project_id = (SELECT project_id FROM memory_sources WHERE memory_sources.id = memory_index_state.object_id) WHERE object_kind = 'source'")


def downgrade():
    # Retain a security-critical filter in the additive rollback window. The
    # original authority downgrade owns eventual removal of the entire table.
    pass
