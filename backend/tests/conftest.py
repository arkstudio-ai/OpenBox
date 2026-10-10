"""Shared test fixtures for all tests."""
import asyncio
import atexit
import os
import shutil
import tempfile

import pytest
from db.base import Base, init_engine, close_engine, get_db_session
from tests.offline_wuying import pytest_runtest_protocol  # noqa: F401

# The spool is the default recording sink. Unless the shell picked a spool
# directory, whatever tests emit lands in a throwaway one, never under
# backend/.openbox where no worker consumes it.
if not os.environ.get("TRAJECTORY_SPOOL_DIR"):
    _TEST_SPOOL_DIR = tempfile.mkdtemp(prefix="openbox-test-spool-")
    os.environ["TRAJECTORY_SPOOL_DIR"] = _TEST_SPOOL_DIR
    atexit.register(shutil.rmtree, _TEST_SPOOL_DIR, ignore_errors=True)

# Recording switches from the shell that started pytest. Importing litellm (in
# its default DEV mode) or main.py loads backend/.env for the rest of the run,
# and TRAJECTORY_RECORDING_ENABLED=true there would turn recording on for tests
# written against the default; they then fail with ownership errors depending
# on which module happened to be imported first.
_SHELL_TRAJECTORY_ENV = {k: v for k, v in os.environ.items() if k.startswith("TRAJECTORY_")}


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


@pytest.fixture(autouse=True)
def query_vector_isolation():
    """No query embedding cached in one test answers the next (each test fakes its own provider)."""
    from memory import retrieval
    retrieval._query_vector_cache.clear()
    yield
    retrieval._query_vector_cache.clear()


@pytest.fixture(autouse=True)
def event_fold_isolation():
    """No fold cached in one test is offered to the next.

    With OPENBOX_VERIFY_EVENT_FOLD=1 every fold a test loads is also compared
    with a full replay by the frozen projectors (tests/unit/event_fold_oracle).
    """
    from session import agent_event_log
    agent_event_log.clear_event_fold_cache()
    previous = agent_event_log._FOLD_VERIFIER, agent_event_log.FOLD_CACHE_MIN_EVENTS
    if os.environ.get("OPENBOX_VERIFY_EVENT_FOLD") == "1":
        from tests.unit.event_fold_oracle import verify_fold_against_oracle
        agent_event_log._FOLD_VERIFIER = verify_fold_against_oracle
        # Cache every Session, so later loads take the incremental path.
        agent_event_log.FOLD_CACHE_MIN_EVENTS = 1
    yield
    agent_event_log._FOLD_VERIFIER, agent_event_log.FOLD_CACHE_MIN_EVENTS = previous
    agent_event_log.clear_event_fold_cache()


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
                 "resolutions": ["480p", "512p", "768p", "2k"], "ratios": ["9:16", "16:9"], "duration_range": [4, 30]},
                {"id": "wan3.0-video", "channel": "sd2", "wire_shape": "metadata",
                 "resolutions": ["480p", "720p", "1080p"], "duration_range": [2, 30]},
                {"id": "doubao-seedance-2-0-260128", "channel": "sd2", "wire_shape": "metadata",
                 "resolutions": ["720p", "1080p"], "duration_range": [4, 15]},
            ]},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    return config
