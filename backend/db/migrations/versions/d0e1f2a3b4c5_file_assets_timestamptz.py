"""Make file_assets timestamps timezone-aware, like every other table.

`add_file_assets` declared these two columns with `sa.DateTime()` while the
ORM's type-annotation map says `DateTime(timezone=True)` — so SQLAlchemy bound
the parameter as timestamptz and asyncpg localised the naive value we passed,
storing every timestamp shifted by the writing process's UTC offset. Nothing
displayed these until the resource centre, which is how it surfaced.

The columns become timestamptz (matching the other 12 tables) and the writers
now pass aware datetimes. Existing rows keep their stored instant: the shift
that was applied depends on the timezone of the machine that wrote each row,
which SQL cannot recover — a deployment that ran in UTC has correct history,
and one that did not should repair its own rows by its own offset.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in ("created_at", "deleted_at"):
        op.execute(
            f"ALTER TABLE file_assets ALTER COLUMN {column} "
            f"TYPE TIMESTAMP WITH TIME ZONE USING {column} AT TIME ZONE 'UTC'"
        )


def downgrade() -> None:
    for column in ("created_at", "deleted_at"):
        op.execute(
            f"ALTER TABLE file_assets ALTER COLUMN {column} "
            f"TYPE TIMESTAMP WITHOUT TIME ZONE USING {column} AT TIME ZONE 'UTC'"
        )
