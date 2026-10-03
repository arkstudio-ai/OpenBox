"""Private assistant sessions and durable task/command/result identities."""
from alembic import op
import sqlalchemy as sa
from db.base import JSONType

revision = "pa1b2c3d4e5f"
down_revision = "ma0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sessions', sa.Column('visibility', sa.String(length=16), server_default='workspace', nullable=False))
    op.add_column('sessions', sa.Column('memory_policy', sa.String(length=32), server_default='standard', nullable=False))
    op.add_column('agent_inbox_items', sa.Column('origin', sa.String(length=32), server_default='unknown', nullable=False))
    op.add_column('agent_inbox_items', sa.Column('origin_ref', JSONType(), server_default='{}', nullable=False))
    op.create_index('uq_sessions_active_assistant', 'sessions', ['user_id', 'workspace_id'], unique=True, postgresql_where=sa.text("kind = 'assistant' AND is_deleted = false"), sqlite_where=sa.text("kind = 'assistant' AND is_deleted = 0"))
    op.create_table('assistant_tasks',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('assistant_session_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('execution_session_id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=128), nullable=False),
    sa.Column('desired_state', sa.String(length=16), nullable=False),
    sa.Column('observed_state', sa.String(length=24), nullable=False),
    sa.Column('control_revision', sa.Integer(), nullable=False),
    sa.Column('intent_revision', sa.Integer(), nullable=False),
    sa.Column('latest_result_id', sa.String(length=64), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("desired_state IN ('running', 'paused', 'canceled')", name='ck_assistant_task_desired'),
    sa.CheckConstraint('control_revision > 0 AND intent_revision > 0', name='ck_assistant_task_revisions'),
    sa.ForeignKeyConstraint(['assistant_session_id'], ['sessions.id'], ),
    sa.ForeignKeyConstraint(['execution_session_id'], ['sessions.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('execution_session_id', name='uq_assistant_task_execution_session')
    )
    op.create_index('ix_assistant_tasks_main', 'assistant_tasks', ['assistant_session_id', 'archived_at'], unique=False)
    op.create_index('ix_assistant_tasks_owner', 'assistant_tasks', ['user_id', 'workspace_id', 'updated_at'], unique=False)
    op.create_table('assistant_commands',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('actor_user_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('assistant_session_id', sa.String(length=64), nullable=False),
    sa.Column('idempotency_key', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('target_type', sa.String(length=24), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('payload_digest', sa.String(length=64), nullable=False),
    sa.Column('expected_revision', sa.Integer(), nullable=True),
    sa.Column('source_ref', JSONType(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('receipt', JSONType(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(idempotency_key) BETWEEN 1 AND 64', name='ck_assistant_command_key'),
    sa.CheckConstraint('length(payload_digest) = 64', name='ck_assistant_command_digest'),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['assistant_session_id'], ['sessions.id'], ),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('actor_user_id', 'workspace_id', 'assistant_session_id', 'idempotency_key', name='uq_assistant_command_key')
    )
    op.create_index('ix_assistant_commands_target', 'assistant_commands', ['target_type', 'target_id'], unique=False)
    op.create_table('assistant_task_submissions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('command_id', sa.String(length=64), nullable=False),
    sa.Column('inbox_id', sa.String(length=64), nullable=False),
    sa.Column('origin', sa.String(length=32), nullable=False),
    sa.Column('source_message_id', sa.String(length=64), nullable=True),
    sa.Column('delivery', sa.String(length=16), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('disposition', sa.String(length=24), nullable=False),
    sa.ForeignKeyConstraint(['command_id'], ['assistant_commands.id'], ),
    sa.ForeignKeyConstraint(['inbox_id'], ['agent_inbox_items.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['assistant_tasks.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('command_id', name='uq_assistant_submission_command'),
    sa.UniqueConstraint('inbox_id', name='uq_assistant_submission_inbox')
    )
    op.create_index('ix_assistant_submissions_task', 'assistant_task_submissions', ['task_id', 'accepted_at'], unique=False)
    op.create_table('assistant_task_results',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('source_event_key', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('settlement_fence', JSONType(), nullable=False),
    sa.Column('outcome', sa.String(length=24), nullable=False),
    sa.Column('consumed_inbox_ids', JSONType(), nullable=False),
    sa.Column('result_message_id', sa.String(length=64), nullable=True),
    sa.Column('output_refs', JSONType(), nullable=False),
    sa.Column('observed_intent_revision', sa.Integer(), nullable=False),
    sa.Column('delivery_state', sa.String(length=16), nullable=False),
    sa.Column('report_attempt', sa.Integer(), nullable=False),
    sa.Column('assistant_inbox_id', sa.String(length=64), nullable=True),
    sa.Column('processed_message_id', sa.String(length=64), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_error_code', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("delivery_state IN ('pending', 'accepted', 'retry_wait', 'processed', 'blocked')", name='ck_assistant_result_delivery'),
    sa.CheckConstraint('generation > 0 AND report_attempt > 0 AND retry_count >= 0', name='ck_assistant_result_counters'),
    sa.ForeignKeyConstraint(['assistant_inbox_id'], ['agent_inbox_items.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['assistant_tasks.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'source_event_key', name='uq_assistant_result_terminal')
    )
    op.create_index('ix_assistant_results_delivery', 'assistant_task_results', ['delivery_state', 'available_at'], unique=False)
    op.create_index('ix_assistant_results_task', 'assistant_task_results', ['task_id', 'created_at'], unique=False)
    op.create_table('assistant_read_cursors',
    sa.Column('assistant_session_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('last_seen_sequence', sa.BigInteger(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('last_seen_sequence >= 0', name='ck_assistant_read_sequence'),
    sa.ForeignKeyConstraint(['assistant_session_id'], ['sessions.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('assistant_session_id', 'user_id')
    )


def downgrade():
    # Removing the audience columns would silently disclose private histories.
    # Removing commands/results would discard recoverable execution receipts.
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT 1 FROM sessions WHERE visibility = 'private' OR kind = 'assistant' "
        "OR memory_policy != 'standard' LIMIT 1"
    )).first():
        raise RuntimeError("Private assistant sessions must be explicitly migrated before downgrade")
    for table in (
        "assistant_read_cursors", "assistant_task_results", "assistant_task_submissions",
        "assistant_commands", "assistant_tasks",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Assistant records must be explicitly migrated before downgrade")
    for table in (
        "assistant_read_cursors", "assistant_task_results", "assistant_task_submissions",
        "assistant_commands", "assistant_tasks",
    ):
        op.drop_table(table)
    op.drop_index("uq_sessions_active_assistant", table_name="sessions")
    op.drop_column("agent_inbox_items", "origin_ref")
    op.drop_column("agent_inbox_items", "origin")
    op.drop_column("sessions", "memory_policy")
    op.drop_column("sessions", "visibility")
