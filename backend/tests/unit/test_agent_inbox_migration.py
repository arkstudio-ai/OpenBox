"""Schema smoke for the durable main-Agent inbox protocol."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


REVISION = "e2b4d6f8a0c3"
INBOX_REVISION = "b5e8f1a4c7d0"
PREVIOUS_REVISION = "a4d7f0c2e9b1"


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
        ):
            connection.exec_driver_sql(ddl)
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_REVISION,),
        )
        connection.exec_driver_sql("INSERT INTO users(id) VALUES ('user-1')")
        connection.exec_driver_sql("INSERT INTO projects(id) VALUES ('project-1')")
        connection.exec_driver_sql("INSERT INTO sessions(id) VALUES ('session-1')")
    engine.dispose()


def test_inbox_migration_is_single_head_and_reversible_when_empty(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "agent-inbox.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("agent_inbox_items")}
    indexes = {index["name"] for index in inspector.get_indexes("agent_inbox_items")}
    uniques = {
        item["name"] for item in inspector.get_unique_constraints("agent_inbox_items")
    }
    assert {
        "id",
        "user_id",
        "project_id",
        "session_id",
        "client_id",
        "request_digest",
        "delivery",
        "target",
        "prompt",
        "attachments",
        "state",
        "message_id",
        "result_message_id",
        "run_id",
        "generation",
        "turn_id",
        "step_id",
        "claim_token",
        "claim_owner",
        "claim_expires_at",
        "accepted_at",
        "claimed_at",
        "canceled_at",
        "settled_at",
        "delivery_attempts",
        "delivery_last_error",
        "created_at",
        "updated_at",
    } <= columns
    assert {
        "ix_agent_inbox_session_queue",
        "ix_agent_inbox_claim_recovery",
        "ix_agent_inbox_run",
        "ix_agent_inbox_user_created",
    } <= indexes
    assert {"uq_agent_inbox_client_id", "uq_agent_inbox_message"} <= uniques
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
    assert "agent_inbox_items" not in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_inbox_migration_refuses_to_drop_durable_input(tmp_path, monkeypatch):
    database_path = tmp_path / "agent-inbox-live.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO agent_inbox_items ("
            "id, user_id, project_id, session_id, request_digest, delivery, "
            "target, prompt, attachments, state, accepted_at, created_at, updated_at"
            ") VALUES ("
            "'inbox-1', 'user-1', 'project-1', 'session-1', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'followup', 'next-turn', 'keep me', '[]', 'accepted', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="durable input still exists"):
        command.downgrade(config, PREVIOUS_REVISION)


def test_delivery_attempt_migration_refuses_to_drop_durable_retry_state(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "agent-inbox-delivery-state.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO agent_inbox_items ("
            "id, user_id, project_id, session_id, request_digest, delivery, "
            "target, prompt, attachments, state, delivery_attempts, "
            "delivery_last_error, accepted_at, created_at, updated_at"
            ") VALUES ("
            "'inbox-1', 'user-1', 'project-1', 'session-1', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'followup', 'next-turn', 'keep retry evidence', '[]', 'accepted', "
            '1, \'{"code":"delivery_failed","retryable":true}\', '
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="durable delivery state exists"):
        command.downgrade(config, INBOX_REVISION)
