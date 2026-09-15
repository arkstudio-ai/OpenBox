"""The trajectory worker inside the backend process (SPEC §8.15; desktop and development).

``start_embedded_worker`` opens the trace database (default
``sqlite+aiosqlite:///<backend>/.openbox/trajectory.db``), brings its schema to
the migration head, starts WorkerServices and mounts the admin routers into the
business app. Alembic's env calls ``asyncio.run``, so no migration runs on this
event loop: SQLite is prepared by ``migrate_sqlite`` and PostgreSQL upgraded by
alembic, each in a worker thread. Viewer authority and audit use this process's
business database directly; a distributed backend (``JWT_SECRET`` and its Redis
cache) shares hints with its replicas over ``trajectory:hints``. Worker start
also removes stale temporary export archives.

``migrate_sqlite`` uses its own sqlite3 connection:

- a new database is stamped at the head and created by
  ``TraceBase.metadata.create_all``;
- a database ``create_all`` made before databases were stamped (trace tables,
  no version table) is stamped at ``CREATE_ALL_REVISION``, the head of that
  time, and then upgraded like any other;
- a database below the head is upgraded by alembic with the revision scripts
  but without env.py, which migrates only TRAJECTORY_DATABASE_URL;
- ``create_all`` finally adds any table still missing, which also completes a
  new database whose creation stopped right after its stamp.
"""
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, inspect, pool
from sqlalchemy.engine import make_url

from core.log import create_logger
from trajectory.config import BACKEND_DIR, integer

log = create_logger("trajectory.worker.embedded")

DEFAULT_DATABASE_PATH = BACKEND_DIR / ".openbox" / "trajectory.db"
MIGRATIONS_DIR = BACKEND_DIR / "trajectory" / "store" / "migrations"
VERSION_TABLE = "trajectory_alembic_version"
BUSINESS_VERSION_TABLE = "alembic_version"
#: The head while embedded SQLite databases were created by create_all without a version stamp.
CREATE_ALL_REVISION = "t0001_initial"


@dataclass
class _Embedded:
    services: Any
    delivery: Any
    owns_blob_store: bool = False
    owns_hints: bool = False


_embedded: _Embedded | None = None


def database_url() -> str:
    return (os.getenv("TRAJECTORY_DATABASE_URL") or "").strip() or f"sqlite+aiosqlite:///{DEFAULT_DATABASE_PATH}"


def oss_internal() -> bool:
    """Whether asset payload reads use the OSS VPC endpoint. An embedded worker runs wherever the backend
    runs (a desktop, a development host), so only an explicit TRAJECTORY_OSS_INTERNAL=true selects it."""
    return (os.getenv("TRAJECTORY_OSS_INTERNAL") or "").strip().lower() in {"1", "true", "yes", "on"}


def mount_admin_routers(app) -> None:
    """Include the worker's admin HTTP and WS routers into ``app`` once."""
    if getattr(app.state, "trajectory_admin_routers", False):
        return
    from trajectory.worker import routes, ws
    app.include_router(routes.router)
    app.include_router(ws.router)
    app.state.trajectory_admin_routers = True


def _alembic_config():
    from alembic.config import Config
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_table", VERSION_TABLE)
    return config


def _upgrade_head(url: str) -> None:
    from alembic import command
    # env.py migrates TRAJECTORY_DATABASE_URL only, so that is what this upgrades.
    if (os.getenv("TRAJECTORY_DATABASE_URL") or "").strip() != url:
        raise RuntimeError("Trace migrations run only against TRAJECTORY_DATABASE_URL")
    try:
        command.upgrade(_alembic_config(), "head")
    except SystemExit as exc:
        raise RuntimeError(str(exc.code)) from None


