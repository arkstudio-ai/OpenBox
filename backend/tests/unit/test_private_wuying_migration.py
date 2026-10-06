"""The Wuying extension preserves all retained legacy private identities."""
import importlib
import json
from datetime import datetime, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, MetaData, Table, text


def test_wuying_migration_keeps_legacy_rows_and_refuses_destructive_downgrade(tmp_path):
    old = importlib.import_module("db.migrations.versions.pb1c2d3e4f5a_private_runtime")
    browser = importlib.import_module("db.migrations.versions.pb2d3e4f5a6b_browser_resource")
    current = importlib.import_module("db.migrations.versions.pb3e4f5a6b7c_wuying_private_actor")
    engine = create_engine(f"sqlite:///{tmp_path / 'wuying-migration.db'}")
    with engine.begin() as db, Operations.context(MigrationContext.configure(db)):
        for table in ("users", "workspaces", "sessions", "resource_control_leases"):
            db.exec_driver_sql(f"CREATE TABLE {table} (id VARCHAR(64) PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO users VALUES ('actor')")
        db.exec_driver_sql("INSERT INTO workspaces VALUES ('workspace')")
        old.upgrade()
        browser.upgrade()
        historical = Table("private_runtimes", MetaData(), autoload_with=db)
        stamp = datetime.now(timezone.utc)
        retained = dict(id="retained", workspace_id="workspace", actor_user_id="actor", kind="sandbox",
            isolation_mode="process_uid", provider="private_docker_v1", status="ready", attempt_id="old-attempt",
            revision=2, provision_phase="ready", container_name="retained-name", container_id="retained-id",
            workspace_volume="retained-workspace", data_volume="retained-data", volume_identities="{}",
            image="retained-image", route_key="private:retained", api_key_ciphertext="retained-ciphertext",
            api_key_hash="f" * 64, created_at=stamp, updated_at=stamp)
        db.execute(historical.insert().values(**retained))
        before = dict(db.execute(text("SELECT * FROM private_runtimes WHERE id='retained'")).mappings().one())
        current.upgrade()
        assert "provider_identity" in {item["name"] for item in inspect(db).get_columns("private_runtimes")}
        # The private browser was removed from the code, not from the schema:
        # its retained tables keep their history and are never dropped.
        assert "provider" in {item["name"] for item in inspect(db).get_columns("browser_resource_bindings")}
        after = dict(db.execute(text("SELECT * FROM private_runtimes WHERE id='retained'")).mappings().one())
        assert {key: after[key] for key in before} == before
        current.downgrade()
        assert dict(db.execute(text("SELECT * FROM private_runtimes WHERE id='retained'")).mappings().one()) == before
        current.upgrade()
        table = Table("private_runtimes", MetaData(), autoload_with=db)
        db.execute(table.insert().values(**{**retained, "id": "wuying", "provider": "private_wuying_v1",
            "isolation_mode": "guest_uid_mount", "container_name": "wuying-actor", "container_id": "wpr_fixture",
            "workspace_volume": None, "data_volume": None, "route_key": "private:wuying",
            "api_key_ciphertext": "", "provider_identity": json.dumps({"guest_binding": {"id": "guest-fixture"}})}))
        assert db.scalar(text("SELECT count(*) FROM private_runtimes")) == 2
        with pytest.raises(RuntimeError, match="Wuying private actor"):
            current.downgrade()
        assert db.scalar(text("SELECT count(*) FROM private_runtimes")) == 2
    engine.dispose()
