import importlib
import pytest
from sqlalchemy import create_engine, inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_additive_migration_up_down_and_nullable_trace_context(tmp_path):
    migration=importlib.import_module('db.migrations.versions.f6a8c0e2b4d6_session_trajectories')
    engine=create_engine(f'sqlite:///{tmp_path / "migration.sqlite"}')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE session_executions (session_id TEXT PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE cron_runs (id TEXT PRIMARY KEY)')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            inspector=inspect(connection)
            assert {'trajectory_events','trajectory_records','trajectory_payloads','trajectory_exports'} <= set(inspector.get_table_names())
            assert next(item for item in inspector.get_columns('session_executions') if item['name']=='trace_context')['nullable']
            assert {'content','storage_status'} <= {item['name'] for item in inspector.get_columns('trajectory_payloads')}
            migration.downgrade()
            assert set(inspect(connection).get_table_names())=={'session_executions','cron_runs'}
    engine.dispose()
