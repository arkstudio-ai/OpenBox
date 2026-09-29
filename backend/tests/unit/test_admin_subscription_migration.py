"""Existing paid terms survive server and desktop schema upgrades."""
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from db.base import _upgrade_desktop_billing_columns


def old_schema(connection):
    connection.exec_driver_sql('CREATE TABLE workspaces (id VARCHAR(64) PRIMARY KEY)')
    connection.exec_driver_sql('CREATE TABLE payment_orders (id VARCHAR(64) PRIMARY KEY)')
    with Operations.context(MigrationContext.configure(connection)):
        import_module('db.migrations.versions.b9d1f3a5c7e9_subscription_plans').upgrade()
    connection.exec_driver_sql("INSERT INTO workspaces VALUES ('ws')")
    connection.exec_driver_sql("INSERT INTO payment_orders (id) VALUES ('paid')")
    connection.exec_driver_sql("INSERT INTO billing_subscriptions VALUES ('paid','ws','pro','monthly','{}','2026-01-01','2026-02-01')")


@pytest.mark.parametrize('desktop', [False, True])
def test_upgrade_preserves_paid_term_and_supports_manual_grant(desktop):
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        old_schema(connection)
        if desktop:
            _upgrade_desktop_billing_columns(connection)
            _upgrade_desktop_billing_columns(connection)
        else:
            with Operations.context(MigrationContext.configure(connection)):
                import_module('db.migrations.versions.f8b3d6a1c092_admin_subscription_management').upgrade()
        assert connection.exec_driver_sql('SELECT id, order_id, plan_id, cancelled_at FROM billing_subscriptions').one() == ('paid', 'paid', 'pro', None)
        connection.exec_driver_sql("INSERT INTO billing_subscriptions (id,workspace_id,plan_id,cycle,plan,starts_at,ends_at) VALUES ('manual','ws','max','monthly','{}','2026-02-01','2026-03-01')")
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM billing_subscriptions').scalar_one() == 2
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match='operator-managed'):
            import_module('db.migrations.versions.f8b3d6a1c092_admin_subscription_management').downgrade()
    engine.dispose()


def test_unused_server_migration_round_trips():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        old_schema(connection)
        migration = import_module('db.migrations.versions.f8b3d6a1c092_admin_subscription_management')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
        assert connection.exec_driver_sql('SELECT order_id,plan_id FROM billing_subscriptions').one() == ('paid','pro')
        assert sa.inspect(connection).get_pk_constraint('billing_subscriptions')['constrained_columns'] == ['order_id']
    engine.dispose()


def test_business_migrations_have_one_unambiguous_head():
    from pathlib import Path
    import warnings
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "db" / "migrations"))
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        script = ScriptDirectory.from_config(config)
        assert script.get_heads() == ["f8b3d6a1c092"]
        revisions = list(script.walk_revisions())
        assert len({item.revision for item in revisions}) == len(revisions)
