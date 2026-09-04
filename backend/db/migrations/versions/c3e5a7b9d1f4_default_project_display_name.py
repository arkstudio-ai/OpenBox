"""Call the starter project 默认空间 without moving its directory.

The slug is what the sandbox uses as a path segment (/workspace/default), so
only the display name changes. A project the user has already renamed keeps
that name — the update is scoped to rows still carrying the seeded pair.

Revision ID: c3e5a7b9d1f4
Revises: f7b9d1e3a5c8
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c3e5a7b9d1f4"
down_revision: Union[str, None] = "f7b9d1e3a5c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE projects SET name = '默认空间' "
        "WHERE slug = 'default' AND name = 'Default'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE projects SET name = 'Default' "
        "WHERE slug = 'default' AND name = '默认空间'"
    )
