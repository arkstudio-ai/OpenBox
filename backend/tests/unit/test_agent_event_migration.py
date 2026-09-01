"""Schema smoke for the canonical Agent event shadow log."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


AGENT_EVENT_REVISION = "a8c1e4f7b9d2"
PREVIOUS_REVISION = "fc4e6d8b0a2c"


def _config(database_path: Path, monkeypatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(backend_dir / "db" / "migrations"),
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    return config


def _previous_schema(database_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE sessions (id VARCHAR(64) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_REVISION,),
        )
        connection.exec_driver_sql("INSERT INTO users(id) VALUES ('user-1')")
        connection.exec_driver_sql("INSERT INTO sessions(id) VALUES ('session-1')")
    engine.dispose()


def test_agent_event_migration_has_required_indexes(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "agent-event.db"
    _previous_schema(database_path)
    config = _config(database_path, monkeypatch)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(AGENT_EVENT_REVISION).down_revision == PREVIOUS_REVISION
    command.upgrade(config, AGENT_EVENT_REVISION)

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("agent_events")}
    indexes = {index["name"] for index in inspector.get_indexes("agent_events")}
    uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("agent_events")
    }
    with engine.connect() as connection:
        version = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert {
        "id",
        "session_id",
        "user_id",
        "sequence",
        "event_key",
        "kind",
        "run_id",
        "generation",
        "turn_id",
        "step_id",
        "message_id",
        "part_id",
        "tool_call_id",
        "payload",
        "created_at",
    } == columns
    assert {
        "ix_agent_events_session_run",
        "ix_agent_events_session_message",
        "ix_agent_events_session_part",
        "ix_agent_events_user_created",
    } <= indexes
    assert {
        "uq_agent_events_session_sequence",
        "uq_agent_events_session_event_key",
    } <= uniques
    assert version == AGENT_EVENT_REVISION
    engine.dispose()

    command.downgrade(config, PREVIOUS_REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    assert "agent_events" not in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_agent_event_downgrade_refuses_live_append_only_history(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "agent-event-live.db"
    _previous_schema(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, AGENT_EVENT_REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO agent_events "
            "(id, session_id, user_id, sequence, event_key, kind, payload, created_at) "
            "VALUES ('event-1', 'session-1', 'user-1', 1, ?, 'surface.seed', ?, "
            "CURRENT_TIMESTAMP)",
            ("a" * 64, '{"version":1}'),
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="downgrade refused"):
        command.downgrade(config, PREVIOUS_REVISION)
