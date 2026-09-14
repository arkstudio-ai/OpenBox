"""Worker app: /health and /metrics formats (NOTES decision 5), lifespan wiring and read-only serving."""
import shutil
import sys
import types

import httpx
import pytest
from sqlalchemy import create_engine, text

import auth.middleware as middleware
import auth.ticket as tickets
import trajectory.auth as trajectory_auth
from core.config import OpenBoxConfig
from tests.unit.test_worker_app_harness import (PREFIX, FakeServices, ReadLayer, admin_env, auth_stores,  # noqa: F401
    business_db, internal_backend, seed_trace, token, trace_url, worker)
from trajectory.ops import cms
from trajectory.storage import LocalBlobStore, MemoryBlobStore, set_blob_store
from trajectory.store.database import TraceBase, get_trace_engine
from trajectory.worker.app import create_app, migration_heads, schema_current
from trajectory.worker.metrics import COUNTERS, GAUGES, get_metrics, reset_metrics_for_tests

HEALTH_KEYS = {"status", "writer", "db", "spool", "blob_store", "version"}


async def test_health_reports_every_component(worker):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://worker") as probe:
        response = await probe.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "writer": True, "db": True, "spool": True, "blob_store": True,
                               "version": "0.1.0"}
    assert response.headers["cache-control"] == "no-store"


async def test_health_degrades_but_keeps_serving_reads(worker):
    worker.services.is_writer = False
    body = (await worker.client.get("/health")).json()
    assert body["status"] == "degraded" and body["writer"] is False and body["db"] is True
    assert (await worker.client.get(PREFIX + "/sessions")).status_code == 200  # a read-only worker still serves
    worker.services.is_writer = True
    worker.blob.fail("exists")
    response = await worker.client.get("/health")
    assert response.status_code == 200 and response.json()["blob_store"] is False
    assert response.json()["status"] == "degraded"
    worker.blob.clear_faults()
    shutil.rmtree(worker.spool)
    response = await worker.client.get("/health")
    assert response.status_code == 200 and response.json()["spool"] is False
    async with get_trace_engine().begin() as connection:
        await connection.execute(text("DROP TABLE trajectory_audit_outbox"))
    response = await worker.client.get("/health")
    assert response.status_code == 503 and set(response.json()) == HEALTH_KEYS
    assert response.json()["db"] is False and response.json()["status"] == "degraded"


def test_schema_check_wants_the_alembic_head_or_a_complete_create_all(tmp_path, monkeypatch):
    modeled = create_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    TraceBase.metadata.create_all(modeled)
    with modeled.connect() as connection:
        assert schema_current(connection) is True
    with modeled.begin() as connection:
        connection.execute(text("DROP TABLE trajectory_gc_queue"))
    with modeled.connect() as connection:
        assert schema_current(connection) is False
    modeled.dispose()

    from alembic import command
    from alembic.config import Config
    from trajectory.worker.app import MIGRATIONS_DIR
    path = tmp_path / "migrated.db"
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    command.upgrade(config, "head")
    migrated = create_engine(f"sqlite:///{path}")
    with migrated.connect() as connection:
        assert schema_current(connection) is True
        assert migration_heads() == {connection.execute(text("SELECT version_num FROM trajectory_alembic_version")).scalar()}
    with migrated.begin() as connection:
        connection.execute(text("UPDATE trajectory_alembic_version SET version_num = 't0000_older'"))
    with migrated.connect() as connection:
        assert schema_current(connection) is False
    migrated.dispose()


async def test_metrics_lists_every_spec_metric(worker):
    reset_metrics_for_tests()
    get_metrics().inc("gaps_recorded", 2)
    get_metrics().set_gauge("projection_lag_events", 17)
    response = await worker.client.get("/metrics")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"counters", "gauges", "uptime_seconds"}
    assert set(body["counters"]) == set(COUNTERS) and set(body["gauges"]) == set(GAUGES)
    assert body["counters"]["gaps_recorded"] == 2 and body["gauges"]["projection_lag_events"] == 17
    assert isinstance(body["uptime_seconds"], float) and body["uptime_seconds"] >= 0
    numbers = cms._numbers(body)
    for name in ("producer_loss_events", "blob_put_failures", "gaps_recorded"):  # read by the gw2 drills
        assert name in numbers


