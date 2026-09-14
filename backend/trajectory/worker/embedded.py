"""The trajectory worker inside the backend process (SPEC §8.15; desktop and development).

``start_embedded_worker`` opens the trace database (default
``sqlite+aiosqlite:///<backend>/.openbox/trajectory.db``), creates its schema,
starts WorkerServices and mounts the admin routers into the business app.
Alembic's env calls ``asyncio.run``, so it never runs on this event loop: SQLite
gets ``TraceBase.metadata.create_all`` and PostgreSQL an upgrade in a thread.
Viewer authority and audit use this process's business database directly.
"""
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.engine import make_url

from core.log import create_logger
from trajectory.config import BACKEND_DIR, integer

log = create_logger("trajectory.worker.embedded")

DEFAULT_DATABASE_PATH = BACKEND_DIR / ".openbox" / "trajectory.db"
MIGRATIONS_DIR = BACKEND_DIR / "trajectory" / "store" / "migrations"


@dataclass
class _Embedded:
    services: Any
    delivery: Any


_embedded: _Embedded | None = None


def database_url() -> str:
    return (os.getenv("TRAJECTORY_DATABASE_URL") or "").strip() or f"sqlite+aiosqlite:///{DEFAULT_DATABASE_PATH}"


def mount_admin_routers(app) -> None:
    """Include the worker's admin HTTP and WS routers into ``app`` once."""
    if getattr(app.state, "trajectory_admin_routers", False):
        return
    from trajectory.worker import routes, ws
    app.include_router(routes.router)
    app.include_router(ws.router)
    app.state.trajectory_admin_routers = True


def _upgrade_head(url: str) -> None:
    from alembic import command
    from alembic.config import Config
    # env.py migrates TRAJECTORY_DATABASE_URL only, so that is what this upgrades.
    if (os.getenv("TRAJECTORY_DATABASE_URL") or "").strip() != url:
        raise RuntimeError("Trace migrations run only against TRAJECTORY_DATABASE_URL")
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_table", "trajectory_alembic_version")
    try:
        command.upgrade(config, "head")
    except SystemExit as exc:
        raise RuntimeError(str(exc.code)) from None


async def prepare_schema(engine, url: str) -> None:
    if engine.dialect.name == "sqlite":
        import trajectory.store.models  # noqa: F401  (registers the trace tables)
        from trajectory.store.database import TraceBase
        async with engine.begin() as connection:
            await connection.run_sync(TraceBase.metadata.create_all)
        return
    await asyncio.to_thread(_upgrade_head, url)


async def start_embedded_worker(app, *, blob_store=None, services_factory: Callable[[Any], Any] | None = None) -> None:
    global _embedded
    if _embedded is None:
        from trajectory.auth import AuditDelivery, LocalBackend, configure_backend
        from trajectory.store.database import close_trace_engine, init_trace_engine
        from trajectory.storage import get_blob_store
        from trajectory.worker.app import default_services

        url = database_url()
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).expanduser().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        engine = init_trace_engine(url, pool_size=integer("TRAJECTORY_DB_POOL_SIZE", 5),
                                   max_overflow=integer("TRAJECTORY_DB_POOL_OVERFLOW", 5))
        services = None
        try:
            await prepare_schema(engine, url)
            store = blob_store if blob_store is not None else get_blob_store()
            services = (services_factory or default_services)(store)
            await services.start()
            backend = LocalBackend()
            configure_backend(backend)
            delivery = AuditDelivery(backend)
            delivery.start()
        except BaseException:
            try:
                if services is not None:
                    await services.stop()
            finally:
                await close_trace_engine()
            raise
        _embedded = _Embedded(services=services, delivery=delivery)
        log.info("Embedded trajectory worker started writer=%s", getattr(services, "is_writer", False))
    mount_admin_routers(app)


async def stop_embedded_worker() -> None:
    global _embedded
    embedded, _embedded = _embedded, None
    if embedded is None:
        return
    from trajectory.auth import configure_backend
    from trajectory.store.database import close_trace_engine
    try:
        await embedded.delivery.stop()
        await embedded.services.stop()
    finally:
        configure_backend(None)
        await close_trace_engine()
    log.info("Embedded trajectory worker stopped")
