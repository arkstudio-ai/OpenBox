"""Trajectory worker application (SPEC §8.0, §8.12-8.13).

``python -m trajectory.worker`` serves it on TRAJECTORY_WORKER_PORT. The lifespan
opens the trace database, the ticket and token-revocation stores shared with
the backend, the blob store and WorkerServices; a worker without the
single-writer lock keeps serving reads. ``/health`` and ``/metrics`` are for the
admin network only (nginx routes neither).
"""
import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect as sa_inspect, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from core.log import create_logger
from trajectory.config import BACKEND_DIR, integer, spool_dir
from trajectory.worker import routes, ws
from trajectory.worker.metrics import TraceDbSizeSampler, get_metrics

log = create_logger("trajectory.worker.app")

VERSION = "0.1.0"
VERSION_TABLE = "trajectory_alembic_version"
MIGRATIONS_DIR = BACKEND_DIR / "trajectory" / "store" / "migrations"
HEALTH_TIMEOUT_SECONDS = 5.0
#: A key no trajectory uses; /health only asks the store whether it exists.
HEALTH_PROBE_TRAJECTORY = "trj_health_probe"


@dataclass
class WorkerRuntime:
    services: Any
    blob_store: Any
    spool_dir: Path


@lru_cache(maxsize=1)
def migration_heads() -> frozenset[str]:
    """Heads of the trace alembic chain, read from the scripts (no database, no env.py)."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def schema_current(connection) -> bool:
    """The trace schema is at the alembic head or, when no alembic ever ran (create_all), complete."""
    import trajectory.store.models  # noqa: F401  (registers the trace tables)
    from trajectory.store.database import TraceBase
    tables = set(sa_inspect(connection).get_table_names())
    if VERSION_TABLE in tables:
        versions = {row[0] for row in connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}"))}
        return versions == migration_heads()
    return set(TraceBase.metadata.tables) <= tables


async def check_schema(url: str) -> bool:
    """One schema check with a throwaway engine, before the worker serves."""
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(schema_current)
    finally:
        await engine.dispose()


async def database_ok() -> bool:
    from trajectory.store.database import get_trace_engine
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
            async with get_trace_engine().connect() as connection:
                return await connection.run_sync(schema_current)
    except Exception as exc:
        log.warning("Trace database health check failed error_type=%s", type(exc).__name__)
        return False


def spool_ok(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
    except OSError:
        return False


async def blob_store_ok(store) -> bool:
    from trajectory.storage import blob_key
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
            await store.exists(blob_key(HEALTH_PROBE_TRAJECTORY, "0" * 64))
        return True
    except Exception as exc:
        log.warning("Blob store health check failed error_type=%s", type(exc).__name__)
        return False


async def health(request: Request) -> JSONResponse:
    """200 while reads can be served (the trace database is usable); ``status`` says whether everything is."""
    runtime: WorkerRuntime | None = getattr(request.app.state, "worker", None)
    if runtime is None:
        body = {"status": "degraded", "writer": False, "db": False, "spool": False, "blob_store": False,
                "version": VERSION}
        return JSONResponse(body, status_code=503, headers={"Cache-Control": "no-store"})
    db, blob = await asyncio.gather(database_ok(), blob_store_ok(runtime.blob_store))
    writer = getattr(runtime.services, "is_writer", False) is True
    spool = spool_ok(runtime.spool_dir)
    body = {"status": "ok" if writer and db and spool and blob else "degraded", "writer": writer, "db": db,
            "spool": spool, "blob_store": blob, "version": VERSION}
    return JSONResponse(body, status_code=200 if db else 503, headers={"Cache-Control": "no-store"})


async def metrics() -> JSONResponse:
    return JSONResponse(get_metrics().snapshot(), headers={"Cache-Control": "no-store"})


def default_services(blob_store):
    from trajectory.worker.services import WorkerServices
    from trajectory.worker.settings import get_worker_settings
    return WorkerServices(get_worker_settings(), blob_store=blob_store)


def _auth_stores():
    """Tickets and token revocation shared with the backend: Redis with JWT_SECRET, else process memory."""
    from auth.jwt import init_auth
    from auth.middleware import init_blacklist
    from auth.ticket import init_ticket_store
    from core.config import get_config
    config = get_config()
    if not config.jwt_secret:
        # Every request is then the single-user desktop administrator "default";
        # a server worker must share JWT_SECRET (and REDIS_URL) with the backend.
        log.warning("JWT_SECRET is not set: the trajectory worker serves the single-user desktop identity")
        from cache.memory_cache import MemoryCache
        cache = MemoryCache()
        init_ticket_store(cache)
        return cache, False
    from cache.redis_cache import RedisCache
    init_auth(config.jwt_secret, config.jwt_access_expire_minutes, config.jwt_refresh_expire_days)
    cache = RedisCache(config.redis_url)
    init_ticket_store(cache)
    init_blacklist(cache)
    return cache, True


def create_app(*, database_url: str | None = None, blob_store=None,
               services_factory: Callable[[Any], Any] | None = None, backend=None, cache=None,
               asset_reader=None) -> FastAPI:
    """The worker ASGI app. Arguments replace components the lifespan otherwise builds from the environment;
    with ``cache`` given, the caller owns the ticket store, the token blacklist and JWT settings.

    A ``blob_store`` given here is the process store while the app runs (routes and exports read through
    ``get_blob_store()``); ``asset_reader`` replaces the OSS reader of asset payloads."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from trajectory.auth import AuditDelivery, HttpBackend, configure_backend
        from trajectory.payload import oss_asset_reader, set_asset_reader
        from trajectory.store.database import close_trace_engine, init_trace_engine
        from trajectory.storage import get_blob_store, set_blob_store

        url = (database_url or os.getenv("TRAJECTORY_DATABASE_URL") or "").strip()
        if not url:
            raise RuntimeError("TRAJECTORY_DATABASE_URL is required by the trajectory worker")
        async with AsyncExitStack() as stack:
            init_trace_engine(url, pool_size=integer("TRAJECTORY_DB_POOL_SIZE", 5),
                              max_overflow=integer("TRAJECTORY_DB_POOL_OVERFLOW", 5))
            stack.push_async_callback(close_trace_engine)
            if cache is None:
                shared_cache, distributed = _auth_stores()
                stack.push_async_callback(shared_cache.close)
                if distributed:
                    # Replicas fan trajectory.available out over trajectory:hints (SPEC §8.7, WAVE3 contract 3);
                    # the worker never subscribes to the business bus channel.
                    from bus.trajectory_hints import close_trajectory_hints, init_trajectory_hints
                    from core.config import get_config
                    await init_trajectory_hints(get_config().redis_url)
                    stack.push_async_callback(close_trajectory_hints)
            client = backend if backend is not None else HttpBackend.from_env()
            if backend is None:
                stack.push_async_callback(client.close)
            configure_backend(client)
            stack.callback(configure_backend, None)
            if blob_store is not None:
                set_blob_store(blob_store)
                stack.callback(set_blob_store, None)
            store = blob_store if blob_store is not None else get_blob_store()
            # Asset payloads: the business bucket, over the VPC endpoint unless TRAJECTORY_OSS_INTERNAL=false.
            set_asset_reader(asset_reader or oss_asset_reader())
            stack.callback(set_asset_reader, None)
            services = (services_factory or default_services)(store)
            await services.start()
            stack.push_async_callback(services.stop)
            delivery = AuditDelivery(client)
            delivery.start()
            stack.push_async_callback(delivery.stop)
            sampler = TraceDbSizeSampler()
            sampler.start()
            stack.push_async_callback(sampler.stop)
            app.state.worker = WorkerRuntime(services=services, blob_store=store, spool_dir=spool_dir())
            stack.callback(setattr, app.state, "worker", None)
            log.info("Trajectory worker serving writer=%s", getattr(services, "is_writer", False))
            yield
        log.info("Trajectory worker stopped")

    from core.config import get_config
    app = FastAPI(title="OpenBox trajectory worker", version=VERSION, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_config().cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes.router)
    app.include_router(ws.router)
    app.add_api_route("/health", health, methods=["GET"], include_in_schema=False)
    app.add_api_route("/metrics", metrics, methods=["GET"], include_in_schema=False)
    return app
