"""SQLite smoke test for the Cron lease migration."""
from pathlib import Path

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from cron.schema import _upgrade_sqlite_cron_lease_schema


REVISION = "e9f1a3c5d7b9"
PREVIOUS_REVISION = "d8e0f2a4b6c8"
_JOB_COLUMNS_FOR_TEST = {
    "run_generation", "run_token", "run_owner", "lease_expires_at", "heartbeat_at",
}
_RUN_COLUMNS_FOR_TEST = {"claim_token", "claim_generation", "claim_owner"}


def _old_cron_tables(connection) -> None:
    connection.exec_driver_sql(
        "CREATE TABLE cron_jobs (id VARCHAR(64) PRIMARY KEY)"
    )
    connection.exec_driver_sql(
        "CREATE TABLE cron_runs (id VARCHAR(64) PRIMARY KEY, job_id VARCHAR(64))"
    )


def _config(database_path: Path, monkeypatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(backend_dir / "db" / "migrations"),
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    return config


def test_cron_lease_migration_up_and_down_preserves_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "cron-lease.db"
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        _old_cron_tables(connection)
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_REVISION,),
        )
        connection.exec_driver_sql("INSERT INTO cron_jobs(id) VALUES ('job-1')")
        connection.exec_driver_sql(
            "INSERT INTO cron_runs(id, job_id) VALUES ('run-1', 'job-1')"
        )
    engine.dispose()

    config = _config(database_path, monkeypatch)
    command.upgrade(config, REVISION)

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    job_columns = {column["name"] for column in inspector.get_columns("cron_jobs")}
    run_columns = {column["name"] for column in inspector.get_columns("cron_runs")}
    assert _JOB_COLUMNS_FOR_TEST <= job_columns
    assert _RUN_COLUMNS_FOR_TEST <= run_columns
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT run_generation FROM cron_jobs WHERE id='job-1'"
        ).scalar_one() == 0
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == REVISION
    engine.dispose()

    command.downgrade(config, PREVIOUS_REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    assert "run_generation" not in {
        column["name"] for column in inspector.get_columns("cron_jobs")
    }
    assert "claim_generation" not in {
        column["name"] for column in inspector.get_columns("cron_runs")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id FROM cron_jobs WHERE id='job-1'"
        ).scalar_one() == "job-1"
        assert connection.exec_driver_sql(
            "SELECT id FROM cron_runs WHERE id='run-1'"
        ).scalar_one() == "run-1"
    engine.dispose()


def test_desktop_bridge_upgrades_an_existing_store_idempotently(tmp_path):
    database_path = tmp_path / "desktop-old.db"
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        _old_cron_tables(connection)
        connection.exec_driver_sql("INSERT INTO cron_jobs(id) VALUES ('job-1')")
        connection.exec_driver_sql(
            "INSERT INTO cron_runs(id, job_id) VALUES ('run-1', 'job-1')"
        )
        _upgrade_sqlite_cron_lease_schema(connection)
        _upgrade_sqlite_cron_lease_schema(connection)

    inspector = sa.inspect(engine)
    assert _JOB_COLUMNS_FOR_TEST <= {
        column["name"] for column in inspector.get_columns("cron_jobs")
    }
    assert _RUN_COLUMNS_FOR_TEST <= {
        column["name"] for column in inspector.get_columns("cron_runs")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT run_generation FROM cron_jobs WHERE id='job-1'"
        ).scalar_one() == 0
    engine.dispose()
