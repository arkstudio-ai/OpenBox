"""Schema smoke for the durable Cron delivery outbox."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


REVISION = "f0b2d4e6a8c1"
PREVIOUS_REVISION = "a8c1e4f7b9d2"


def _config(database_path: Path, monkeypatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "db" / "migrations"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    return config


def _at_previous_head(database_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_REVISION,),
        )
    engine.dispose()


def test_outbox_is_single_head_with_claim_indexes(tmp_path, monkeypatch):
    database_path = tmp_path / "cron-outbox.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    columns = {
        column["name"] for column in inspector.get_columns("cron_delivery_outbox")
    }
    indexes = {index["name"] for index in inspector.get_indexes("cron_delivery_outbox")}
    uniques = {
        item["name"]
        for item in inspector.get_unique_constraints("cron_delivery_outbox")
    }
    with engine.connect() as connection:
        version = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert {
        "id",
        "run_id",
        "job_id",
        "user_id",
        "project_id",
        "session_id",
        "kind",
        "payload",
        "state",
        "attempts",
        "available_at",
        "claim_token",
        "claim_owner",
        "claim_expires_at",
        "delivered_at",
        "last_error",
        "created_at",
        "updated_at",
    } == columns
    assert {
        "ix_cron_delivery_claim",
        "ix_cron_delivery_run",
        "ix_cron_delivery_session",
    } <= indexes
    assert "uq_cron_delivery_run_kind" in uniques
    assert version == REVISION
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    engine.dispose()


def test_downgrade_refuses_pending_delivery(tmp_path, monkeypatch):
    database_path = tmp_path / "cron-outbox-pending.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO cron_delivery_outbox "
            "(id, run_id, job_id, user_id, kind, payload, state, attempts, "
            "available_at, created_at, updated_at) VALUES "
            "('delivery-1','run-1','job-1','user-1','event','{}','pending',0,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="pending deliveries"):
        command.downgrade(config, PREVIOUS_REVISION)


def test_desktop_bridge_creates_outbox_on_an_old_open_store(tmp_path):
    from cron.schema import _upgrade_sqlite_cron_lease_schema

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'desktop.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE cron_jobs (id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE cron_runs (id VARCHAR PRIMARY KEY, job_id VARCHAR)"
        )
        _upgrade_sqlite_cron_lease_schema(connection)

    inspector = sa.inspect(engine)
    assert "cron_delivery_outbox" in inspector.get_table_names()
    assert "run_generation" in {
        column["name"] for column in inspector.get_columns("cron_jobs")
    }
    engine.dispose()
