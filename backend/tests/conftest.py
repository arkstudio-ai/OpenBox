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


@pytest.fixture
async def db_session():
    """Get a test database session."""
    async with get_db_session() as session:
        yield session


@pytest.fixture
def video_gateway_config(monkeypatch):
    """Portable video protocol contracts, independent of private openbox.json."""
    from core.config import OpenBoxConfig

    config = OpenBoxConfig.model_validate({
        "provider": {"test": {"api_key": "test-only", "base_url": "https://video.invalid"}},
        "video_generation": {"provider": "test", "channel_providers": {"sd2": "test", "task": "test"},
            "models": [
                {"id": "MiniMax-H3", "channel": "sd2", "wire_shape": "size",
                 "resolutions": ["480p", "512p", "768p", "2k"], "ratios": ["9:16", "16:9"], "duration_range": [4, 15]},
                {"id": "wan3.0-video", "channel": "sd2", "wire_shape": "metadata",
                 "resolutions": ["480p", "720p", "1080p"], "duration_range": [2, 30]},
                {"id": "doubao-seedance-2-0-260128", "channel": "sd2", "wire_shape": "metadata",
                 "resolutions": ["720p", "1080p"], "duration_range": [4, 15]},
            ]},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    return config
