"""Application database bootstrap is independent of retired job workers."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text

from db import base as db_base


_EXPOSURE_PART_COLUMNS = {
    "stream_seq",
    "canonical_tool_id",
    "wire_tool_name",
    "provider_binding_digest",
    "provider_dialect",
}

_SKILL_LIFECYCLE_COLUMNS = {
    "lifecycle_state",
    "lifecycle_generation",
}


def _create_pre_c7_desktop_store(database_path) -> None:
    """Make a current-shaped store with additive bridge columns removed."""
    import db.models  # noqa: F401

    engine = create_engine(f"sqlite:///{database_path}")
    db_base.Base.metadata.create_all(engine)
    with engine.begin() as connection:
        db_base._seed_single_user_scope(connection)
        connection.exec_driver_sql(
            "INSERT INTO sessions "
            "(id, user_id, project_id, created_at, updated_at) "
            "VALUES ('legacy-session', 'default', 'default', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO messages "
            "(id, session_id, user_id, role, created_at) "
            "VALUES ('legacy-message', 'legacy-session', 'default', "
            "'user', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO parts "
            "(id, message_id, session_id, user_id, type, data, created_at) "
            "VALUES ('legacy-part', 'legacy-message', 'legacy-session', "
            "'default', 'text', '{}', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO user_skills "
            "(id, owner_id, name, install_dir, description, icon, status, version, "
            "archive_data, archive_sha256, archive_size, metadata_data, created_at, "
            "updated_at) VALUES ("
            "'legacy-skill', 'default', 'Legacy Skill', 'legacy-skill', '', '', "
            "'unpublished', 1, X'6c6567616379', "
            "'c49fea7425fa7f8699897a97c159c6690267d9003bb78c53fb0220f3df5d34a0', "
            "6, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

        connection.exec_driver_sql("DROP TABLE internal_parts")
        connection.exec_driver_sql("DROP INDEX ix_parts_message_stream")
        connection.exec_driver_sql("DROP INDEX ix_parts_canonical_tool")
        connection.exec_driver_sql(
            "ALTER TABLE sessions DROP COLUMN tool_exposure_state"
        )
        for column in _EXPOSURE_PART_COLUMNS:
            connection.exec_driver_sql(f'ALTER TABLE parts DROP COLUMN "{column}"')
        connection.exec_driver_sql("DROP INDEX ix_user_skills_owner_lifecycle")
        for column in _SKILL_LIFECYCLE_COLUMNS:
            connection.exec_driver_sql(
                f'ALTER TABLE user_skills DROP COLUMN "{column}"'
            )
    engine.dispose()


def test_desktop_bridge_adds_durable_inbox_delivery_state_idempotently():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE agent_inbox_items (id VARCHAR PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO agent_inbox_items (id) VALUES ('legacy-inbox')"
        )
        db_base._ensure_single_user_legacy_tables(connection)
        db_base._ensure_single_user_legacy_tables(connection)
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("agent_inbox_items")
        }
        row = connection.exec_driver_sql(
            "SELECT delivery_attempts, delivery_last_error "
            "FROM agent_inbox_items WHERE id = 'legacy-inbox'"
        ).one()
    engine.dispose()

    assert columns["delivery_attempts"]["nullable"] is False
    assert columns["delivery_attempts"]["default"] == "0"
    assert row == (0, None)


@pytest.mark.asyncio
async def test_single_user_cold_start_creates_only_live_orm_tables(
    tmp_path, monkeypatch
):
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
        assert {
            "users",
            "projects",
            "sessions",
            "kv_store",
            "agent_inbox_items",
            "external_effects",
            "external_effect_evidence",
        } <= tables
        assert (
            not {
                "skill_jobs",
                "skill_job_attempts",
                "skill_job_events",
                "skill_job_inputs",
                "skill_job_artifacts",
                "session_inbox",
                "user_skill_settings",
            }
            & tables
        )
        assert (tmp_path / ".openbox" / "skill_jobs.db").is_file()
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


@pytest.mark.asyncio
async def test_existing_pre_c7_desktop_store_is_upgraded_idempotently(
    tmp_path,
    monkeypatch,
):
    await db_base.close_engine()
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / ".openbox" / "skill_jobs.db"
    database_path.parent.mkdir()
    _create_pre_c7_desktop_store(database_path)
    config = SimpleNamespace(
        jwt_secret="",
        database_url="unused",
        db_pool_size=1,
        db_pool_overflow=0,
    )

    try:
        engine = await db_base.ensure_engine(config)
        async with engine.begin() as connection:
            # The bridge is safe to repeat after the startup pass.
            await connection.run_sync(db_base._ensure_single_user_legacy_tables)
            await connection.run_sync(db_base._ensure_single_user_legacy_tables)
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "sessions": {
                        column["name"]: column
                        for column in inspect(sync_connection).get_columns("sessions")
                    },
                    "parts": {
                        column["name"]: column
                        for column in inspect(sync_connection).get_columns("parts")
                    },
                    "part_indexes": {
                        index["name"]
                        for index in inspect(sync_connection).get_indexes("parts")
                    },
                    "user_skills": {
                        column["name"]: column
                        for column in inspect(sync_connection).get_columns(
                            "user_skills"
                        )
                    },
                    "user_skill_indexes": {
                        index["name"]
                        for index in inspect(sync_connection).get_indexes("user_skills")
                    },
                }
            )
            exposure_state = (
                await connection.execute(
                    text(
                        "SELECT tool_exposure_state FROM sessions "
                        "WHERE id = 'legacy-session'"
                    )
                )
            ).scalar_one()
            legacy_part = (
                await connection.execute(
                    text("SELECT canonical_tool_id FROM parts WHERE id = 'legacy-part'")
                )
            ).scalar_one()
            legacy_skill = (
                await connection.execute(
                    text(
                        "SELECT lifecycle_state, lifecycle_generation "
                        "FROM user_skills WHERE id = 'legacy-skill'"
                    )
                )
            ).one()

        assert {
            "kv_store",
            "internal_parts",
            "external_effects",
            "external_effect_evidence",
        } <= schema["tables"]
        state_column = schema["sessions"]["tool_exposure_state"]
        assert str(state_column["type"]) == "TEXT"
        assert state_column["nullable"] is False
        assert state_column["default"] == "'{}'"
        assert exposure_state == "{}"
        assert _EXPOSURE_PART_COLUMNS <= schema["parts"].keys()
        assert str(schema["parts"]["stream_seq"]["type"]) == "INTEGER"
        assert str(schema["parts"]["canonical_tool_id"]["type"]) == "VARCHAR(128)"
        assert str(schema["parts"]["provider_binding_digest"]["type"]) == "VARCHAR(64)"
        assert legacy_part is None
        assert _SKILL_LIFECYCLE_COLUMNS <= schema["user_skills"].keys()
        assert str(schema["user_skills"]["lifecycle_state"]["type"]) == "VARCHAR(16)"
        assert schema["user_skills"]["lifecycle_state"]["nullable"] is False
        assert schema["user_skills"]["lifecycle_state"]["default"] == "'active'"
        assert str(schema["user_skills"]["lifecycle_generation"]["type"]) == "INTEGER"
        assert schema["user_skills"]["lifecycle_generation"]["nullable"] is False
        assert schema["user_skills"]["lifecycle_generation"]["default"] == "1"
        assert legacy_skill == ("active", 1)
        assert "ix_user_skills_owner_lifecycle" in schema["user_skill_indexes"]
        assert {
            "ix_parts_message_stream",
            "ix_parts_canonical_tool",
        } <= schema["part_indexes"]
        assert await db_base.database_schema_ready() is True
    finally:
        await db_base.close_engine()


def test_single_user_legacy_bridge_is_a_postgresql_noop():
    class PostgreSQLConnection:
        dialect = SimpleNamespace(name="postgresql")

        def exec_driver_sql(self, _statement):
            raise AssertionError("desktop bridge must not execute on PostgreSQL")

    db_base._ensure_single_user_legacy_tables(PostgreSQLConnection())
