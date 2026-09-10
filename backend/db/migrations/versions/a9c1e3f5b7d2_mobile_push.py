"""Exclusive mobile login and native push outbox.

Revision ID: a9c1e3f5b7d2
Revises: b8d0f2a4c6e8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a9c1e3f5b7d2"
down_revision = "b8d0f2a4c6e8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('mobile_sessions',
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('session_id', sa.String(length=64), nullable=False),
    sa.Column('installation_id', sa.String(length=128), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('installation_id'),
    sa.UniqueConstraint('session_id')
    )
    op.create_table('push_devices',
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('mobile_session_id', sa.String(length=64), nullable=False),
    sa.Column('binding_id', sa.String(length=64), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('apns_environment', sa.String(length=16), nullable=False),
    sa.Column('bundle_id', sa.String(length=255), nullable=False),
    sa.Column('app_version', sa.String(length=64), nullable=False),
    sa.Column('locale', sa.String(length=32), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('binding_id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('push_messages',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('event_key', sa.String(length=255), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=True),
    sa.Column('payload', sa.Text().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'event_key', name='uq_push_message_event')
    )
    op.create_table('push_deliveries',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('message_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('mobile_session_id', sa.String(length=64), nullable=False),
    sa.Column('binding_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_id', sa.String(length=64), nullable=True),
    sa.Column('provider_message_id', sa.String(length=255), nullable=True),
    sa.Column('error', sa.String(length=128), nullable=True),
    sa.ForeignKeyConstraint(['message_id'], ['push_messages.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id')
    )
    op.create_index('ix_push_delivery_due', 'push_deliveries', ['status', 'available_at', 'lease_until'], unique=False)


def downgrade():
    op.drop_table("push_deliveries")
    op.drop_table("push_messages")
    op.drop_table("push_devices")
    op.drop_table("mobile_sessions")
