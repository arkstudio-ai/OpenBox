"""Embedded worker in the backend process: SQLite create_all (never alembic on the loop), services and routers."""
import asyncio
import os
import tempfile
import threading
import time
import types

import httpx
import pytest
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import auth.middleware as middleware
import auth.ticket as tickets
import trajectory.auth as trajectory_auth
from cache.memory_cache import MemoryCache
from tests.unit.test_worker_app_harness import (PREFIX, FakeServices, ReadLayer, admin_env, business_db,  # noqa: F401
    seed_trace, socket)
from trajectory.config import BACKEND_DIR
from trajectory.storage import MemoryBlobStore
from trajectory.store.database import TraceBase, get_trace_engine
from trajectory.worker import embedded


def admin_routes(app) -> list[str]:
    return [route.path for route in app.routes if isinstance(route, (APIRoute, APIWebSocketRoute))
            and route.path.startswith(("/api/admin/trajectories", "/ws/admin/trajectories"))]


@pytest.fixture
def desktop(business_db, admin_env, monkeypatch, tmp_path):
    """Desktop mode: no JWT, a process ticket store, the business database of this process."""
    monkeypatch.setattr(middleware, "_auth_enabled", False)
    monkeypatch.setattr(tickets, "_cache", MemoryCache())
    monkeypatch.setattr(trajectory_auth, "_backend", None)
    monkeypatch.setattr(trajectory_auth, "_facts", {})
    monkeypatch.setattr(trajectory_auth, "_audited", {})
    monkeypatch.setattr(embedded, "_embedded", None)
    path = tmp_path / "nested" / "trajectory.db"
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    return path


def test_default_database_is_the_sqlite_file_next_to_the_backend(monkeypatch):
    monkeypatch.delenv("TRAJECTORY_DATABASE_URL", raising=False)
    assert embedded.database_url() == f"sqlite+aiosqlite:///{BACKEND_DIR / '.openbox' / 'trajectory.db'}"
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", " postgresql+asyncpg://trace@db/openbox_trace ")
    assert embedded.database_url() == "postgresql+asyncpg://trace@db/openbox_trace"


async def test_embedded_worker_serves_the_admin_api_from_the_backend_process(desktop, monkeypatch):
    from alembic import command

    def no_alembic(*args, **kwargs):
        raise AssertionError("alembic must not run inside the backend event loop")

    monkeypatch.setattr(command, "upgrade", no_alembic)
    blob = MemoryBlobStore()
    ReadLayer(blob).install(monkeypatch)
    services = FakeServices()
    app = FastAPI()
    await embedded.start_embedded_worker(app, blob_store=blob, services_factory=lambda store: services)
    try:
        engine = create_engine(f"sqlite:///{desktop}")
        try:
            assert set(TraceBase.metadata.tables) <= set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert (desktop.parent.stat().st_mode & 0o777) == 0o700
        assert services.started == 1 and isinstance(trajectory_auth.get_backend(), trajectory_auth.LocalBackend)
        routes = admin_routes(app)
        assert len(routes) == 14 and len(set(routes)) == 14
        await embedded.start_embedded_worker(app, blob_store=blob, services_factory=lambda store: FakeServices())
        embedded.mount_admin_routers(app)
        assert admin_routes(app) == routes and services.started == 1

        await seed_trace(blob)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://desktop") as client:
            listing = await client.get(PREFIX + "/sessions")  # the default desktop admin needs no token
            assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
            ticket = (await client.post(PREFIX + "/ticket")).json()["ticket"]
        async with socket(app, ticket) as (send, receive):
            assert (await receive())["type"] == "websocket.accept"
            await send({"type": "subscribe", "session_id": "session_a_1"})
            assert (await receive())["data"]["trajectory_id"] == "trj_a1"
        await trajectory_auth.AuditDelivery().deliver_once()
    finally:
        await embedded.stop_embedded_worker()
    assert services.stopped == 1 and trajectory_auth._backend is None
    with pytest.raises(RuntimeError):
        get_trace_engine()
    await embedded.stop_embedded_worker()
    assert services.stopped == 1


async def test_a_failed_start_releases_the_engine_and_mounts_nothing(desktop):
    class Broken(FakeServices):
        async def start(self):
            await super().start()
            raise OSError("spool unavailable")

    broken = Broken()
    app = FastAPI()
    with pytest.raises(OSError):
        await embedded.start_embedded_worker(app, blob_store=MemoryBlobStore(), services_factory=lambda store: broken)
    assert broken.stopped == 1 and embedded._embedded is None and admin_routes(app) == []
    with pytest.raises(RuntimeError):
        get_trace_engine()


async def test_postgresql_schema_is_migrated_in_a_worker_thread(monkeypatch):
    threads = []
    monkeypatch.setattr(embedded, "_upgrade_head", lambda url: threads.append((url, threading.current_thread())))
    engine = types.SimpleNamespace(dialect=types.SimpleNamespace(name="postgresql"))
    await embedded.prepare_schema(engine, "postgresql+asyncpg://trace@db/openbox_trace")
    (url, thread), = threads
    assert url == "postgresql+asyncpg://trace@db/openbox_trace" and thread is not threading.main_thread()


