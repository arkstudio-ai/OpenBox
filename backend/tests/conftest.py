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
def _runtime_flags_are_test_owned(monkeypatch):
    """Pin the SkillJob flags to the repo defaults for every test.

    get_config() reads the operator's local openbox.json, so without this the
    suite silently inherits deployment decisions: the day the operator disabled
    the durable runtime (2026-08-30), 33 tests failed that had never declared
    a dependency on it being enabled. Tests own these flags — the shipped
    defaults here, explicit monkeypatch in tests that assert a variation.
    """
    from core.config import get_config

    config = get_config()
    monkeypatch.setattr(config, "skill_jobs_enabled", True)
    monkeypatch.setattr(config, "skill_jobs_video_write", False)


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


@pytest.fixture
async def db_session():
    """Get a test database session."""
    async with get_db_session() as session:
        yield session
