"""Embedded worker in the backend process: SQLite create_all (never alembic on the loop), services and routers."""
import asyncio
import threading
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from sqlalchemy import create_engine, inspect

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