def test_trace_migrations_only_target_the_configured_url(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", "postgresql+asyncpg://trace@db/openbox_trace")
    with pytest.raises(RuntimeError, match="TRAJECTORY_DATABASE_URL"):
        embedded._upgrade_head("postgresql+asyncpg://trace@elsewhere/openbox")


async def test_start_does_not_block_other_tasks(desktop):
    ticks = []

    async def ticker():
        for _ in range(3):
            ticks.append(1)
            await asyncio.sleep(0)

    app = FastAPI()
    services = FakeServices()
    await asyncio.gather(ticker(), embedded.start_embedded_worker(app, blob_store=MemoryBlobStore(),
                                                                 services_factory=lambda store: services))
    try:
        assert len(ticks) == 3 and services.started == 1
    finally:
        await embedded.stop_embedded_worker()


def _versions(path) -> set[str]:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            return {row[0] for row in connection.execute(text(f"SELECT version_num FROM {embedded.VERSION_TABLE}"))}
    finally:
        engine.dispose()


def _heads() -> set[str]:
    return set(ScriptDirectory.from_config(embedded._alembic_config()).get_heads())


@pytest.fixture
def schema_steps(monkeypatch):
    """The revisions migrate_sqlite stamps and the threads its alembic upgrades run in."""
    seen = types.SimpleNamespace(stamps=[], upgrades=[])
    stamp, run_migrations = MigrationContext.stamp, EnvironmentContext.run_migrations

    def recorded_stamp(self, script, revision):
        seen.stamps.append(revision)
        return stamp(self, script, revision)

    def recorded_run(self, **kwargs):
        seen.upgrades.append(threading.current_thread())
        return run_migrations(self, **kwargs)

    monkeypatch.setattr(MigrationContext, "stamp", recorded_stamp)
    monkeypatch.setattr(EnvironmentContext, "run_migrations", recorded_run)
    return seen


def test_a_new_sqlite_database_is_stamped_at_the_head_and_created(tmp_path, schema_steps):
    path = tmp_path / "new.db"
    embedded.migrate_sqlite(f"sqlite+aiosqlite:///{path}")
    assert schema_steps.stamps == ["heads"] and schema_steps.upgrades == []
    assert _versions(path) == _heads() and len(_heads()) == 1
    engine = create_engine(f"sqlite:///{path}")
    try:
        assert set(TraceBase.metadata.tables) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


async def test_a_create_all_database_is_stamped_at_the_initial_revision_and_upgraded_in_a_worker_thread(
        tmp_path, schema_steps):
    path = tmp_path / "created.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        TraceBase.metadata.create_all(engine)
    finally:
        engine.dispose()
    url = f"sqlite+aiosqlite:///{path}"
    trace = create_async_engine(url)
    try:
        await embedded.prepare_schema(trace, url)
    finally:
        await trace.dispose()
    assert schema_steps.stamps == [embedded.CREATE_ALL_REVISION] == ["t0001_initial"]
    assert _versions(path) == _heads() and embedded.CREATE_ALL_REVISION not in _heads()
    (thread,) = schema_steps.upgrades
    assert thread is not threading.current_thread()


async def test_start_removes_stale_export_temp_files(desktop, monkeypatch, tmp_path):
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    stale, building = temp / "openbox-export-crashed.zip", temp / "openbox-export-building.zip"
    for path in (stale, building):
        path.write_bytes(b"zip")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    services = FakeServices()
    await embedded.start_embedded_worker(FastAPI(), blob_store=MemoryBlobStore(), services_factory=lambda store: services)
    try:
        assert (stale.exists(), building.exists(), services.started) == (False, True, 1)
    finally:
        await embedded.stop_embedded_worker()


async def test_admin_routes_answer_503_while_the_trace_database_is_not_open(desktop):
    """main.create_app mounts the routers in embedded mode; they stay mounted when the worker then fails to start."""
    class Broken(FakeServices):
        async def start(self):
            raise OSError("spool unavailable")

    app = FastAPI()
    embedded.mount_admin_routers(app)
    with pytest.raises(OSError):
        await embedded.start_embedded_worker(app, blob_store=MemoryBlobStore(), services_factory=lambda store: Broken())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://desktop") as client:
        for path in ("/sessions", "/sessions/session_a_1", "/sessions/session_a_1/blobs/" + "a" * 64,
                     "/sessions/session_a_1/exports/exp_1/download"):
            response = await client.get(PREFIX + path)
            assert response.status_code == 503, response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {"detail": "Trajectory database is unavailable"}


async def test_the_hint_channel_opens_only_beside_a_redis_cache(monkeypatch):
    """The dev server has JWT_SECRET but no Redis: it must not try to reach one."""
    import cache
    from bus import trajectory_hints
    from cache.redis_cache import RedisCache
    from core.config import OpenBoxConfig

    opened = []

    async def init(url, **kwargs):
        opened.append(url)
        return object()

    monkeypatch.setattr(trajectory_hints, "init_trajectory_hints", init)
    monkeypatch.setattr("core.config.get_config",
                        lambda: OpenBoxConfig(jwt_secret="dev-secret", redis_url="redis://hints.invalid:6379/3"))
    monkeypatch.setattr(cache, "_instance", MemoryCache())
    assert await embedded._open_hints() is False and opened == []
    monkeypatch.setattr(cache, "_instance", RedisCache.__new__(RedisCache))
    assert await embedded._open_hints() is True and opened == ["redis://hints.invalid:6379/3"]
