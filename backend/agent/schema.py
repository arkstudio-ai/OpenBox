"""Desktop SQLite bridge for durable subagent protocol tables.

PostgreSQL uses Alembic. Desktop startup historically uses metadata.create_all;
this explicit checkfirst bridge also supports an already-open legacy engine in
tests and embedded launchers without pretending to be a general migrator.
"""
from __future__ import annotations


def _upgrade_sqlite_subagent_schema(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    from db.models.subagent import (
        SubagentActivation,
        SubagentDescriptor,
        SubagentOutbox,
    )

    for table in (
        SubagentDescriptor.__table__,
        SubagentActivation.__table__,
        SubagentOutbox.__table__,
    ):
        table.create(connection, checkfirst=True)

    # ``create(checkfirst=True)`` does not evolve an already-created desktop
    # table.  Keep the embedded SQLite bridge monotonic with the Alembic head;
    # the empty default is intentionally rejected by the runtime for legacy
    # live descriptors because their former parent boundary is unknowable.
    columns = {
        row[1]
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(subagent_descriptors)"
        )
    }
    if "authority_snapshot" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE subagent_descriptors ADD COLUMN "
            "authority_snapshot TEXT NOT NULL DEFAULT '{}'"
        )


async def ensure_desktop_subagent_schema() -> None:
    from db.base import get_engine

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
    async with engine.begin() as connection:
        await connection.run_sync(_upgrade_sqlite_subagent_schema)
