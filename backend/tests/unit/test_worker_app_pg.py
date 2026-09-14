"""The worker service on PostgreSQL: alembic head checks, embedded migrations, audit claims and the route matrix.

Skipped unless TRAJECTORY_TRACE_TEST_DATABASE_URL names a disposable database
whose name starts with ``openbox_trace_test_``. Every test starts from an empty
``public`` schema. The imported SQLite tests run again here against PostgreSQL
through the ``trace_url`` and ``trace_engine`` overrides below.
"""
import asyncio
import os

import pytest
from fastapi import FastAPI
from sqlalchemy import pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import trajectory.auth as trajectory_auth
from tests.unit.test_trajectory_auth_audit import (  # noqa: F401
    test_a_refused_entry_does_not_hold_back_its_batch, test_concurrent_deliverers_never_send_a_row_twice,
    test_delivery_writes_business_audit_logs_and_empties_the_outbox)
from tests.unit.test_worker_app_harness import (FakeServices, admin_env, auth_stores, business_db,  # noqa: F401
    internal_backend, worker)
from tests.unit.test_worker_routes import (  # noqa: F401
    test_blob_endpoint_returns_the_ref_value_at_the_watermark, test_events_records_checkpoint_and_search_parameters,
    test_export_create_status_and_download_contract, test_exports_are_invalidated_by_later_content_deletion_or_expiry,
    test_payload_download_is_audited_and_revalidated_after_the_read)
from tests.unit.test_worker_routes_read_races import test_download_rechecks_during_blob_read  # noqa: F401
from tests.unit.test_worker_ws import test_read_only_protocol_and_direct_watermarks  # noqa: F401
from trajectory.storage import MemoryBlobStore
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine
from trajectory.worker import embedded
from trajectory.worker.app import check_schema, database_ok
from trajectory.worker.metrics import trace_db_bytes

URL = os.environ.get("TRAJECTORY_TRACE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")


async def reset_schema(*, create: bool) -> None:
    assert make_url(URL).database.startswith("openbox_trace_test_"), "only a disposable trace test database"
    engine = create_async_engine(URL, poolclass=pool.NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
            if create:
                await connection.run_sync(TraceBase.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture
async def trace_url():
    await reset_schema(create=True)
    yield URL
    await reset_schema(create=False)


@pytest.fixture
async def trace_engine(trace_url):
    engine = init_trace_engine(trace_url)
    yield engine
    await close_trace_engine()


@pytest.fixture
def migrations_env(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", URL)
    monkeypatch.delenv("DATABASE_URL", raising=False)


async def test_trace_db_size_is_the_postgresql_database_size(trace_engine):
    async with trace_engine.connect() as connection:
        reported = await connection.scalar(text("SELECT pg_database_size(current_database())"))
    size = await trace_db_bytes(trace_engine)
    assert isinstance(size, int) and size > 0
    assert abs(size - reported) < 1024 * 1024  # the same measure, a moment apart


async def test_embedded_worker_migrates_postgresql_outside_the_event_loop(migrations_env, monkeypatch):
    await reset_schema(create=False)
    monkeypatch.setattr(embedded, "_embedded", None)
    monkeypatch.setattr(trajectory_auth, "_backend", None)
    services = FakeServices()
    app = FastAPI()
    try:
        await embedded.start_embedded_worker(app, blob_store=MemoryBlobStore(), services_factory=lambda store: services)
        try:
            assert services.started == 1
            assert await database_ok() is True
        finally:
            await embedded.stop_embedded_worker()
        assert await check_schema(URL) is True
    finally:
        await reset_schema(create=False)


async def test_schema_check_follows_the_postgresql_version_table(migrations_env):
    await reset_schema(create=False)
    try:
        assert await check_schema(URL) is False
        await asyncio.to_thread(embedded._upgrade_head, URL)
        assert await check_schema(URL) is True
        engine = create_async_engine(URL, poolclass=pool.NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE trajectory_alembic_version SET version_num = 't0000_older'"))
        finally:
            await engine.dispose()
        assert await check_schema(URL) is False
    finally:
        await reset_schema(create=False)
