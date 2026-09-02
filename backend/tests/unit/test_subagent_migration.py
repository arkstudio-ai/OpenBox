"""Migration/readiness smoke for the durable subagent three-table protocol."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

from tests.support.migrations import current_head


#: The tree's end, not a pinned id — see tests/support/migrations.
REVISION = current_head()
PREVIOUS_REVISION = "fd5f7a9c1e3b"


def _config(database_path: Path, monkeypatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "db" / "migrations"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    return config


def _at_previous_head(database_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        for ddl in (
            "CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)",
            "CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY)",
            "CREATE TABLE sessions (id VARCHAR(64) PRIMARY KEY)",
            "CREATE TABLE messages (id VARCHAR(64) PRIMARY KEY)",
            "CREATE TABLE parts (id VARCHAR(64) PRIMARY KEY)",
        ):
            connection.exec_driver_sql(ddl)
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_REVISION,),
        )
    engine.dispose()


def test_subagent_migration_is_single_head_and_reversible_when_empty(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "subagents.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    assert {
        "subagent_descriptors",
        "subagent_activations",
        "subagent_outbox",
    } <= set(inspector.get_table_names())
    assert "authority_snapshot" in {
        column["name"] for column in inspector.get_columns("subagent_descriptors")
    }
    activation_uniques = {
        item["name"]
        for item in inspector.get_unique_constraints("subagent_activations")
    }
    assert {
        "uq_subagent_activations_parent_part",
        "uq_subagent_activations_descriptor_generation",
        "uq_subagent_activations_trigger",
    } <= activation_uniques
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            == REVISION
        )
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    engine.dispose()

    command.downgrade(config, PREVIOUS_REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    assert not {
        "subagent_descriptors",
        "subagent_activations",
        "subagent_outbox",
    } & set(sa.inspect(engine).get_table_names())
    engine.dispose()


def test_subagent_downgrade_refuses_live_descriptors(tmp_path, monkeypatch):
    database_path = tmp_path / "subagents-live.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO subagent_descriptors "
            "(id,user_id,project_id,parent_session_id,child_session_id,"
            "root_session_id,depth,subagent_type,lifecycle,state,generation,"
            "created_at,updated_at) VALUES "
            "('descriptor-1','user-1','project-1','parent-1','child-1',"
            "'parent-1',1,'explore','continuable','active',1,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="descriptors still exist"):
        command.downgrade(config, PREVIOUS_REVISION)


def test_desktop_bridge_creates_all_protocol_tables_idempotently(tmp_path):
    from agent.schema import _upgrade_sqlite_subagent_schema

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'desktop.db'}")
    with engine.begin() as connection:
        # SQLite permits referenced tables to be absent while DDL is created;
        # desktop metadata startup creates their real definitions first.
        _upgrade_sqlite_subagent_schema(connection)
        _upgrade_sqlite_subagent_schema(connection)
    assert {
        "subagent_descriptors",
        "subagent_activations",
        "subagent_outbox",
    } <= set(sa.inspect(engine).get_table_names())
    engine.dispose()


def test_desktop_bridge_adds_fail_closed_authority_column_to_legacy_table(tmp_path):
    from agent.schema import _upgrade_sqlite_subagent_schema

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'desktop-legacy.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE subagent_descriptors (id VARCHAR(64) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO subagent_descriptors(id) VALUES ('legacy-child')"
        )
        _upgrade_sqlite_subagent_schema(connection)
        snapshot = connection.exec_driver_sql(
            "SELECT authority_snapshot FROM subagent_descriptors "
            "WHERE id='legacy-child'"
        ).scalar_one()
    assert snapshot == "{}"
    assert "authority_snapshot" in {
        column["name"]
        for column in sa.inspect(engine).get_columns("subagent_descriptors")
    }
    engine.dispose()
