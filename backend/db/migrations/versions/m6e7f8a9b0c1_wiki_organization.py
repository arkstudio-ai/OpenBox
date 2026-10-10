"""Wiki concepts, incremental extraction, organization jobs and maintenance policy.

Revision ID: m6e7f8a9b0c1
Revises: m5d6e7f8a9b0
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "m6e7f8a9b0c1"
down_revision = "m5d6e7f8a9b0"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade():
    op.create_table('wiki_concepts',
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('canonical_key', sa.String(160), nullable=False),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('aliases', JSON, nullable=False),
        sa.Column('category', sa.String(80), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('slug', sa.String(80), nullable=False),
        sa.Column('page_id', sa.String(64), nullable=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('merged_into', sa.String(64), nullable=True),
        sa.Column('user_edited', sa.Boolean(), nullable=False),
        sa.Column('extra_metadata', JSON, nullable=False),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('domain', 'canonical_key', name='uq_wiki_concept_canonical'),
    )
    op.create_index('ix_wiki_concept_scope', 'wiki_concepts', ['user_id', 'workspace_id', 'project_id', 'status'], unique=False)
    op.create_table('wiki_concept_bindings',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('concept_id', sa.String(64), nullable=False),
        sa.Column('memory_id', sa.String(64), nullable=False),
        sa.Column('memory_revision', sa.Integer(), nullable=False),
        sa.Column('extraction_id', sa.String(64), nullable=False),
        sa.Column('source_manifest', JSON, nullable=False),
        sa.Column('evidence', JSON, nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.UniqueConstraint('concept_id', 'memory_id', name='uq_wiki_concept_binding'),
    )
    op.create_index('ix_wiki_binding_memory', 'wiki_concept_bindings', ['memory_id', 'status'], unique=False)
    op.create_table('wiki_concept_extractions',
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('cache_key', sa.String(64), nullable=False),
        sa.Column('memory_id', sa.String(64), nullable=False),
        sa.Column('memory_revision', sa.Integer(), nullable=False),
        sa.Column('source_manifest', JSON, nullable=False),
        sa.Column('memory_manifest', JSON, nullable=False),
        sa.Column('acl_epoch', sa.Integer(), nullable=False),
        sa.Column('concepts', JSON, nullable=False),
        sa.Column('model', sa.String(128), nullable=False),
        sa.Column('prompt_version', sa.String(64), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('usage', JSON, nullable=False),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('cache_key'),
    )
    op.create_index('ix_wiki_extraction_memory', 'wiki_concept_extractions', ['memory_id', 'status'], unique=False)
    op.create_table('wiki_organization_runs',
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('spec', JSON, nullable=False),
        sa.Column('result', JSON, nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('cursor', sa.Integer(), nullable=False),
        sa.Column('model_calls', sa.Integer(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_owner', sa.String(128), nullable=True),
        sa.Column('lease_generation', sa.Integer(), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason_code', sa.String(80), nullable=True),
        sa.Column('automation_id', sa.String(64), nullable=True),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'workspace_id', 'request_id', name='uq_wiki_organization_request'),
    )
    op.create_index('ix_wiki_organization_claim', 'wiki_organization_runs', ['status', 'available_at'], unique=False)
    op.create_index('ix_wiki_organization_domain', 'wiki_organization_runs', ['domain', 'created_at'], unique=False)
    op.create_table('wiki_maintenance_policies',
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('compile_pages', sa.Boolean(), nullable=False),
        sa.Column('call_limit', sa.Integer(), nullable=False),
        sa.Column('calls_used', sa.Integer(), nullable=False),
        sa.Column('window_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_input_hash', sa.String(64), nullable=True),
        sa.Column('last_run_id', sa.String(64), nullable=True),
        sa.Column('reason_code', sa.String(80), nullable=True),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('domain'),
    )
    op.create_table('wiki_relations',
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('identity', sa.String(64), nullable=False),
        sa.Column('relation_type', sa.String(80), nullable=False),
        sa.Column('from_kind', sa.String(24), nullable=False),
        sa.Column('from_id', sa.String(64), nullable=False),
        sa.Column('to_kind', sa.String(24), nullable=False),
        sa.Column('to_id', sa.String(64), nullable=False),
        sa.Column('attributes', JSON, nullable=False),
        sa.Column('evidence', JSON, nullable=False),
        sa.Column('memory_manifest', JSON, nullable=False),
        sa.Column('source_manifest', JSON, nullable=False),
        sa.Column('acl_epoch', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.String(64), nullable=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('identity'),
    )
    op.create_index('ix_wiki_relation_scope', 'wiki_relations', ['user_id', 'workspace_id', 'project_id', 'status'], unique=False)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM wiki_concepts LIMIT 1")).first():
        raise RuntimeError("Wiki platform downgrade would discard data; export and explicitly plan migration first")
    if bind.execute(sa.text("SELECT 1 FROM wiki_concept_bindings LIMIT 1")).first():
        raise RuntimeError("Wiki platform downgrade would discard data; export and explicitly plan migration first")
    if bind.execute(sa.text("SELECT 1 FROM wiki_concept_extractions LIMIT 1")).first():
        raise RuntimeError("Wiki platform downgrade would discard data; export and explicitly plan migration first")
    if bind.execute(sa.text("SELECT 1 FROM wiki_organization_runs LIMIT 1")).first():
        raise RuntimeError("Wiki platform downgrade would discard data; export and explicitly plan migration first")
    if bind.execute(sa.text("SELECT 1 FROM wiki_maintenance_policies LIMIT 1")).first():
        raise RuntimeError("Wiki platform downgrade would discard data; export and explicitly plan migration first")
    if bind.execute(sa.text("SELECT 1 FROM wiki_relations LIMIT 1")).first():
        raise RuntimeError("Wiki platform downgrade would discard data; export and explicitly plan migration first")
    op.drop_table('wiki_relations')
    op.drop_table('wiki_maintenance_policies')
    op.drop_table('wiki_organization_runs')
    op.drop_table('wiki_concept_extractions')
    op.drop_table('wiki_concept_bindings')
    op.drop_table('wiki_concepts')
