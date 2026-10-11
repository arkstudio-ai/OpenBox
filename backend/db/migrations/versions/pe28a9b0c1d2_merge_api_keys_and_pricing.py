"""Merge the public-API key branch with the mainline (voice/memory + pricing rules).

Revision ID: pe28a9b0c1d2
Revises: e7c9a1b3d5f0, pd17c8d9e0f1

Replaces the earlier merge revision that reused the id ``pd17c8d9e0f1``,
which main then assigned to ``pricing_rules``. A database stamped at the
old merge point (the Gaode host) must be re-stamped at
``e7c9a1b3d5f0 pd17c8d9e0f1``'s parents (``e7c9a1b3d5f0 pc06b7c8d9e0``)
before upgrading, so ``pricing_rules`` is actually created.
"""

revision = "pe28a9b0c1d2"
down_revision = ("e7c9a1b3d5f0", "pd17c8d9e0f1")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