def migrate_sqlite(url: str) -> None:
    """Bring the SQLite trace database of ``url`` to the migration head, as the module docstring describes.

    Blocking: call it in a worker thread, never on an event loop.
    """
    import trajectory.store.models  # noqa: F401  (registers the trace tables)
    from alembic.runtime.environment import EnvironmentContext
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from trajectory.store.database import TraceBase

    config = _alembic_config()
    script = ScriptDirectory.from_config(config)
    heads = set(script.get_heads())
    # A plain sqlite3 connection keeps foreign keys off, as migrations that copy tables need.
    engine = create_engine(make_url(url).set(drivername="sqlite"), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            if BUSINESS_VERSION_TABLE in tables:
                raise RuntimeError(f"The trace database holds the business migration table {BUSINESS_VERSION_TABLE}")
            context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
            if VERSION_TABLE not in tables:
                created = bool(tables & set(TraceBase.metadata.tables))
                context.stamp(script, CREATE_ALL_REVISION if created else "heads")
                connection.commit()
            current = set(context.get_current_heads())
            # End the transaction that read began, so alembic owns the migration's.
            connection.rollback()
            if current != heads:
                def upgrade(revision, _context):
                    return script._upgrade_revs("heads", revision)

                with EnvironmentContext(config, script, fn=upgrade, destination_rev="heads") as environment:
                    environment.configure(connection=connection, target_metadata=TraceBase.metadata,
                                          version_table=VERSION_TABLE)
                    with environment.begin_transaction():
                        environment.run_migrations()
                connection.commit()
                log.info("Embedded trace database upgraded from=%s to=%s", ",".join(sorted(current)) or "base",
                         ",".join(sorted(heads)))
            TraceBase.metadata.create_all(connection)
            connection.commit()
    finally:
        engine.dispose()


async def prepare_schema(engine, url: str) -> None:
    if engine.dialect.name != "sqlite":
        await asyncio.to_thread(_upgrade_head, url)
        return
    database = make_url(url).database
    if not database or database == ":memory:":
        # An in-memory database lives in its connections: there is nothing to stamp or upgrade.
        import trajectory.store.models  # noqa: F401  (registers the trace tables)
        from trajectory.store.database import TraceBase
        async with engine.begin() as connection:
            await connection.run_sync(TraceBase.metadata.create_all)
        return
    await asyncio.to_thread(migrate_sqlite, url)


async def _open_hints() -> bool:
    """Open the hint channel for a distributed backend; whether this call opened it.

    Distributed means JWT_SECRET with the business Redis cache. A process whose
    cache is not Redis (the dev server) has no replicas to reach: in-process
    dispatch reaches every socket, and no Redis connection is attempted.
    """
    try:
        from cache import get_cache
        from cache.redis_cache import RedisCache
        from core.config import get_config
        config = get_config()
        if not config.jwt_secret or not isinstance(get_cache(), RedisCache):
            return False
        from bus.trajectory_hints import init_trajectory_hints
        return await init_trajectory_hints(config.redis_url) is not None
    except Exception as exc:
        log.warning("Trajectory hint channel did not open error_type=%s", type(exc).__name__)
        return False


async def start_embedded_worker(app, *, blob_store=None, services_factory: Callable[[Any], Any] | None = None,
                                asset_reader=None) -> None:
    """Start the worker in this process once and mount its admin routers into ``app``.

    A ``blob_store`` given here becomes the process store (admin reads and exports use
    ``get_blob_store()``); ``asset_reader`` replaces the OSS reader of asset payloads.
    """
    global _embedded
    if _embedded is None:
        from trajectory.auth import AuditDelivery, LocalBackend, configure_backend
        from trajectory.export import remove_stale_temp_files
        from trajectory.payload import oss_asset_reader, set_asset_reader
        from trajectory.store.database import close_trace_engine, init_trace_engine
        from trajectory.storage import get_blob_store, set_blob_store
        from trajectory.worker.app import default_services

        url = database_url()
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).expanduser().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        engine = init_trace_engine(url, pool_size=integer("TRAJECTORY_DB_POOL_SIZE", 5),
                                   max_overflow=integer("TRAJECTORY_DB_POOL_OVERFLOW", 5))
        services = None
        owns_hints = False
        try:
            await prepare_schema(engine, url)
            if blob_store is not None:
                set_blob_store(blob_store)
            store = blob_store if blob_store is not None else get_blob_store()
            set_asset_reader(asset_reader or oss_asset_reader(internal=oss_internal()))
            await asyncio.to_thread(remove_stale_temp_files)
            services = (services_factory or default_services)(store)
            await services.start()
            owns_hints = await _open_hints()
            backend = LocalBackend()
            configure_backend(backend)
            delivery = AuditDelivery(backend)
            delivery.start()
        except BaseException:
            try:
                if services is not None:
                    await services.stop()
            finally:
                if owns_hints:
                    from bus.trajectory_hints import close_trajectory_hints
                    await close_trajectory_hints()
                configure_backend(None)
                set_asset_reader(None)
                if blob_store is not None:
                    set_blob_store(None)
                await close_trace_engine()
            raise
        _embedded = _Embedded(services=services, delivery=delivery, owns_blob_store=blob_store is not None,
                              owns_hints=owns_hints)
        log.info("Embedded trajectory worker started writer=%s", getattr(services, "is_writer", False))
    mount_admin_routers(app)


async def stop_embedded_worker() -> None:
    global _embedded
    embedded, _embedded = _embedded, None
    if embedded is None:
        return
    from trajectory.auth import configure_backend
    from trajectory.payload import set_asset_reader
    from trajectory.storage import set_blob_store
    from trajectory.store.database import close_trace_engine
    try:
        await embedded.delivery.stop()
        await embedded.services.stop()
    finally:
        if embedded.owns_hints:
            from bus.trajectory_hints import close_trajectory_hints
            await close_trajectory_hints()
        configure_backend(None)
        set_asset_reader(None)
        if embedded.owns_blob_store:
            set_blob_store(None)
        await close_trace_engine()
    log.info("Embedded trajectory worker stopped")
