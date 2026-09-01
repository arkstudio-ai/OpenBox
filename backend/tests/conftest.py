"""Shared test fixtures for all tests."""
import asyncio
import pytest
from db.base import Base, init_engine, close_engine, get_db_session


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def ensure_test_db():
    """Ensure a test database engine exists for every test.

    Re-initializes if the engine was closed (e.g., by integration test teardown).
    """
    from db.base import _engine
    if _engine is None:
        engine = init_engine("sqlite+aiosqlite:///:memory:")
        import db.models  # noqa: F401
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


@pytest.fixture(autouse=True)
async def isolate_cron_claims(request, ensure_test_db):
    """Keep durable Cron claims from leaking between independent Cron tests.

    The production quota intentionally counts every live row in the database.
    Older lease tests predated that invariant and sometimes left a valid claim
    behind for the next test, which is shared-state pollution rather than a
    production scenario. Limit cleanup to Cron modules so unrelated durability
    tests retain their exact fixtures.
    """
    if not request.node.path.name.startswith("test_cron_"):
        yield
        return

    from db.models.cron import CronJob
    from sqlalchemy import update

    async def clear_claims():
        async with get_db_session() as session:
            await session.execute(
                update(CronJob).values(
                    running_at=None,
                    run_token=None,
                    run_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            )

    await clear_claims()
    try:
        yield
    finally:
        await clear_claims()


@pytest.fixture
async def db_session():
    """Get a test database session."""
    async with get_db_session() as session:
        yield session
