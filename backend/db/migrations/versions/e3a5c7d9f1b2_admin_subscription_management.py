"""Independent subscription identities for audited operator grants.

Revision ID: e3a5c7d9f1b2
Revises: d0a2c4e6f8b1
"""
from alembic import op
import sqlalchemy as sa

revision = "e3a5c7d9f1b2"
down_revision = "d0a2c4e6f8b1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("billing_subscriptions", sa.Column("id", sa.String(64), nullable=True))
    op.add_column("billing_subscriptions", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE billing_subscriptions SET id = order_id")
    pk = sa.inspect(op.get_bind()).get_pk_constraint("billing_subscriptions")["name"]
    with op.batch_alter_table("billing_subscriptions", naming_convention={"pk": "pk_%(table_name)s"}) as batch:
        batch.drop_constraint(pk or "pk_billing_subscriptions", type_="primary")
        batch.alter_column("id", existing_type=sa.String(64), nullable=False)
        batch.alter_column("order_id", existing_type=sa.String(64), nullable=True)
        batch.create_primary_key("pk_billing_subscriptions", ["id"])
        batch.create_unique_constraint("uq_subscription_order", ["order_id"])


def downgrade():
    used = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM billing_subscriptions WHERE order_id IS NULL OR cancelled_at IS NOT NULL OR id != order_id"
    )).scalar_one()
    if used:
        raise RuntimeError("Subscription downgrade refused: operator-managed terms exist")
    with op.batch_alter_table("billing_subscriptions") as batch:
        batch.drop_constraint("uq_subscription_order", type_="unique")
        batch.drop_constraint("pk_billing_subscriptions", type_="primary")
        batch.alter_column("order_id", existing_type=sa.String(64), nullable=False)
        batch.create_primary_key("pk_billing_subscriptions", ["order_id"])
        batch.drop_column("id")
        batch.drop_column("cancelled_at")
