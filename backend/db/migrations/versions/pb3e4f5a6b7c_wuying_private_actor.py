"""Pin private actor identities on the existing Wuying desktop.

Revision ID: pb3e4f5a6b7c
Revises: pb2d3e4f5a6b
"""
from alembic import op
import sqlalchemy as sa
from db.base import JSONType

revision = "pb3e4f5a6b7c"
down_revision = "pb2d3e4f5a6b"
branch_labels = None
depends_on = None

_OLD_ISOLATION = "(kind = 'sandbox' AND isolation_mode = 'process_uid') OR (kind = 'browser_profile' AND isolation_mode IN ('chromium_sandbox','container_uid'))"
_OLD_VOLUMES = "(kind = 'sandbox' AND workspace_volume IS NOT NULL) OR (kind = 'browser_profile' AND workspace_volume IS NULL)"
_ISOLATION = "(provider = 'private_docker_v1' AND ((kind = 'sandbox' AND isolation_mode = 'process_uid') OR (kind = 'browser_profile' AND isolation_mode IN ('chromium_sandbox','container_uid')))) OR (provider = 'private_wuying_v1' AND ((kind = 'sandbox' AND isolation_mode = 'guest_uid_mount') OR (kind = 'browser_profile' AND isolation_mode = 'wuying_guest_uid')))"
_VOLUMES = "(provider = 'private_docker_v1' AND data_volume IS NOT NULL AND ((kind = 'sandbox' AND workspace_volume IS NOT NULL) OR (kind = 'browser_profile' AND workspace_volume IS NULL))) OR (provider = 'private_wuying_v1' AND workspace_volume IS NULL AND data_volume IS NULL)"


def upgrade():
    with op.batch_alter_table("private_runtimes") as batch:
        batch.add_column(sa.Column("provider_identity", JSONType(), nullable=False, server_default="{}"))
        batch.alter_column("data_volume", existing_type=sa.String(128), nullable=True)
        batch.drop_constraint("uq_private_runtime_actor_kind", type_="unique")
        batch.create_unique_constraint("uq_private_runtime_actor_kind", ["workspace_id", "actor_user_id", "kind", "provider"])
        batch.drop_constraint("ck_private_runtime_isolation", type_="check")
        batch.drop_constraint("ck_private_runtime_volumes", type_="check")
        batch.create_check_constraint("ck_private_runtime_isolation", _ISOLATION)
        batch.create_check_constraint("ck_private_runtime_volumes", _VOLUMES)
    with op.batch_alter_table("browser_resource_bindings") as batch:
        batch.add_column(sa.Column("provider", sa.String(32), nullable=False, server_default="private_docker_v1"))
        batch.drop_constraint("uq_browser_resource_actor", type_="unique")
        batch.create_unique_constraint("uq_browser_resource_actor", ["actor_user_id", "workspace_id", "provider"])


def downgrade():
    # A downgrade must never discard or reinterpret an enrolled Wuying actor.
    if op.get_bind().execute(sa.text("SELECT 1 FROM private_runtimes WHERE provider = 'private_wuying_v1' LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while Wuying private actor bindings exist")
    if op.get_bind().execute(sa.text("SELECT 1 FROM browser_resource_bindings WHERE provider = 'private_wuying_v1' LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while Wuying browser bindings exist")
    with op.batch_alter_table("browser_resource_bindings") as batch:
        batch.drop_constraint("uq_browser_resource_actor", type_="unique")
        batch.create_unique_constraint("uq_browser_resource_actor", ["actor_user_id", "workspace_id"])
        batch.drop_column("provider")
    with op.batch_alter_table("private_runtimes") as batch:
        batch.drop_constraint("ck_private_runtime_isolation", type_="check")
        batch.drop_constraint("ck_private_runtime_volumes", type_="check")
        batch.create_check_constraint("ck_private_runtime_isolation", _OLD_ISOLATION)
        batch.create_check_constraint("ck_private_runtime_volumes", _OLD_VOLUMES)
        batch.alter_column("data_volume", existing_type=sa.String(128), nullable=False)
        batch.drop_constraint("uq_private_runtime_actor_kind", type_="unique")
        batch.create_unique_constraint("uq_private_runtime_actor_kind", ["workspace_id", "actor_user_id", "kind"])
        batch.drop_column("provider_identity")
