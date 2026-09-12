"""Add durable session trajectories, resumable runs and replay indexes.

Revision ID: f6a8c0e2b4d6
Revises: e4f6a8b0c2d4
"""
from alembic import op
import sqlalchemy as sa
from db.base import JSONType

revision = "f6a8c0e2b4d6"
down_revision = "e4f6a8b0c2d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('session_trajectories',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('session_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('next_seq', sa.BigInteger(), nullable=False),
        sa.Column('committed_seq', sa.BigInteger(), nullable=False),
        sa.Column('projected_seq', sa.BigInteger(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        sa.Column('recording_status', sa.String(32), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('session_id', name=None),
        sa.UniqueConstraint('user_id', 'session_id', name='uq_trajectory_owner_session'),
    )
    op.create_table('trajectory_events',
        sa.Column('event_id', sa.String(128), nullable=False, primary_key=True),
        sa.Column('trajectory_id', sa.String(64), nullable=False),
        sa.Column('seq', sa.BigInteger(), nullable=False),
        sa.Column('type', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('session_id', sa.String(64), nullable=False),
        sa.Column('source_session_id', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=True),
        sa.Column('call_id', sa.String(128), nullable=True),
        sa.Column('agent_id', sa.String(128), nullable=True),
        sa.Column('context', JSONType(), nullable=False),
        sa.Column('data', JSONType(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('trajectory_id', 'seq', name='uq_trajectory_event_seq'),
        sa.ForeignKeyConstraint(['trajectory_id'], ['session_trajectories.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_trajectory_event_agent', 'trajectory_events', ['trajectory_id', 'agent_id', 'seq'], unique=False)
    op.create_index('ix_trajectory_event_call', 'trajectory_events', ['trajectory_id', 'call_id', 'seq'], unique=False)
    op.create_index('ix_trajectory_event_request', 'trajectory_events', ['trajectory_id', 'request_id', 'seq'], unique=False)
    op.create_table('trajectory_payloads',
        sa.Column('payload_id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('trajectory_id', sa.String(64), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('storage_key', sa.Text(), nullable=False),
        sa.Column('storage_status', sa.String(16), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('media_type', sa.String(128), nullable=False),
        sa.Column('encoding', sa.String(16), nullable=False),
        sa.Column('availability', sa.String(24), nullable=False),
        sa.Column('first_seq', sa.BigInteger(), nullable=False),
        sa.Column('source_asset_id', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['trajectory_id'], ['session_trajectories.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_trajectory_payload_digest', 'trajectory_payloads', ['trajectory_id', 'sha256'], unique=False)
    op.create_index('ix_trajectory_payload_pending', 'trajectory_payloads', ['storage_status', 'availability', 'created_at'], unique=False)
    op.create_index('ix_trajectory_payloads_source_asset_id', 'trajectory_payloads', ['source_asset_id'], unique=False)
    op.create_index('ix_trajectory_payloads_trajectory_id', 'trajectory_payloads', ['trajectory_id'], unique=False)
    op.create_table('trajectory_records',
        sa.Column('trajectory_id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('record_id', sa.String(256), nullable=False, primary_key=True),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('agent_id', sa.String(128), nullable=True),
        sa.Column('start_seq', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.String(128), nullable=True),
        sa.Column('end_seq', sa.BigInteger(), nullable=True),
        sa.Column('applied_seq', sa.BigInteger(), nullable=False),
        sa.Column('projector_version', sa.Integer(), nullable=False),
        sa.Column('data', JSONType(), nullable=False),
        sa.Column('summary', JSONType(), nullable=False),
        sa.Column('search_text', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['trajectory_id'], ['session_trajectories.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_trajectory_record_filter', 'trajectory_records', ['trajectory_id', 'kind', 'status', 'start_seq'], unique=False)
    op.create_index('ix_trajectory_record_message', 'trajectory_records', ['trajectory_id', 'message_id'], unique=False)
    op.create_index('ix_trajectory_record_order', 'trajectory_records', ['trajectory_id', 'start_seq', 'record_id'], unique=False)
    op.create_table('trajectory_session_summaries',
        sa.Column('trajectory_id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('session_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('running_status', sa.String(32), nullable=False),
        sa.Column('recording_status', sa.String(32), nullable=False),
        sa.Column('model', sa.String(128), nullable=True),
        sa.Column('applied_seq', sa.BigInteger(), nullable=False),
        sa.Column('statistics', JSONType(), nullable=False),
        sa.ForeignKeyConstraint(['trajectory_id'], ['session_trajectories.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_trajectory_summary_activity', 'trajectory_session_summaries', ['last_activity_at', 'session_id'], unique=False)
    op.create_index('ix_trajectory_summary_owner', 'trajectory_session_summaries', ['user_id', 'last_activity_at', 'session_id'], unique=False)
    op.create_index('ix_trajectory_summary_status', 'trajectory_session_summaries', ['running_status', 'recording_status', 'last_activity_at'], unique=False)
    op.create_index('ix_trajectory_summary_workspace', 'trajectory_session_summaries', ['workspace_id', 'last_activity_at', 'session_id'], unique=False)
    op.create_table('trajectory_checkpoints',
        sa.Column('trajectory_id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('through_seq', sa.BigInteger(), nullable=False, primary_key=True),
        sa.Column('projector_version', sa.Integer(), nullable=False),
        sa.Column('state', JSONType(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['trajectory_id'], ['session_trajectories.id'], ondelete='CASCADE'),
    )
    op.create_table('trajectory_exports',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('trajectory_id', sa.String(64), nullable=False),
        sa.Column('viewer_id', sa.String(64), nullable=False),
        sa.Column('through_seq', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('storage_key', sa.Text(), nullable=True),
        sa.Column('sha256', sa.String(64), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['trajectory_id'], ['session_trajectories.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_trajectory_exports_trajectory_id', 'trajectory_exports', ['trajectory_id'], unique=False)
    op.add_column("session_executions", sa.Column("trace_context", JSONType(), nullable=True, server_default=sa.text("'{}'")))
    op.add_column("cron_runs", sa.Column("trace_context", JSONType(), nullable=True))


def downgrade():
    op.drop_column("cron_runs", "trace_context")
    op.drop_column("session_executions", "trace_context")
    op.drop_table('trajectory_exports')
    op.drop_table('trajectory_checkpoints')
    op.drop_table('trajectory_session_summaries')
    op.drop_table('trajectory_records')
    op.drop_table('trajectory_payloads')
    op.drop_table('trajectory_events')
    op.drop_table('session_trajectories')
