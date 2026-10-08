"""Join main's admin billing head with the personal assistant chain.

Production (main) is at ``f8b3d6a1c092``; it upgrades through the assistant
chain and then this point. A database that already ran the assistant chain
(``pbaf0a1b2c3d``) applies only ``f8b3d6a1c092`` and then this point. The two
branches touch disjoint tables, so the merge itself changes nothing.

Revision ID: pbb0a1b2c3d4
Revises: f8b3d6a1c092, pbaf0a1b2c3d
"""
from typing import Sequence, Union


# revision identifiers
revision: str = "pbb0a1b2c3d4"
down_revision: Union[str, None] = ("f8b3d6a1c092", "pbaf0a1b2c3d")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
