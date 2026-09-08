"""The skill-store moderation migration round-trips and keeps the store intact."""
import importlib
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


migration = importlib.import_module(
    "db.migrations.versions.c8e0a2b4d6f1_admin_console_skill_store"
)


def _run(connection, direction):
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, direction)()


def _schema():
    """The pre-migration shape of the two tables the migration rewrites."""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    skills = sa.Table(
        "user_skills", metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("install_dir", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archive_data", sa.LargeBinary(), nullable=False),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("archive_size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "owner_id", "name",
            name="uq_user_skills_workspace_owner_name",
        ),
    )
    sa.Index("ix_user_skills_status_published", skills.c.status, skills.c.published_at)
    installs = sa.Table(
        "skill_installs", metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("user_skill_id", sa.String(64), sa.ForeignKey("user_skills.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("install_dir", sa.String(64), nullable=False),
        sa.Column("installed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "install_dir", name="uq_skill_installs_user_dir"),
    )
    sa.Index("ix_skill_installs_user", installs.c.user_id, installs.c.installed_at)
    metadata.create_all(engine)
    return engine, skills, installs


def _seed(connection, skills, installs):
    now = datetime.now(timezone.utc)
    connection.execute(skills.insert(), [
        {
            "id": "sk-public", "owner_id": "u-1", "workspace_id": "ws-1",
            "name": "web-research", "install_dir": "web-research",
            "description": "", "icon": "", "status": "published", "version": 2,
            "archive_data": b"PK\x03\x04", "archive_sha256": "a" * 64,
            "archive_size": 4, "created_at": now, "updated_at": now,
            "published_at": now,
        },
        {
            "id": "sk-draft", "owner_id": "u-1", "workspace_id": "ws-1",
            "name": "private-notes", "install_dir": "private-notes",
            "description": "", "icon": "", "status": "unpublished", "version": 1,
            "archive_data": b"PK\x03\x04", "archive_sha256": "b" * 64,
            "archive_size": 4, "created_at": now, "updated_at": now,
            "published_at": None,
        },
    ])
    connection.execute(installs.insert(), {
        "id": "si-1", "user_id": "u-2", "user_skill_id": "sk-public",
        "name": "web-research", "install_dir": "web-research", "installed_at": now,
    })


def _indexes(connection, table):
    return {index["name"] for index in sa.inspect(connection).get_indexes(table)}


def test_admin_console_migration_upgrade_and_downgrade():
    engine, skills, installs = _schema()
    with engine.begin() as connection:
        _seed(connection, skills, installs)
        _run(connection, "upgrade")

        assert "catalog_overrides" in sa.inspect(connection).get_table_names()

        # Nothing that was already public may fall off the shelf.
        listings = dict(connection.execute(
            sa.text("SELECT id, listing FROM user_skills")
        ).all())
        assert listings["sk-public"] == "listed"
        assert listings["sk-draft"] == "listed"
        flags = connection.execute(sa.text(
            "SELECT is_official, featured FROM user_skills WHERE id = 'sk-public'"
        )).one()
        assert flags == (0, 0)
        assert "ix_user_skills_listing_published" in _indexes(connection, "user_skills")

        install = connection.execute(sa.text(
            "SELECT catalog_id, kind FROM skill_installs WHERE id = 'si-1'"
        )).one()
        assert install == ("community:sk-public", "skill")
        user_skill_id = next(
            column for column in sa.inspect(connection).get_columns("skill_installs")
            if column["name"] == "user_skill_id"
        )
        assert user_skill_id["nullable"] is True
        install_indexes = _indexes(connection, "skill_installs")
        assert "ix_skill_installs_catalog_installed" in install_indexes
        assert "ix_skill_installs_user" in install_indexes

        # A catalogue entry install: no user_skills row behind it.
        connection.execute(sa.text(
            "INSERT INTO skill_installs "
            "(id, user_id, user_skill_id, kind, catalog_id, name, install_dir, installed_at) "
            "VALUES ('si-2', 'u-2', NULL, 'mcp', 'mcp:playwright', 'playwright', "
            "'playwright', CURRENT_TIMESTAMP)"
        ))

        _run(connection, "downgrade")
        columns = {
            column["name"] for column in
            sa.inspect(connection).get_columns("user_skills")
        }
        assert {"listing", "listing_note", "is_official", "featured"} & columns == set()
        assert "catalog_overrides" not in sa.inspect(connection).get_table_names()
        # The community row survives; the catalogue row has no old-schema shape.
        remaining = connection.execute(
            sa.text("SELECT id FROM skill_installs")
        ).scalars().all()
        assert remaining == ["si-1"]


def test_migration_widens_the_install_guard_to_cover_the_kind():
    """``install_dir`` holds two namespaces, so it is only unique within one.

    A skill's sandbox directory and an MCP server's name are written to the
    same column, and they collide: the catalogue ships a server called
    "memory" and nothing stops a community skill from being called that too.
    Under the old ``(user_id, install_dir)`` key the second install would take
    over the first one's provenance row.
    """
    engine, skills, installs = _schema()
    with engine.begin() as connection:
        _seed(connection, skills, installs)
        _run(connection, "upgrade")
        assert {
            constraint["name"] for constraint
            in sa.inspect(connection).get_unique_constraints("skill_installs")
        } == {"uq_skill_installs_user_kind_dir"}

        # Same user, same directory, different kind: both rows must survive.
        connection.execute(sa.text(
            "INSERT INTO skill_installs "
            "(id, user_id, user_skill_id, kind, catalog_id, name, install_dir, installed_at) "
            "VALUES ('si-mcp', 'u-2', NULL, 'mcp', 'mcp:web-research', 'web-research', "
            "'web-research', CURRENT_TIMESTAMP)"
        ))
        assert connection.execute(sa.text(
            "SELECT count(*) FROM skill_installs WHERE install_dir = 'web-research'"
        )).scalar_one() == 2

        # …and the guard still bites within one kind.
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text(
                "INSERT INTO skill_installs "
                "(id, user_id, user_skill_id, kind, catalog_id, name, install_dir, installed_at) "
                "VALUES ('si-dup', 'u-2', NULL, 'skill', 'skill:web-research', "
                "'web-research', 'web-research', CURRENT_TIMESTAMP)"
            ))


def test_trimmed_sqlite_fixture_without_the_skill_library_still_migrates():
    """Partial SQLite schemas (test fixtures) skip the store tables."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run(connection, "upgrade")
        assert "catalog_overrides" in sa.inspect(connection).get_table_names()
        _run(connection, "downgrade")
        assert "catalog_overrides" not in sa.inspect(connection).get_table_names()


def test_a_real_backend_missing_the_skill_library_refuses_instead_of_skipping():
    """The skip is a SQLite convenience; PostgreSQL must never half-migrate."""
    engine = sa.create_engine("sqlite://")
    engine.dialect.name = "postgresql"
    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="user_skills"):
            _run(connection, "upgrade")
