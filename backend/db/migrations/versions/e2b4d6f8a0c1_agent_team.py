"""Add versioned Agent definitions and the session-owned team event journal.

Revision ID: e2b4d6f8a0c1
Revises: d0a2c4e6f8b1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e2b4d6f8a0c1"
down_revision = "d0a2c4e6f8b1"
branch_labels = None
depends_on = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table('agent_definitions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('owner_user_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('active_name', sa.Integer(), nullable=True),
    sa.Column('current_version_id', sa.String(length=64), nullable=True),
    sa.Column('draft_version_id', sa.String(length=64), nullable=True),
    sa.Column('provenance', _json_type(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'active', 'archived')", name='ck_agent_definition_status'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_user_id', 'workspace_id', 'name', 'active_name', name='uq_agent_definitions_active_name')
    )
    op.create_index('ix_agent_definitions_owner', 'agent_definitions', ['owner_user_id', 'workspace_id', 'status'], unique=False)
    op.create_table('agent_definition_versions',
    sa.Column('definition_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('spec_json', _json_type(), nullable=False),
    sa.Column('capability_summary', _json_type(), nullable=False),
    sa.Column('content_digest', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['definition_id'], ['agent_definitions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('definition_id', 'version', name='uq_agent_definition_version')
    )
    op.create_table('team_definitions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('owner_user_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('active_name', sa.Integer(), nullable=True),
    sa.Column('current_version_id', sa.String(length=64), nullable=True),
    sa.Column('draft_version_id', sa.String(length=64), nullable=True),
    sa.Column('provenance', _json_type(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'active', 'archived')", name='ck_team_definition_status'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_user_id', 'workspace_id', 'name', 'active_name', name='uq_team_definitions_active_name')
    )
    op.create_index('ix_team_definitions_owner', 'team_definitions', ['owner_user_id', 'workspace_id', 'status'], unique=False)
    op.create_table('team_definition_versions',
    sa.Column('definition_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('spec_json', _json_type(), nullable=False),
    sa.Column('capability_summary', _json_type(), nullable=False),
    sa.Column('content_digest', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['definition_id'], ['team_definitions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('definition_id', 'version', name='uq_team_definition_version')
    )
    op.create_table('team_runs',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('root_session_id', sa.String(length=64), nullable=False),
    sa.Column('owner_user_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('template_id', sa.String(length=64), nullable=True),
    sa.Column('template_version_id', sa.String(length=64), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('goal', sa.Text(), nullable=False),
    sa.Column('summary', _json_type(), nullable=False),
    sa.Column('policy_snapshot', _json_type(), nullable=False),
    sa.Column('grant_snapshot', _json_type(), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('pause_reason', sa.String(length=1000), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('session_active', sa.Integer(), nullable=True),
    sa.Column('project_active', sa.Integer(), nullable=True),
    sa.Column('last_seq', sa.BigInteger(), nullable=False),
    sa.Column('state_cache', _json_type(), nullable=True),
    sa.Column('cache_seq', sa.BigInteger(), nullable=False),
    sa.Column('start_snapshot', sa.String(length=128), nullable=True),
    sa.Column('end_snapshot', sa.String(length=128), nullable=True),
    sa.Column('final_summary', sa.Text(), nullable=True),
    sa.Column('final_artifact_ids', _json_type(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(state IN ('completed', 'canceled', 'failed') AND session_active IS NULL AND project_active IS NULL) OR (state NOT IN ('completed', 'canceled', 'failed') AND session_active IS NOT NULL AND project_active IS NOT NULL AND session_active = 1 AND project_active = 1)", name='ck_team_run_active'),
    sa.CheckConstraint("state IN ('provisioning', 'running', 'waiting', 'pausing', 'paused', 'canceling', 'canceled', 'completing', 'completed', 'failed')", name='ck_team_run_state'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['root_session_id'], ['sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'project_active', name='uq_team_run_active_project'),
    sa.UniqueConstraint('root_session_id', 'session_active', name='uq_team_run_active_session')
    )
    op.create_index('ix_team_runs_owner_created', 'team_runs', ['owner_user_id', 'workspace_id', 'created_at', 'id'], unique=False)
    op.create_index('ix_team_runs_recovery', 'team_runs', ['session_active', 'updated_at'], unique=False)
    op.create_table('team_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('team_run_id', sa.String(length=64), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('event_key', sa.String(length=64), nullable=False),
    sa.Column('request_digest', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('actor_type', sa.String(length=16), nullable=False),
    sa.Column('actor_member_id', sa.String(length=64), nullable=True),
    sa.Column('entity_type', sa.String(length=24), nullable=False),
    sa.Column('entity_id', sa.String(length=128), nullable=False),
    sa.Column('payload', _json_type(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('sequence > 0', name='ck_team_event_sequence'),
    sa.ForeignKeyConstraint(['team_run_id'], ['team_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('team_run_id', 'event_key', name='uq_team_event_key'),
    sa.UniqueConstraint('team_run_id', 'sequence', name='uq_team_event_sequence')
    )
    op.create_index('ix_team_events_entity', 'team_events', ['team_run_id', 'entity_type', 'entity_id', 'sequence'], unique=False)
    op.create_index('ix_team_events_member_binding', 'team_events', ['entity_id', 'kind'], unique=False)
    op.add_column('agent_inbox_items', sa.Column('source_type', sa.String(length=24), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    tables = ("team_events", "team_runs", "team_definition_versions", "team_definitions", "agent_definition_versions", "agent_definitions")
    if any(connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in tables):
        raise RuntimeError("Team data is durable. Disable admission and drain active runs; retain these additive tables on rollback.")
    if connection.execute(sa.text("SELECT COUNT(*) FROM agent_inbox_items WHERE source_type IS NOT NULL")).scalar_one():
        raise RuntimeError("Team Inbox provenance must be retained on rollback.")
    for table in tables:
        op.drop_table(table)
    op.drop_column("agent_inbox_items", "source_type")
