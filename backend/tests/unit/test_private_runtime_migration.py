import importlib
from datetime import datetime, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text, Table, MetaData
from sqlalchemy.exc import IntegrityError

from core.config import OpenBoxConfig, _apply_env_overrides


def test_private_runtime_migration_is_additive_and_retains_populated_authority(tmp_path):
    migration = importlib.import_module("db.migrations.versions.pb1c2d3e4f5a_private_runtime")
    engine = create_engine(f"sqlite:///{tmp_path / 'private-migration.db'}")
    with engine.begin() as db, Operations.context(MigrationContext.configure(db)):
        db.exec_driver_sql("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)")
        db.exec_driver_sql("CREATE TABLE workspaces (id VARCHAR(64) PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO users VALUES ('actor')")
        db.exec_driver_sql("INSERT INTO workspaces VALUES ('workspace')")
        migration.upgrade()
        assert "private_runtimes" in inspect(db).get_table_names()
        migration.downgrade()
        migration.upgrade()
        fields = dict(id="binding", workspace_id="workspace", actor_user_id="actor", kind="sandbox", isolation_mode="process_uid",
            provider="private_docker_v1", status="reserved", attempt_id="attempt", revision=1,
            provision_phase="reserved", container_name="openbox-private-one", workspace_volume="private-workspace",
            data_volume="private-data", volume_identities="{}", image="local-test", route_key="private:binding",
            api_key_ciphertext="retained-ciphertext", api_key_hash="f" * 64,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        historical_table = Table("private_runtimes", MetaData(), autoload_with=db)
        db.execute(historical_table.insert().values(**fields))
        with pytest.raises(IntegrityError):
            db.execute(historical_table.insert().values(**{**fields, "id": "other",
                "container_name": "different", "workspace_volume": "different-workspace", "data_volume": "different-data",
                "route_key": "private:other"}))
        with pytest.raises(RuntimeError, match="must be retained"):
            migration.downgrade()
        assert db.scalar(text("SELECT count(*) FROM private_runtimes")) == 1
        assert db.scalar(text("SELECT count(*) FROM users")) == 1
    engine.dispose()


def test_retired_private_runtime_settings_are_ignored(monkeypatch):
    # Deployments may still carry the removed section or its variables.
    monkeypatch.setenv("OPENBOX_PRIVATE_RUNTIME_ENABLED", "true")
    assert "private_runtime" not in _apply_env_overrides({})
    config = OpenBoxConfig.model_validate({"private_runtime": {"enabled": True}})
    assert not hasattr(config, "private_runtime")
