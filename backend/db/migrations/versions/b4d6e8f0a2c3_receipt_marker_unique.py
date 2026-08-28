"""Database-enforced idempotency for skill-job chat receipts.

write_receipt used check-then-insert on messages.client_message_id, which two
concurrent outbox publishers can both pass. A partial unique index scoped to
the 'sjr:' marker prefix makes the second insert fail instead, without
constraining ordinary client-supplied message ids.

Revision ID: b4d6e8f0a2c3
Revises: a2c4e6f8b0d1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d6e8f0a2c3"
down_revision: Union[str, None] = "a2c4e6f8b0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_messages_receipt_marker",
        "messages",
        ["session_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id LIKE 'sjr:%'"),
        sqlite_where=sa.text("client_message_id LIKE 'sjr:%'"),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_receipt_marker", table_name="messages")
