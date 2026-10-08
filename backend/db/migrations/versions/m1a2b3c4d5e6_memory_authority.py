"""Versioned personal memory authority and derived-delivery state.

Revision ID: m1a2b3c4d5e6
Revises: d0a2c4e6f8b1

Application rollback retains these tables. Schema downgrade is permitted only
before new revisions/evidence exist and must be preceded by a backup.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from hashlib import sha256
import json
import re

revision = "m1a2b3c4d5e6"
down_revision = "d0a2c4e6f8b1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_memories", sa.Column('revision', sa.Integer(), nullable=False, server_default=sa.text('1')))
    op.add_column("user_memories", sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")))
    op.add_column("user_memories", sa.Column('confirmation_status', sa.String(24), nullable=False, server_default=sa.text("'PENDING'")))
    op.add_column("user_memories", sa.Column('confirmation_actor_id', sa.String(64), nullable=True))
    op.add_column("user_memories", sa.Column('fact_key', sa.String(255), nullable=True))
    op.add_column("user_memories", sa.Column('fact_identity', sa.String(64), nullable=True))
    op.add_column("user_memories", sa.Column('content_hash', sa.String(64), nullable=True))
    op.add_column("user_memories", sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_memories", sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_memories", sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_memories", sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_memories", sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_memories", sa.Column('supersedes_id', sa.String(64), nullable=True))
    op.add_column("user_memories", sa.Column('policy_version', sa.String(32), nullable=False, server_default=sa.text("'personal-v1'")))
    op.add_column("user_memories", sa.Column('acl_epoch', sa.Integer(), nullable=False, server_default=sa.text('1')))
    op.create_index("uq_user_memories_fact_identity", "user_memories", ["fact_identity"], unique=True)
    op.create_index("ix_user_memories_authority", "user_memories", ["user_id", "workspace_id", "project_id", "status", "confirmation_status"])
    op.execute("UPDATE user_memories SET confirmation_status = CASE WHEN status = 'CANDIDATE' THEN 'LEGACY_CANDIDATE' WHEN status = 'ACTIVE' AND owner IN ('USER_CONFIRMED', 'OPERATOR_CONFIRMED') THEN 'CONFIRMED' ELSE 'PENDING' END, recorded_at = created_at, deleted_at = CASE WHEN status = 'DEPRECATED' THEN updated_at ELSE NULL END")
    op.create_table('memory_revisions',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('memory_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('value', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('confirmation_status', sa.String(24), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('reason', sa.String(64), nullable=False),
        sa.Column('actor_user_id', sa.String(64), nullable=True),
        sa.Column('source_set_hash', sa.String(64), nullable=True),
        sa.Column('request_id', sa.String(128), nullable=True),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('memory_id', 'request_id', name='uq_memory_revision_request'),
        sa.ForeignKeyConstraint(['memory_id'], ['user_memories.id']),
        sa.UniqueConstraint('memory_id', 'revision', name='uq_memory_revision')
    )
    op.create_index('ix_memory_revisions_scope', 'memory_revisions', ['user_id', 'workspace_id', 'memory_id'], unique=False)
    op.create_table('memory_sources',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('source_revision', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('source_kind', sa.String(32), nullable=False),
        sa.Column('session_id', sa.String(64), nullable=True),
        sa.Column('branch_id', sa.String(128), nullable=True),
        sa.Column('turn_id', sa.String(128), nullable=True),
        sa.Column('message_id', sa.String(64), nullable=True),
        sa.Column('part_id', sa.String(64), nullable=True),
        sa.Column('start_seq', sa.Integer(), nullable=True),
        sa.Column('end_seq', sa.Integer(), nullable=True),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('source_metadata', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('acl_epoch', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('status', sa.String(16), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('id', 'source_revision', name='uq_memory_source_version')
    )
    op.create_index('ix_memory_sources_scope', 'memory_sources', ['user_id', 'workspace_id', 'project_id', 'status'], unique=False)
    op.create_index('ix_memory_sources_session', 'memory_sources', ['session_id', 'message_id'], unique=False)
    op.create_table('memory_source_links',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('memory_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('source_revision', sa.Integer(), nullable=False),
        sa.Column('relation', sa.String(16), nullable=False, server_default=sa.text("'SUPPORTS'")),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('memory_id', 'revision', 'source_id', 'source_revision', name='uq_memory_source_link'),
        sa.ForeignKeyConstraint(['source_id'], ['memory_sources.id']),
        sa.ForeignKeyConstraint(['memory_id'], ['user_memories.id'])
    )
    op.create_index('ix_memory_source_links_source', 'memory_source_links', ['source_id', 'memory_id'], unique=False)
    op.create_table('memory_outbox',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('event_id', sa.String(128), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('object_kind', sa.String(16), nullable=False),
        sa.Column('object_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('operation', sa.String(24), nullable=False),
        sa.Column('index_generation', sa.String(64), nullable=False, server_default=sa.text("'memory-v1'")),
        sa.Column('payload', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('status', sa.String(16), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('lease_owner', sa.String(128), nullable=True),
        sa.Column('lease_generation', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', name=None)
    )
    op.create_index('ix_memory_outbox_claim', 'memory_outbox', ['status', 'available_at', 'priority'], unique=False)
    op.create_index('ix_memory_outbox_object', 'memory_outbox', ['object_kind', 'object_id', 'revision'], unique=False)
    op.create_table('memory_index_state',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('object_kind', sa.String(16), nullable=False),
        sa.Column('object_id', sa.String(64), nullable=False),
        sa.Column('index_generation', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('desired_revision', sa.Integer(), nullable=False),
        sa.Column('indexed_revision', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('chunk_ids', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('chunk_manifest_hash', sa.String(64), nullable=True),
        sa.Column('config_hash', sa.String(64), nullable=True),
        sa.Column('status', sa.String(24), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_kind', 'object_id', 'index_generation', name='uq_memory_index_object')
    )
    op.create_table('memory_tombstones',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('object_kind', sa.String(16), nullable=False),
        sa.Column('object_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('source_hash', sa.String(64), nullable=True),
        sa.Column('fact_key', sa.String(255), nullable=True),
        sa.Column('scope', sa.String(16), nullable=False, server_default=sa.text("'MEMORY'")),
        sa.Column('purge_status', sa.String(24), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_kind', 'object_id', name='uq_memory_tombstone_object')
    )
    op.create_index('ix_memory_tombstones_suppression', 'memory_tombstones', ['user_id', 'workspace_id', 'project_id', 'content_hash'], unique=False)
    op.create_table('memory_debug_runs',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('attempt_id', sa.String(128), nullable=False),
        sa.Column('parent_run_id', sa.String(64), nullable=True),
        sa.Column('session_id', sa.String(64), nullable=True),
        sa.Column('turn_id', sa.String(128), nullable=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('policy_version', sa.String(32), nullable=False, server_default=sa.text("'personal-v1'")),
        sa.Column('input_hash', sa.String(64), nullable=True),
        sa.Column('input_snapshot', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('source_refs', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('usage', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_memory_debug_runs_scope', 'memory_debug_runs', ['user_id', 'workspace_id', 'created_at'], unique=False)
    op.create_table('memory_debug_steps',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('run_id', sa.String(64), nullable=False),
        sa.Column('phase', sa.String(32), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('reason_code', sa.String(64), nullable=True),
        sa.Column('data', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('usage', postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['run_id'], ['memory_debug_runs.id'])
    )
    op.create_index('ix_memory_debug_steps_run', 'memory_debug_steps', ['run_id', 'created_at'], unique=False)
    op.execute("INSERT INTO memory_revisions (id, memory_id, revision, user_id, workspace_id, project_id, value, status, confirmation_status, reason, created_at) SELECT id, id, 1, user_id, workspace_id, project_id, value, status, confirmation_status, 'legacy_import', created_at FROM user_memories")
    if not op.get_context().as_sql:
        bind = op.get_bind()
        rows = bind.execute(sa.text("SELECT id, user_id, workspace_id, project_id, value, status, updated_at FROM user_memories")).mappings().all()
        for row in rows:
            value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"] or {}
            summary = value.get("summary", "")
            digest = sha256(re.sub(r"\s+", " ", summary).strip().encode()).hexdigest()
            bind.execute(sa.text("UPDATE user_memories SET content_hash = :hash WHERE id = :id"), {"hash": digest, "id": row["id"]})
            if row["status"] == "DEPRECATED":
                bind.execute(sa.text("INSERT INTO memory_tombstones (id, object_kind, object_id, revision, user_id, workspace_id, project_id, content_hash, scope, purge_status, deleted_at) VALUES (:id, 'memory', :id, 1, :user_id, :workspace_id, :project_id, :hash, 'FACT', 'PENDING', :deleted_at)"),
                    {"id": row["id"], "user_id": row["user_id"], "workspace_id": row["workspace_id"], "project_id": row["project_id"], "hash": digest, "deleted_at": row["updated_at"]})


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM memory_sources")).scalar() or bind.execute(sa.text("SELECT count(*) FROM memory_revisions WHERE reason != 'legacy_import'")).scalar() or bind.execute(sa.text("SELECT count(*) FROM memory_tombstones WHERE revision > 1 OR object_kind != 'memory'")).scalar():
        raise RuntimeError("Memory authority contains new evidence, revisions or tombstones; retain schema for application rollback")
    op.drop_table('memory_debug_steps')
    op.drop_table('memory_debug_runs')
    op.drop_table('memory_tombstones')
    op.drop_table('memory_index_state')
    op.drop_table('memory_outbox')
    op.drop_table('memory_source_links')
    op.drop_table('memory_sources')
    op.drop_table('memory_revisions')
    op.drop_index("ix_user_memories_authority", table_name="user_memories")
    op.drop_index("uq_user_memories_fact_identity", table_name="user_memories")
    with op.batch_alter_table("user_memories") as batch:
        batch.drop_column('acl_epoch')
        batch.drop_column('policy_version')
        batch.drop_column('supersedes_id')
        batch.drop_column('deleted_at')
        batch.drop_column('valid_to')
        batch.drop_column('valid_from')
        batch.drop_column('recorded_at')
        batch.drop_column('occurred_at')
        batch.drop_column('content_hash')
        batch.drop_column('fact_identity')
        batch.drop_column('fact_key')
        batch.drop_column('confirmation_actor_id')
        batch.drop_column('confirmation_status')
        batch.drop_column('visibility')
        batch.drop_column('revision')
