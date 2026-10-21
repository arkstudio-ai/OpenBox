"""Upgrade old rows safely and refuse destructive loss of new user data."""
import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


@pytest.mark.parametrize("populated", ["none", "decision", "description"])
def test_shared_context_upgrade_and_safe_downgrade(tmp_path, populated):
    migration = importlib.import_module("db.migrations.versions.ph62e3f4a5b6_assistant_shared_context")
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    with engine.begin() as db, Operations.context(MigrationContext.configure(db)):
        db.exec_driver_sql("CREATE TABLE question_checkpoints (id TEXT PRIMARY KEY)")
        db.exec_driver_sql("CREATE TABLE file_assets (id TEXT PRIMARY KEY, name TEXT)")
        db.exec_driver_sql("INSERT INTO question_checkpoints VALUES ('old-question')")
        db.exec_driver_sql("INSERT INTO file_assets VALUES ('old-file', 'IMG_001.png')")
        migration.upgrade()
        assert db.scalar(text("SELECT detached FROM question_checkpoints")) == 0
        assert db.scalar(text("SELECT description FROM file_assets")) == ""
        assert db.scalar(text("SELECT name FROM file_assets")) == "IMG_001.png"
        if populated == "none":
            migration.downgrade()
            assert "description" not in {c["name"] for c in inspect(db).get_columns("file_assets")}
            migration.upgrade()
        else:
            db.exec_driver_sql("UPDATE question_checkpoints SET detached=true" if populated == "decision"
                               else "UPDATE file_assets SET description='香蕉猫参考图'")
            with pytest.raises(RuntimeError):
                migration.downgrade()
            assert "description" in {c["name"] for c in inspect(db).get_columns("file_assets")}
    engine.dispose()
