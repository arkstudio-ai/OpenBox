"""Desktop-only additive schema bridge for durable Cron leases and outbox.

PostgreSQL deployments use Alembic. Desktop mode historically opens its
persistent SQLite database with ``metadata.create_all()``, which creates new
tables but cannot add columns to existing ones. Keep that legacy entry path
working without pretending this is a general migration runner.
"""
from __future__ import annotations

import sqlalchemy as sa


_JOB_COLUMNS = {
    "run_generation": "BIGINT NOT NULL DEFAULT 0",
    "run_token": "VARCHAR(64)",
    "run_owner": "VARCHAR(160)",
    "lease_expires_at": "DATETIME",
    "heartbeat_at": "DATETIME",
}

_RUN_COLUMNS = {
    "claim_token": "VARCHAR(64)",
    "claim_generation": "BIGINT",
    "claim_owner": "VARCHAR(160)",
}


def _upgrade_sqlite_cron_lease_schema(connection) -> None:
    """Idempotently bridge Cron lease fields and the durable delivery table."""
    if connection.dialect.name != "sqlite":
        return

    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    for table, columns in (
        ("cron_jobs", _JOB_COLUMNS),
        ("cron_runs", _RUN_COLUMNS),
    ):
        if table not in tables:
            continue
        present = {
            column["name"] for column in sa.inspect(connection).get_columns(table)
        }
        for name, ddl_type in columns.items():
            if name not in present:
                connection.exec_driver_sql(
                    f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl_type}'
                )

    # SQLite supports IF NOT EXISTS for indexes and fresh desktop databases
    # already have these through metadata.create_all().
    if "cron_jobs" in tables:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cron_jobs_lease "
            "ON cron_jobs (lease_expires_at)"
        )
    if "cron_runs" in tables:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cron_runs_claim "
            "ON cron_runs (job_id, claim_generation)"
        )

    # A new table is safe for metadata to create, but an already-open desktop
    # engine may have run create_all before this version was loaded.  Create
    # this one table explicitly as part of the same startup gate.
    from db.models.cron import CronDeliveryOutbox

    CronDeliveryOutbox.__table__.create(connection, checkfirst=True)


async def ensure_desktop_cron_lease_schema() -> None:
    """Upgrade an existing desktop SQLite store; no-op for PostgreSQL."""
    from db.base import get_engine

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
    async with engine.begin() as connection:
        await connection.run_sync(_upgrade_sqlite_cron_lease_schema)
