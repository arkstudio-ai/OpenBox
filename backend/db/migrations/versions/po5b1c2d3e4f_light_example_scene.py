"""Light example pick: a scene on each example, and the pick on the conversation (docs/LIGHT_EXAMPLE_PICK_PLAN.md).

``light_examples.scene`` is what the piece was made for (a poster's purpose, a
cut's type, a menu's style); ``pick_count`` is how often owners chose it on the
card. ``sessions.light_scene`` / ``light_example_id`` are the scene and the
example the owner picked in a guided conversation.

Revision ID: po5b1c2d3e4f
Revises: pn4a0b1c2d3e
"""
from alembic import op
import sqlalchemy as sa

revision = "po5b1c2d3e4f"
down_revision = "pn4a0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("light_examples") as batch:
        batch.add_column(sa.Column("scene", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("pick_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.create_index("ix_light_examples_scene", "light_examples", ["entry_id", "scene", "status"])
    # A menu's scene is its style, which the library already kept in ``subtype``.
    op.execute("UPDATE light_examples SET scene = subtype WHERE entry_id = 'menu' AND subtype IN "
               "('menu_mono', 'menu_red', 'menu_chalk', 'menu_dark')")
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("light_scene", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("light_example_id", sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("light_example_id")
        batch.drop_column("light_scene")
    op.drop_index("ix_light_examples_scene", table_name="light_examples")
    with op.batch_alter_table("light_examples") as batch:
        batch.drop_column("pick_count")
        batch.drop_column("scene")
