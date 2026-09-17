"""Schema smoke tests for the durable external-effect ledger."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


REVISION = "c6f9a1d3e5b7"
PREVIOUS_REVISION = "b5e8f1a4c7d0"


def _config(database_path: Path, monkeypatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "db" / "migrations"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    return config


def _previous_schema(database_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE sessions (id VARCHAR(64) PRIMARY KEY)")
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


def test_external_effect_migration_is_single_successor_with_required_indexes(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "effects.db"
    _previous_schema(database_path)
    config = _config(database_path, monkeypatch)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    command.upgrade(config, REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    assert {
        "id", "tenant_id", "project_id", "session_id", "run_id",
        "run_generation", "adapter", "provider", "operation", "idempotency_key",
        "request_hash", "safe_context", "state", "attempt_count",
        "reconcile_count", "claim_generation", "claim_kind", "claim_token",
        "claim_owner", "claim_expires_at", "provider_handle",
        "provider_receipt", "projection", "last_error", "reconcile_after",
        "prepared_at", "submitting_at", "accepted_at", "completed_at",
        "created_at", "updated_at",
    } == {column["name"] for column in inspector.get_columns("external_effects")}
    assert {
        "id", "effect_id", "sequence", "claim_generation", "phase",
        "evidence", "created_at",
    } == {
        column["name"]
        for column in inspector.get_columns("external_effect_evidence")
    }
    assert {
        "ix_external_effect_recovery",
        "ix_external_effect_run",
        "ix_external_effect_tenant_created",
    } <= {index["name"] for index in inspector.get_indexes("external_effects")}
    assert "ix_external_effect_evidence_effect" in {
        index["name"]
        for index in inspector.get_indexes("external_effect_evidence")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == REVISION
    engine.dispose()

    command.downgrade(config, PREVIOUS_REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    tables = set(sa.inspect(engine).get_table_names())
    assert "external_effects" not in tables
    assert "external_effect_evidence" not in tables
    engine.dispose()


def test_external_effect_downgrade_refuses_live_audit_history(tmp_path, monkeypatch):
    database_path = tmp_path / "effects-live.db"
    _previous_schema(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO external_effects "
            "(id, tenant_id, project_id, session_id, run_id, run_generation, "
            "adapter, provider, operation, idempotency_key, request_hash, safe_context, "
            "state, prepared_at, created_at, updated_at) VALUES "
            "('effect-1', 'user-1', 'project-1', 'session-1', 'run-1', 1, "
            "'test', 'test-provider', 'post', 'stable-key', ?, ?, 'prepared', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("a" * 64, '{"text":"路径/你好😀"}'),
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="downgrade refused"):
        command.downgrade(config, PREVIOUS_REVISION)
