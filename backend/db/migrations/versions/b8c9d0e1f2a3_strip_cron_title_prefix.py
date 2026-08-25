"""Strip the legacy "[定时] " / "[Cron] " prefix from cron session titles.

The sidebar identifies cron run sessions with a clock badge now; the text
prefix predates that and reads as noise.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-25
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for prefix in ("[定时] ", "[Cron] "):
        op.execute(
            "UPDATE sessions SET title = SUBSTRING(title FROM %d) "
            "WHERE kind = 'cron' AND title LIKE '%s%%'" % (len(prefix) + 1, prefix)
        )


def downgrade() -> None:
    pass  # cosmetic data fix; nothing to restore
