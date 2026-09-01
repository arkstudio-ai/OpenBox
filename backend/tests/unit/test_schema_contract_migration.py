"""Migration smoke for legacy nullability and unmanaged storage contracts."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


REVISION = "e2b4d6f8a0c3"
PREVIOUS_REVISION = "d0a2c4e6f8b1"


def _config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option(
        "script_location", str(backend_dir / "db" / "migrations")
    )
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}"
    )
    return config


def _at_previous_head(database_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE containers ("
            "id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE image_gen_cache ("
            "id VARCHAR(64) PRIMARY KEY, request_data JSON NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE user_memories ("
            "id VARCHAR(64) PRIMARY KEY, value JSON NULL, evidence JSON NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_REVISION,),
        )
        connection.exec_driver_sql(
            "INSERT INTO containers(id, project_id) VALUES ('container-1', 'project-1')"
        )
        connection.exec_driver_sql(
            "INSERT INTO image_gen_cache(id, request_data) VALUES ('cache-1', NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO user_memories(id, value, evidence) "
            "VALUES ('memory-1', NULL, NULL)"
        )
    engine.dispose()


def _nullable(inspector, table: str, column: str) -> bool:
    return bool(next(
        item["nullable"]
        for item in inspector.get_columns(table)
        if item["name"] == column
    ))


def test_schema_contract_upgrade_backfills_and_is_reversible(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "schema-contract.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)

    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    assert _nullable(inspector, "containers", "project_id") is True
    assert _nullable(inspector, "image_gen_cache", "request_data") is False
    assert _nullable(inspector, "user_memories", "value") is False
    assert _nullable(inspector, "user_memories", "evidence") is False
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT request_data FROM image_gen_cache WHERE id='cache-1'"
        ).scalar_one() == "{}"
        assert connection.exec_driver_sql(
            "SELECT value FROM user_memories WHERE id='memory-1'"
        ).scalar_one() == "{}"
        assert connection.exec_driver_sql(
            "SELECT evidence FROM user_memories WHERE id='memory-1'"
        ).scalar_one() == "{}"
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == REVISION
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    engine.dispose()

    command.downgrade(config, PREVIOUS_REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    assert _nullable(inspector, "containers", "project_id") is False
    assert _nullable(inspector, "image_gen_cache", "request_data") is True
    assert _nullable(inspector, "user_memories", "value") is True
    assert _nullable(inspector, "user_memories", "evidence") is True
    engine.dispose()


def test_schema_contract_downgrade_refuses_projectless_container(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "schema-contract-live.db"
    _at_previous_head(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO containers(id, project_id) "
            "VALUES ('container-projectless', NULL)"
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="projectless containers exist"):
        command.downgrade(config, PREVIOUS_REVISION)


def test_alembic_check_ignores_explicitly_managed_kv_store(
    tmp_path,
    monkeypatch,
):
    from db.base import Base
    import db.models  # noqa: F401

    database_path = tmp_path / "autogen-contract.db"
    engine = sa.create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE kv_store ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (REVISION,),
        )
    engine.dispose()

    command.check(_config(database_path, monkeypatch))
