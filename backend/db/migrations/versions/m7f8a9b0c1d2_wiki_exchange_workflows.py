"""Wiki exchange, profiles, typed records and durable workflows.

Revision ID: m7f8a9b0c1d2
Revises: m6e7f8a9b0c1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "m7f8a9b0c1d2"
down_revision = "m6e7f8a9b0c1"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade():
    op.create_table('wiki_exchange_bundles',
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('manifest', JSON, nullable=False),
        sa.Column('attachments', JSON, nullable=False),
        sa.Column('warnings', JSON, nullable=False),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('domain', 'content_hash', name='uq_wiki_exchange_bundle'),
    )
    op.create_table('wiki_exchange_documents',
        sa.Column('bundle_id', sa.String(64), nullable=False),
        sa.Column('path', sa.String(512), nullable=False),
        sa.Column('frontmatter', JSON, nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('slug', sa.String(80), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('page_id', sa.String(64), nullable=True),
        sa.Column('source_id', sa.String(64), nullable=True),
        sa.Column('source_ids', JSON, nullable=False),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('bundle_id', 'path', name='uq_wiki_exchange_document'),
    )
    op.create_index('ix_wiki_exchange_document_scope', 'wiki_exchange_documents', ['user_id', 'workspace_id', 'project_id', 'status'], unique=False)
    op.create_table('wiki_profiles',
        sa.Column('domain', sa.String(64), nullable=False),
        sa.Column('profile_key', sa.String(80), nullable=False),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('definition', JSON, nullable=False),
        sa.Column('definition_hash', sa.String(64), nullable=False),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('domain', 'profile_key', name='uq_wiki_profile_key'),
    )
    op.create_table('wiki_typed_records',
        sa.Column('profile_id', sa.String(64), nullable=False),
        sa.Column('entity_type', sa.String(80), nullable=False),
        sa.Column('slug', sa.String(80), nullable=False),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('fields', JSON, nullable=False),
        sa.Column('page_id', sa.String(64), nullable=False),
        sa.Column('page_revision', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('profile_id', 'entity_type', 'slug', name='uq_wiki_typed_record'),
    )
    op.create_index('ix_wiki_typed_record_scope', 'wiki_typed_records', ['user_id', 'workspace_id', 'project_id'], unique=False)
    op.create_table('wiki_artifacts',
        sa.Column('profile_id', sa.String(64), nullable=False),
        sa.Column('artifact_type', sa.String(80), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('media_type', sa.String(80), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('record_manifest', JSON, nullable=False),
        sa.Column('run_id', sa.String(64), nullable=True),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('wiki_workflow_runs',
        sa.Column('profile_id', sa.String(64), nullable=False),
        sa.Column('profile_hash', sa.String(64), nullable=False),
        sa.Column('definition', JSON, nullable=False),
        sa.Column('workflow_id', sa.String(80), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('inputs', JSON, nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('stage_index', sa.Integer(), nullable=False),
        sa.Column('stages', JSON, nullable=False),
        sa.Column('reason', sa.String(800), nullable=True),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('project_id', sa.String(64), nullable=True),
        sa.Column('visibility', sa.String(16), nullable=False, server_default=sa.text("'PERSONAL'")),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'workspace_id', 'request_id', name='uq_wiki_workflow_request'),
    )
    op.create_index('ix_wiki_workflow_scope', 'wiki_workflow_runs', ['user_id', 'workspace_id', 'project_id', 'status'], unique=False)
    op.create_table('wiki_workflow_events',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('run_id', sa.String(64), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(40), nullable=False),
        sa.Column('stage_id', sa.String(80), nullable=True),
        sa.Column('detail', JSON, nullable=False),
        sa.Column('actor_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('run_id', 'request_id', name='uq_wiki_workflow_event_request'),
        sa.UniqueConstraint('run_id', 'version', name='uq_wiki_workflow_event_version'),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM wiki_exchange_bundles LIMIT 1")).first():
        raise RuntimeError("Refusing to drop non-empty Wiki platform tables")
    if connection.execute(sa.text("SELECT 1 FROM wiki_exchange_documents LIMIT 1")).first():
        raise RuntimeError("Refusing to drop non-empty Wiki platform tables")
    if connection.execute(sa.text("SELECT 1 FROM wiki_profiles LIMIT 1")).first():
        raise RuntimeError("Refusing to drop non-empty Wiki platform tables")
    if connection.execute(sa.text("SELECT 1 FROM wiki_typed_records LIMIT 1")).first():
        raise RuntimeError("Refusing to drop non-empty Wiki platform tables")
    if connection.execute(sa.text("SELECT 1 FROM wiki_artifacts LIMIT 1")).first():
        raise RuntimeError("Refusing to drop non-empty Wiki platform tables")
    if connection.execute(sa.text("SELECT 1 FROM wiki_workflow_runs LIMIT 1")).first():
        raise RuntimeError("Refusing to drop non-empty Wiki platform tables")
    if connection.execute(sa.text("SELECT 1 FROM wiki_workflow_events LIMIT 1")).first():
        raise RuntimeError("Refusing to drop non-empty Wiki platform tables")
    op.drop_table('wiki_workflow_events')
    op.drop_table('wiki_workflow_runs')
    op.drop_table('wiki_artifacts')
    op.drop_table('wiki_typed_records')
    op.drop_table('wiki_profiles')
    op.drop_table('wiki_exchange_documents')
    op.drop_table('wiki_exchange_bundles')
