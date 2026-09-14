"""Shared test fixtures for all tests."""
import asyncio
import os
import pytest
from db.base import Base, init_engine, close_engine, get_db_session

# Recording switches from the shell that started pytest. Importing litellm (in
# its default DEV mode) or main.py loads backend/.env for the rest of the run,
# and TRAJECTORY_RECORDING_ENABLED=true there would turn recording on for tests
# written against the default; they then fail with ownership errors depending
# on which module happened to be imported first.
_SHELL_TRAJECTORY_ENV = {k: v for k, v in os.environ.items() if k.startswith("TRAJECTORY_")}


def pytest_collection_modifyitems(config, items):
    """Skip the legacy trajectory tests listed in tests/legacy_trajectory_quarantine.py."""
    from tests.legacy_trajectory_quarantine import QUARANTINE
    for item in items:
        reason = QUARANTINE.get(item.nodeid)
        if reason:
            item.add_marker(pytest.mark.skip(reason=f"legacy trajectory quarantine: {reason}"))


@pytest.fixture(autouse=True)
def trajectory_env_from_shell():
    """Tests see only the shell's TRAJECTORY_* values; a test that records sets its own.

    Plain os.environ, not monkeypatch: requesting monkeypatch here would set it
    up before every test's own fixtures and so undo its patches after their
    teardowns, which then run against whatever the test had patched in.
    """
    for key in [k for k in os.environ if k.startswith("TRAJECTORY_") and k not in _SHELL_TRAJECTORY_ENV]:
        del os.environ[key]
    os.environ.update(_SHELL_TRAJECTORY_ENV)


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
