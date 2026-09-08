"""Application database bootstrap is independent of retired job workers."""

from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text

from db import base as db_base


@pytest.mark.asyncio
async def test_single_user_cold_start_creates_only_live_orm_tables(tmp_path, monkeypatch):
    await db_base.close_engine()
    monkeypatch.chdir(tmp_path)
    config = SimpleNamespace(
        jwt_secret="",
        database_url="unused",
        db_pool_size=1,
        db_pool_overflow=0,
    )

    try:
        engine = await db_base.ensure_engine(config)
        async with engine.begin() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            user_id = (
                await connection.execute(
                    text("SELECT id FROM users WHERE id = 'default'")
                )
            ).scalar_one()
            project_id = (
                await connection.execute(
                    text("SELECT id FROM projects WHERE id = 'default'")
                )
            ).scalar_one()

        assert user_id == "default"
        assert project_id == "default"
        assert {"users", "projects", "sessions"} <= tables
        assert not {
            "skill_jobs",
            "skill_job_attempts",
            "skill_job_events",
            "skill_job_inputs",
            "skill_job_artifacts",
            "session_inbox",
            "user_skill_settings",
        } & tables
        assert (tmp_path / ".openbox" / "skill_jobs.db").is_file()
    finally:
        await db_base.close_engine()


#: ``user_skills`` and ``skill_installs`` exactly as they shipped before the
#: moderated store.  Every desktop database that has ever opened the skill
#: library already looks like this, and ``create_all`` will not touch a table
#: that exists — so this is the shape the retrofit has to close the gap from.
_PRE_STORE_SCHEMA = (
    """
    CREATE TABLE user_skills (
        id VARCHAR(64) NOT NULL PRIMARY KEY,
        owner_id VARCHAR(64) NOT NULL,
        workspace_id VARCHAR(64) NOT NULL,
        name VARCHAR(64) NOT NULL,
        install_dir VARCHAR(64) NOT NULL,
        description TEXT NOT NULL,
        icon VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'unpublished',
        version INTEGER NOT NULL DEFAULT 1,
        archive_data BLOB NOT NULL,
        archive_sha256 VARCHAR(64) NOT NULL,
        archive_size BIGINT NOT NULL,
        metadata_data TEXT,
        published_name VARCHAR(64),
        published_install_dir VARCHAR(64),
        published_description TEXT,
        published_icon VARCHAR(16),
        published_version INTEGER,
        published_archive_data BLOB,
        published_archive_sha256 VARCHAR(64),
        published_archive_size BIGINT,
        published_metadata_data TEXT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        published_at DATETIME,
        CONSTRAINT uq_user_skills_workspace_owner_name
            UNIQUE (workspace_id, owner_id, name)
    )
    """,
    """
    CREATE TABLE skill_installs (
        id VARCHAR(64) NOT NULL PRIMARY KEY,
        user_id VARCHAR(64) NOT NULL,
        user_skill_id VARCHAR(64) NOT NULL,
        name VARCHAR(64) NOT NULL,
        install_dir VARCHAR(64) NOT NULL,
        installed_at DATETIME NOT NULL,
        CONSTRAINT uq_skill_installs_user_dir UNIQUE (user_id, install_dir)
    )
    """,
    "CREATE INDEX ix_skill_installs_user ON skill_installs (user_id, installed_at)",
    """
    INSERT INTO skill_installs
        (id, user_id, user_skill_id, name, install_dir, installed_at)
    VALUES ('si-old', 'default', 'sk-old', 'notes', 'notes', CURRENT_TIMESTAMP)
    """,
)


def _seed_pre_store_desktop_db(path):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        for statement in _PRE_STORE_SCHEMA:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_desktop_upgrade_retrofits_the_store_columns_create_all_cannot_add(
    tmp_path, monkeypatch,
):
    """Desktop mode never runs Alembic, and create_all only creates.

    A desktop user who has used the skill library before this release already
    has both tables in their pre-store shape.  Without the retrofit the store's
    very first query — installs grouped by ``catalog_id`` — fails with "no such
    column" and takes browsing, publishing and installing down with it.
    """
    await db_base.close_engine()
    monkeypatch.chdir(tmp_path)
    _seed_pre_store_desktop_db(tmp_path / ".openbox" / "skill_jobs.db")
    config = SimpleNamespace(
        jwt_secret="", database_url="unused", db_pool_size=1, db_pool_overflow=0,
    )

    try:
        engine = await db_base.ensure_engine(config)
        async with engine.begin() as connection:
            skill_columns = await connection.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns("user_skills")
                }
            )
            install_columns = await connection.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns("skill_installs")
                }
            )
            uniques = await connection.run_sync(
                lambda sync: {
                    constraint["name"] for constraint
                    in sync.dialect.get_unique_constraints(sync, "skill_installs")
                }
            )
            # The query that used to raise OperationalError.
            counted = (
                await connection.execute(
                    text(
                        "SELECT catalog_id, count(*) FROM skill_installs "
                        "GROUP BY catalog_id"
                    )
                )
            ).all()
            listings = (
                await connection.execute(text("SELECT count(*) FROM user_skills WHERE listing = 'listed'"))
            ).scalar_one()

        assert {
            "listing", "listing_note", "listing_changed_by", "listing_changed_at",
            "is_official", "featured",
        } <= skill_columns
        assert {"kind", "catalog_id"} <= install_columns
        # Same backfill the migration writes: a row that predates the store can
        # only be a community install.
        assert counted == [("community:sk-old", 1)]
        assert listings == 0  # no rows seeded, but the column resolves
        assert "uq_skill_installs_user_kind_dir" in uniques
    finally:
        await db_base.close_engine()


@pytest.mark.asyncio
async def test_desktop_upgrade_leaves_an_already_migrated_database_alone(
    tmp_path, monkeypatch,
):
    """Bootstrap runs on every launch, so the retrofit has to be idempotent."""
    await db_base.close_engine()
    monkeypatch.chdir(tmp_path)
    _seed_pre_store_desktop_db(tmp_path / ".openbox" / "skill_jobs.db")
    config = SimpleNamespace(
        jwt_secret="", database_url="unused", db_pool_size=1, db_pool_overflow=0,
    )

    try:
        await db_base.ensure_engine(config)
        await db_base.close_engine()
        engine = await db_base.ensure_engine(config)
        async with engine.begin() as connection:
            rows = (
                await connection.execute(
                    text("SELECT id, kind, catalog_id FROM skill_installs")
                )
            ).all()
        assert rows == [("si-old", "skill", "community:sk-old")]
    finally:
        await db_base.close_engine()


@pytest.mark.asyncio
async def test_existing_multi_user_engine_is_reused():
    existing = db_base.get_engine()
    config = SimpleNamespace(
        jwt_secret="configured",
        database_url="postgresql+asyncpg://must-not-connect",
        db_pool_size=99,
        db_pool_overflow=99,
    )

    assert await db_base.ensure_engine(config) is existing
