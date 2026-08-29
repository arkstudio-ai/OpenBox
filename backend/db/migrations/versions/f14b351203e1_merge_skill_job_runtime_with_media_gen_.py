"""merge skill-job runtime with media-gen routing

Revision ID: f14b351203e1
Revises: a6c8e0f2b4d6, c5e7f9a1b3d5
Create Date: 2026-08-29 15:35:32.143539
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = 'f14b351203e1'
down_revision: Union[str, None] = ('a6c8e0f2b4d6', 'c5e7f9a1b3d5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
