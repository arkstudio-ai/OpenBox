"""Reversible empty schema and loss-preventing nonempty downgrade on SQLite."""
import importlib
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
import pytest

migration = importlib.import_module("db.migrations.versions.ph84a5b6c7d8_memory_protocol")


def test_empty_upgrade_downgrade_upgrade():
    with create_engine("sqlite://").begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) == {"memory_change_clocks", "memory_write_receipts", "memory_experiences"}
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
            migration.upgrade()
            assert len(inspect(connection).get_table_names()) == 3


def test_readiness_requires_protocol_migration_even_with_recall_disabled():
    from db.base import _missing_readiness_schema
    tables = {"memory_change_clocks", "memory_write_receipts", "memory_experiences"}
    with create_engine("sqlite://").begin() as connection:
        assert tables <= set(_missing_readiness_schema(connection))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert not any(name.split(".", 1)[0] in tables for name in _missing_readiness_schema(connection))
        connection.execute(text("ALTER TABLE memory_write_receipts DROP COLUMN payload_hash"))
        assert "memory_write_receipts.payload_hash" in _missing_readiness_schema(connection)


@pytest.mark.parametrize("table", ["memory_change_clocks", "memory_write_receipts", "memory_experiences"])
def test_committed_new_data_blocks_downgrade_before_any_table_drop(table):
    with create_engine("sqlite://").begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            if table == "memory_change_clocks":
                connection.execute(text("""INSERT INTO memory_change_clocks
                    (user_id,revision,updated_at) VALUES ('owner',1,CURRENT_TIMESTAMP)"""))
            elif table == "memory_write_receipts":
                connection.execute(text("""INSERT INTO memory_write_receipts
                    (user_id,operation_id,kind,payload_hash,state,result,source_refs,created_at)
                    VALUES ('owner','op','profile','hash','saved','{}','[]',CURRENT_TIMESTAMP)"""))
            else:
                connection.execute(text("""INSERT INTO memory_experiences
                    (id,user_id,workspace_id,operation_id,content,source_refs,validation,created_at,updated_at)
                    VALUES ('experience','owner','space','op','{}','[]','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""))
            with pytest.raises(RuntimeError, match="Keep memory protocol schema"):
                migration.downgrade()
            assert len(inspect(connection).get_table_names()) == 3
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
