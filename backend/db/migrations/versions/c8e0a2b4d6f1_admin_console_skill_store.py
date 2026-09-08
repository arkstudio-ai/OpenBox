"""Moderated skill store: listing state, catalogue installs, catalogue overrides.

Adds the operator-owned axis to ``user_skills`` (``listing`` and friends),
widens ``skill_installs`` from "community skills only" to every store entry,
and creates ``catalog_overrides`` for the entries that live in code.

Downgrade caveat: the pre-migration ``skill_installs`` schema has no way to
represent a catalogue install (``user_skill_id`` was NOT NULL and pointed at a
``user_skills`` row that catalogue entries do not have), so downgrading deletes
those provenance rows.  Community installs are untouched.

Revision ID: c8e0a2b4d6f1
Revises: b8e3f5a7c9d1
Create Date: 2026-09-08 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e0a2b4d6f1"
down_revision: Union[str, None] = "b8e3f5a7c9d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STORE_TABLES = ("user_skills", "skill_installs")


def _store_tables_present(connection) -> bool:
    """Whether this database carries the skill library at all.

    Same escape hatch as d4f6a8b0c2e5, which scopes these very tables: a
    trimmed schema is only ever a local SQLite fixture, so skip there, and
    refuse on a real deployment rather than leave PostgreSQL half-migrated.
    """
    present = set(sa.inspect(connection).get_table_names())
    missing = [table for table in STORE_TABLES if table not in present]
    if not missing:
        return True
    if connection.dialect.name != "sqlite":
        raise RuntimeError(
            f"Required skill store table is missing: {', '.join(missing)}"
        )
    return False


def upgrade() -> None:
    connection = op.get_bind()
    if _store_tables_present(connection):
        # SQLite accepts ADD COLUMN NOT NULL as long as a constant default
        # comes with it, so these need no table rebuild on either backend.
        op.add_column(
            "user_skills",
            sa.Column(
                "listing", sa.String(16), nullable=False,
                server_default=sa.text("'listed'"),
            ),
        )
        op.add_column("user_skills", sa.Column("listing_note", sa.Text(), nullable=True))
        op.add_column(
            "user_skills", sa.Column("listing_changed_by", sa.String(64), nullable=True)
        )
        op.add_column(
            "user_skills",
            sa.Column("listing_changed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            "user_skills",
            sa.Column(
                "is_official", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.add_column(
            "user_skills",
            sa.Column(
                "featured", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
        )

        # The column default already lands every row on 'listed'.  Stating the
        # guarantee that actually matters — nothing already public disappears
        # the moment this migration runs — as its own statement keeps it true
        # even if someone later decides drafts should default to something else.
        op.execute(
            sa.text(
                "UPDATE user_skills SET listing = 'listed' WHERE status = 'published'"
            )
        )
        op.create_index(
            "ix_user_skills_listing_published",
            "user_skills",
            ["listing", "published_at"],
        )

        # Batch mode: SQLite cannot relax a NOT NULL column in place.
        with op.batch_alter_table("skill_installs") as batch:
            batch.alter_column(
                "user_skill_id", existing_type=sa.String(64), nullable=True
            )
            batch.add_column(
                sa.Column(
                    "kind", sa.String(8), nullable=False,
                    server_default=sa.text("'skill'"),
                )
            )
            # Added nullable so the batch rebuild has something to copy;
            # tightened below once the backfill has given every row a key.
            batch.add_column(sa.Column("catalog_id", sa.String(96), nullable=True))

        # Every pre-existing row is a community install — the column it came
        # from was NOT NULL — so this leaves no NULL for the ALTER below.
        # `||` is the concatenation operator on both PostgreSQL and SQLite.
        op.execute(
            sa.text(
                "UPDATE skill_installs "
                "SET catalog_id = 'community:' || user_skill_id, kind = 'skill'"
            )
        )
        with op.batch_alter_table("skill_installs") as batch:
            batch.alter_column(
                "catalog_id", existing_type=sa.String(96), nullable=False
            )
            # ``install_dir`` holds a skill's sandbox directory *and* an MCP
            # server's name, which are separate namespaces: without ``kind`` in
            # the key, installing the server "memory" would take over the
            # provenance row of a community skill called "memory".
            batch.drop_constraint("uq_skill_installs_user_dir", type_="unique")
            batch.create_unique_constraint(
                "uq_skill_installs_user_kind_dir",
                ["user_id", "kind", "install_dir"],
            )
        op.create_index(
            "ix_skill_installs_catalog_installed",
            "skill_installs",
            ["catalog_id", "installed_at"],
        )

    op.create_table(
        "catalog_overrides",
        sa.Column("catalog_id", sa.String(96), primary_key=True),
        sa.Column("listing", sa.String(16), nullable=False),
        sa.Column(
            "featured", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(64), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    connection = op.get_bind()
    op.drop_table("catalog_overrides")
    if not _store_tables_present(connection):
        return

    # Catalogue installs cannot exist under the old schema (see the module
    # docstring); drop them rather than fail the rollback on a NOT NULL that
    # only provenance rows violate.
    op.execute(sa.text("DELETE FROM skill_installs WHERE user_skill_id IS NULL"))
    op.drop_index("ix_skill_installs_catalog_installed", table_name="skill_installs")
    with op.batch_alter_table("skill_installs") as batch:
        # The narrower key can be restored safely only because the delete above
        # took every MCP row with it: what is left is community skill installs,
        # already unique on (user_id, install_dir) under the wide key.
        batch.drop_constraint("uq_skill_installs_user_kind_dir", type_="unique")
        batch.create_unique_constraint(
            "uq_skill_installs_user_dir", ["user_id", "install_dir"]
        )
        batch.drop_column("catalog_id")
        batch.drop_column("kind")
        batch.alter_column("user_skill_id", existing_type=sa.String(64), nullable=False)

    op.drop_index("ix_user_skills_listing_published", table_name="user_skills")
    for column in (
        "featured",
        "is_official",
        "listing_changed_at",
        "listing_changed_by",
        "listing_note",
        "listing",
    ):
        op.drop_column("user_skills", column)
