"""Application database bootstrap is independent of retired job workers."""

from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text

from db import base as db_base


@pytest.mark.asyncio
async def test_single_user_cold_start_creates_only_live_orm_tables(tmp_path, monkeypatch):
    await db_base.close_engine()
    monkeypatch.chdir(tmp_path)
    config = SimpleNamespace(
        jwt_secret="",
        database_url="unused",
        db_pool_size=1,
        db_pool_overflow=0,
    )

    try:
        engine = await db_base.ensure_engine(config)
        async with engine.begin() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            user_id = (
                await connection.execute(
                    text("SELECT id FROM users WHERE id = 'default'")
                )
            ).scalar_one()
            project_id = (
                await connection.execute(
                    text("SELECT id FROM projects WHERE id = 'default'")
                )
            ).scalar_one()

        assert user_id == "default"
        assert project_id == "default"
        assert {"users", "projects", "sessions"} <= tables
        assert not {
            "skill_jobs",
            "skill_job_attempts",
            "skill_job_events",
            "skill_job_inputs",
            "skill_job_artifacts",
            "session_inbox",
            "user_skill_settings",
        } & tables
        assert (tmp_path / ".openbox" / "skill_jobs.db").is_file()
    finally:
        await db_base.close_engine()


@pytest.mark.asyncio
async def test_existing_multi_user_engine_is_reused():
    existing = db_base.get_engine()
    config = SimpleNamespace(
        jwt_secret="configured",
        database_url="postgresql+asyncpg://must-not-connect",
        db_pool_size=99,
        db_pool_overflow=99,
    )

    assert await db_base.ensure_engine(config) is existing