async def test_lifespan_owns_services_backend_and_engine(trace_url, internal_backend, business_db, auth_stores,
                                                         admin_env, tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path))
    blob = MemoryBlobStore()
    ReadLayer(blob).install(monkeypatch)
    services = FakeServices(writer=False)
    built = []
    http_backend = internal_backend.client()

    def factory(store):
        built.append(store)
        return services

    app = create_app(database_url=trace_url, blob_store=blob, services_factory=factory, backend=http_backend,
                     cache=auth_stores)
    async with app.router.lifespan_context(app):
        assert built == [blob] and (services.started, services.stopped) == (1, 0)
        assert trajectory_auth.get_backend() is http_backend
        await seed_trace(blob)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker",
                                     headers={"Authorization": f"Bearer {token()}"}) as client:
            assert (await client.get(PREFIX + "/sessions")).status_code == 200
            assert (await client.get("/health")).json()["writer"] is False
    assert (services.started, services.stopped) == (1, 1)
    with pytest.raises(RuntimeError):
        get_trace_engine()
    assert trajectory_auth._backend is None and app.state.worker is None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        assert (await client.get("/health")).status_code == 503
    await http_backend.close()


async def test_lifespan_requires_the_trace_database_url(monkeypatch):
    monkeypatch.delenv("TRAJECTORY_DATABASE_URL", raising=False)
    app = create_app()
    with pytest.raises(RuntimeError, match="TRAJECTORY_DATABASE_URL"):
        async with app.router.lifespan_context(app):
            pass


async def test_default_components_come_from_the_environment(trace_url, tmp_path, monkeypatch):
    created = []
    settings_module = types.ModuleType("trajectory.worker.settings")
    settings_module.get_worker_settings = lambda: "worker-settings"
    services_module = types.ModuleType("trajectory.worker.services")

    class WorkerServices(FakeServices):
        def __init__(self, settings, *, blob_store=None, spool_dir=None):
            super().__init__(blob_store)
            created.append((settings, blob_store, spool_dir))

    services_module.WorkerServices = WorkerServices
    monkeypatch.setitem(sys.modules, "trajectory.worker.settings", settings_module)
    monkeypatch.setitem(sys.modules, "trajectory.worker.services", services_module)
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret="", internal_api_token="tok"))
    monkeypatch.setattr(tickets, "_cache", tickets._cache)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", trace_url)
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "local")
    monkeypatch.setenv("TRAJECTORY_BLOB_LOCAL_PATH", str(tmp_path / "blobs"))
    monkeypatch.delenv("TRAJECTORY_BACKEND_INTERNAL_URL", raising=False)
    set_blob_store(None)
    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            (settings, store, spool_dir), = created
            assert settings == "worker-settings" and isinstance(store, LocalBlobStore) and spool_dir is None
            backend = trajectory_auth.get_backend()
            assert isinstance(backend, trajectory_auth.HttpBackend)
            assert str(backend._client.base_url) == "http://backend:8080"
            assert tickets._cache is not None and middleware.is_auth_enabled() in {True, False}
            assert app.state.worker.services.started == 1
        assert app.state.worker is None and trajectory_auth._backend is None
    finally:
        set_blob_store(None)


def test_server_mode_shares_redis_tickets_and_the_token_blacklist(monkeypatch):
    import auth.jwt as jwt_module
    from cache.redis_cache import RedisCache
    from trajectory.worker import app as worker_app
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret="server-secret",
                                                                        redis_url="redis://redis.invalid:6379/0"))
    for module, name in ((jwt_module, "_secret"), (middleware, "_cache"), (middleware, "_auth_enabled"),
                         (tickets, "_cache")):
        monkeypatch.setattr(module, name, getattr(module, name))
    cache, distributed = worker_app._auth_stores()
    assert distributed is True and isinstance(cache, RedisCache)
    assert tickets._cache is cache and middleware._cache is cache and middleware.is_auth_enabled()
    assert jwt_module._secret == "server-secret"
