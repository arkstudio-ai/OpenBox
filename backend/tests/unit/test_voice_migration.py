"""voice_calls gains a nullable summary; downgrading drops only that column (SQLite)."""
import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_call_summary_upgrade_keeps_calls_and_downgrade_drops_only_the_column(tmp_path):
    calls = importlib.import_module("db.migrations.versions.pbc2d3e4f5a6_voice_calls")
    summary = importlib.import_module("db.migrations.versions.pbd3e4f5a6b7_voice_call_summary")
    assert summary.down_revision == calls.revision
    engine = create_engine(f"sqlite:///{tmp_path / 'voice-migration.db'}")
    with engine.begin() as db, Operations.context(MigrationContext.configure(db)):
        for table in ("users", "workspaces", "sessions"):
            db.exec_driver_sql(f"CREATE TABLE {table} (id VARCHAR(64) PRIMARY KEY)")
        calls.upgrade()
        db.exec_driver_sql("INSERT INTO voice_calls (id, user_id, workspace_id, main_session_id, client, model, voice, "
                           "status, started_at, price_date) VALUES ('c1', 'u', 'w', 's', 'web', 'm', 'Serena', "
                           "'ended', '2026-10-07 12:00:00', '2026-10-07')")
        summary.upgrade()
        columns = {column["name"]: column for column in inspect(db).get_columns("voice_calls")}
        assert columns["summary"]["nullable"]
        assert db.scalar(text("SELECT summary FROM voice_calls WHERE id='c1'")) is None  # earlier calls have none
        db.exec_driver_sql("UPDATE voice_calls SET summary='聊了贪吃蛇' WHERE id='c1'")
        summary.downgrade()
        assert "summary" not in {column["name"] for column in inspect(db).get_columns("voice_calls")}
        assert db.scalar(text("SELECT status FROM voice_calls WHERE id='c1'")) == "ended"
        assert {index["name"] for index in inspect(db).get_indexes("voice_calls")} >= {
            "ix_voice_calls_owner", "ix_voice_calls_active"}
        with pytest.raises(IntegrityError):  # the rebuilt table keeps its status check
            with db.begin_nested():
                db.exec_driver_sql("UPDATE voice_calls SET status='bogus' WHERE id='c1'")
        summary.upgrade()  # and back again
        assert "summary" in {column["name"] for column in inspect(db).get_columns("voice_calls")}
    engine.dispose()
