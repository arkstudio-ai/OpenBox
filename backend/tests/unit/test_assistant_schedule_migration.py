"""Both deployment paths preserve old schedules and retain private receipts."""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from db.base import _upgrade_desktop_assistant_columns


@pytest.mark.parametrize("desktop", [False, True])
def test_additive_schedule_migration_and_binding_constraints(tmp_path, desktop):
    migration = importlib.import_module("db.migrations.versions.pa6f7a8b9c0d_assistant_schedules")
    engine = create_engine(f"sqlite:///{tmp_path / 'schedule-migration.db'}")
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.exec_driver_sql("CREATE TABLE sessions (id VARCHAR(64) PRIMARY KEY, user_id TEXT, workspace_id TEXT, kind TEXT, is_deleted BOOLEAN)")
            for table in ("assistant_commands", "assistant_tasks", "assistant_task_submissions", "assistant_task_results"):
                extra = ", target_type TEXT, target_id TEXT, action TEXT" if table == "assistant_commands" else ""
                connection.exec_driver_sql(f"CREATE TABLE {table} (id VARCHAR(64) PRIMARY KEY{extra})")
            connection.exec_driver_sql("CREATE TABLE cron_jobs (id VARCHAR(64) PRIMARY KEY, name TEXT, enabled BOOLEAN, task_prompt TEXT)")
            connection.exec_driver_sql("CREATE TABLE cron_runs (id VARCHAR(64) PRIMARY KEY, job_id TEXT, ended_at DATETIME, summary_text TEXT)")
            connection.exec_driver_sql("INSERT INTO cron_jobs VALUES ('old','Legacy',1,'Retained original instructions')")
            connection.exec_driver_sql("INSERT INTO cron_runs VALUES ('old-run','old',CURRENT_TIMESTAMP,'Retained result')")
            if desktop:
                _upgrade_desktop_assistant_columns(connection)
                _upgrade_desktop_assistant_columns(connection)
            else:
                migration.upgrade()
            assert connection.exec_driver_sql("SELECT name,enabled,task_prompt,revision,assistant_session_id,assistant_command_id FROM cron_jobs").one() == (
                "Legacy", 1, "Retained original instructions", 1, None, None)
            assert connection.exec_driver_sql("SELECT summary_text,assistant_task_id FROM cron_runs").one() == ("Retained result", None)
            assert any(i["name"] == "uq_cron_assistant_active" and i["unique"] for i in inspect(connection).get_indexes("cron_runs"))
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("UPDATE cron_jobs SET assistant_session_id='main' WHERE id='old'")
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("UPDATE cron_runs SET assistant_task_id='task' WHERE id='old-run'")
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("UPDATE cron_jobs SET revision=0")
            connection.exec_driver_sql("UPDATE cron_jobs SET assistant_session_id='main',assistant_command_id='command' WHERE id='old'")
            connection.exec_driver_sql("UPDATE cron_runs SET assistant_task_id='task',assistant_submission_id='submission',assistant_configuration_id='command',assistant_slot='slot',ended_at=NULL WHERE id='old-run'")
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("INSERT INTO cron_runs (id,job_id,assistant_task_id,assistant_submission_id,assistant_configuration_id,assistant_slot) VALUES ('second','old','task-2','submission-2','command','slot-2')")
            if not desktop:
                with pytest.raises(RuntimeError, match="must be retained"):
                    migration.downgrade()
    finally:
        engine.dispose()
