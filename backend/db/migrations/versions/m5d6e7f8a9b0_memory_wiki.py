"""Host-owned Wiki candidates, dependencies and fenced compilation jobs.

Revision ID: m5d6e7f8a9b0
Revises: m4c5d6e7f8a9
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "m5d6e7f8a9b0"
down_revision = "m4c5d6e7f8a9"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade():
    op.create_table('memory_wiki_pages',
        sa.Column('id', sa.String(64), primary_key=True, nullable=False),
        sa.Column('target_identity', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64)),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('slug', sa.String(80), nullable=False),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('body', sa.Text()),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('source_manifest', JSON, nullable=False),
        sa.Column('memory_manifest', JSON, nullable=False),
        sa.Column('paragraphs', JSON, nullable=False),
        sa.Column('acl_epoch', sa.Integer(), nullable=False),
        sa.Column('policy_version', sa.String(64), nullable=False),
        sa.Column('model', sa.String(128), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('candidate_id', sa.String(64), nullable=False),
        sa.Column('invalidation_reason', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('target_identity'))
    op.create_index('ix_memory_wiki_pages_scope', 'memory_wiki_pages', ['user_id', 'workspace_id', 'project_id', 'status'], unique=False)
    op.create_table('memory_wiki_candidates',
        sa.Column('id', sa.String(64), primary_key=True, nullable=False),
        sa.Column('job_id', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64)),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('target_identity', sa.String(64), nullable=False),
        sa.Column('target_page_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('candidate_hash', sa.String(64), nullable=False),
        sa.Column('draft', JSON, nullable=False),
        sa.Column('source_manifest', JSON, nullable=False),
        sa.Column('memory_manifest', JSON, nullable=False),
        sa.Column('acl_epoch', sa.Integer(), nullable=False),
        sa.Column('expected_target_revision', sa.Integer(), nullable=False),
        sa.Column('expected_target_hash', sa.String(64)),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(24), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('reason_code', sa.String(64)),
        sa.Column('usage', JSON, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True)),
        sa.Column('approved_by', sa.String(64)),
        sa.Column('request_id', sa.String(128)),
        sa.UniqueConstraint('job_id'))
    op.create_index('ix_memory_wiki_candidates_scope', 'memory_wiki_candidates', ['user_id', 'workspace_id', 'project_id', 'status'], unique=False)
    op.create_table('memory_wiki_dependencies',
        sa.Column('id', sa.String(64), primary_key=True, nullable=False),
        sa.Column('page_id', sa.String(64), nullable=False),
        sa.Column('page_revision', sa.Integer(), nullable=False),
        sa.Column('object_kind', sa.String(16), nullable=False),
        sa.Column('object_id', sa.String(64), nullable=False),
        sa.Column('object_revision', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.UniqueConstraint('page_id', 'page_revision', 'object_kind', 'object_id', name='uq_memory_wiki_dependency'))
    op.create_index('ix_memory_wiki_dependencies_reverse', 'memory_wiki_dependencies', ['object_kind', 'object_id', 'page_id'], unique=False)
    op.create_table('memory_wiki_jobs',
        sa.Column('id', sa.String(64), primary_key=True, nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64)),
        sa.Column('target_identity', sa.String(64), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(128)),
        sa.Column('spec', JSON, nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('lease_owner', sa.String(128)),
        sa.Column('lease_generation', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_error', sa.String(64)),
        sa.Column('candidate_id', sa.String(64)),
        sa.Column('usage', JSON, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'workspace_id', 'request_id', name='uq_memory_wiki_job_request'))
    op.create_index('ix_memory_wiki_jobs_claim', 'memory_wiki_jobs', ['status', 'available_at'], unique=False)
    op.create_index('ix_memory_wiki_jobs_input', 'memory_wiki_jobs', ['target_identity', 'input_hash'], unique=False)


def downgrade():
    op.drop_table('memory_wiki_jobs')
    op.drop_table('memory_wiki_dependencies')
    op.drop_table('memory_wiki_candidates')
    op.drop_table('memory_wiki_pages')
