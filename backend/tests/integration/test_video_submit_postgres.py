"""The submission guard on real JSONB, restricted to a disposable local DB."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db.base as base
from tests.unit.test_video_submit_regression import (
    test_rejection_blocks_new_keys_in_same_run_but_not_the_next_run,
    test_rejection_guard_is_scoped_to_user_session_model_and_route,
    test_real_http_client_submits_size_and_reuses_accepted_task,
)

pytestmark = pytest.mark.usefixtures("video_postgres")


@pytest.fixture
async def video_postgres():
    url = os.environ.get("VIDEO_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("VIDEO_TEST_POSTGRES_URL is not configured")
    assert make_url(url).host in {"127.0.0.1", "localhost"}
    schema = "video_submit_test_" + uuid4().hex
    admin = create_async_engine(url)
    previous_engine, previous_factory = base._engine, base._session_factory
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(base.Base.metadata.create_all)
        base._engine = engine
        base._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        yield
    finally:
        base._engine, base._session_factory = previous_engine, previous_factory
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
