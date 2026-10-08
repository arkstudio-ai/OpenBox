"""Resource control lease timestamps carry their time zone, as the model does.

The table was created with ``timestamp without time zone`` while the model and
every write use aware UTC datetimes. PostgreSQL then stored them in the
session time zone and reads treated them as UTC, which skews lease expiry on a
server whose time zone is not UTC. Existing values were written in the server
time zone, so they are converted from it. SQLite keeps no zone; nothing to do.

Revision ID: pbb1c2d3e4f5
Revises: pbb0a1b2c3d4
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers
revision: str = "pbb1c2d3e4f5"
down_revision: Union[str, None] = "pbb0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("expires_at", "created_at", "updated_at")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE resource_control_leases ALTER COLUMN {column} TYPE TIMESTAMP WITH TIME ZONE "
            f"USING {column} AT TIME ZONE current_setting('TimeZone')"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE resource_control_leases ALTER COLUMN {column} TYPE TIMESTAMP WITHOUT TIME ZONE "
            f"USING {column} AT TIME ZONE current_setting('TimeZone')"
        )
