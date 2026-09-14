"""Alembic environment for the trajectory trace database.

Migrates only ``TRAJECTORY_DATABASE_URL``. It never falls back to
``DATABASE_URL`` or an ini URL: every container receives the business URL, so
a fallback would silently migrate the business database.
"""
import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import inspect, pool
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine

from db.base import JSONType
from trajectory.store.database import TraceBase
from trajectory.store.partitions import DEFAULT_PARTITION, partition_date
import trajectory.store.models  # noqa: F401  (registers the trace tables)

VERSION_TABLE = "trajectory_alembic_version"
BUSINESS_VERSION_TABLE = "alembic_version"
_DEFAULT_PORTS = {"postgresql": 5432}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = TraceBase.metadata
version_table = config.get_main_option("version_table") or VERSION_TABLE


class MigrationRefused(Exception):
    pass


def _database_identity(raw: str) -> tuple | None:
    """What a URL connects to, ignoring driver, credentials and query; None when unparsable."""
    try:
        url = make_url(raw)
    except ArgumentError:
        return None
    backend = url.get_backend_name()
    if backend == "sqlite":
        database = url.database or ""
        if database and database != ":memory:" and not database.startswith("file:"):
            database = str(Path(database).expanduser().resolve())
        return (backend, database)
    host = (url.host or "localhost").lower()
    return (backend, "localhost" if host in _LOOPBACK else host,
            url.port or _DEFAULT_PORTS.get(backend), url.database or "")


def trace_database_url() -> str:
    url = os.environ.get("TRAJECTORY_DATABASE_URL", "").strip()
    if not url:
        raise MigrationRefused("TRAJECTORY_DATABASE_URL is not set; DATABASE_URL is never used for trace migrations")
    identity = _database_identity(url)
    if identity is None:
        raise MigrationRefused("TRAJECTORY_DATABASE_URL is not a valid database URL")
    business = os.environ.get("DATABASE_URL", "").strip()
    if business and (url == business or identity == _database_identity(business)):
        raise MigrationRefused("TRAJECTORY_DATABASE_URL points at the business database (DATABASE_URL)")
    if version_table == BUSINESS_VERSION_TABLE:
        raise MigrationRefused(f"version_table must not be the business {BUSINESS_VERSION_TABLE}")
    return url


def render_item(type_, obj, autogen_context):
    """Render JSONType as JSONB with SQLite fallback in migration scripts."""
    if type_ == "type" and isinstance(obj, JSONType):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return 'postgresql.JSONB().with_variant(sa.Text(), "sqlite")'
    return False


def include_name(name, type_, parent_names) -> bool:
    """Autogenerate never reflects the ``trajectory_events`` partitions.

    The default partition comes from the migration and the daily ones from
    ``trajectory.store.partitions``, not from the models, so autogenerate
    would otherwise propose dropping them together with their hot events.
    """
    if type_ == "table" and name is not None:
        return name != DEFAULT_PARTITION and partition_date(name) is None
    return True


def include_object_for(dialect_name: str):
    """Autogenerate compares a dialect-conditional index (``Index.ddl_if``) only on its own dialect."""
    def include_object(obj, name, type_, reflected, compare_to) -> bool:
        condition = getattr(obj, "_ddl_if", None)
        if type_ != "index" or reflected or condition is None or condition.dialect is None:
            return True
        dialects = (condition.dialect,) if isinstance(condition.dialect, str) else tuple(condition.dialect)
        return dialect_name in dialects

    return include_object


def run_migrations_offline(url: str) -> None:
    """Run migrations in 'offline' mode: generate SQL without connecting."""
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=version_table,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


#: DDL on populated tables can outlast the trace role's 5 s statement_timeout default (PostgreSQL).
MIGRATION_STATEMENT_TIMEOUT = "1h"


def do_run_migrations(connection) -> None:
    business_database = inspect(connection).has_table(BUSINESS_VERSION_TABLE)
    # The inspection autobegan a transaction. Alembic would treat it as an
    # external transaction and never commit, so end it and let alembic own one.
    connection.rollback()
    if business_database:
        raise MigrationRefused(f"the target database holds the business migration table {BUSINESS_VERSION_TABLE}")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
        render_item=render_item,
        include_name=include_name,
        include_object=include_object_for(connection.dialect.name),
    )
    with context.begin_transaction():
        if connection.dialect.name == "postgresql":
            context.execute(f"SET LOCAL statement_timeout = '{MIGRATION_STATEMENT_TIMEOUT}'")
        context.run_migrations()


async def run_async_migrations(url: str) -> None:
    """Run migrations in 'online' mode with an async engine."""
    connectable = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def main() -> None:
    try:
        url = trace_database_url()
        if context.is_offline_mode():
            run_migrations_offline(url)
        else:
            asyncio.run(run_async_migrations(url))
    except MigrationRefused as exc:
        sys.exit(f"Trajectory trace migrations refused: {exc}")


main()
