"""Shared test fixtures for all tests."""
import asyncio
import atexit
import os
import shutil
import tempfile
from types import SimpleNamespace

# Hermetic configuration, before anything below imports core.config, litellm or main.
# A test sees only the config it sets up: no developer's private backend/openbox.json
# (the working directory's project file), ~/.config/openbox/openbox.json, OPENBOX_CONFIG
# or inline OPENBOX_CONFIG_CONTENT, and no backend/.env, which main.py loads on import
# and litellm in its DEV mode (find_dotenv from site-packages up to the checkout).
# Subprocesses a test starts inherit all of it. A test that needs file-based config
# uses the ``config_files`` fixture (or sets OPENBOX_CONFIG itself).
for _name in ("OPENBOX_CONFIG", "OPENAGENT_CONFIG", "OPENBOX_CONFIG_CONTENT", "OPENAGENT_CONFIG_CONTENT"):
    os.environ.pop(_name, None)
os.environ["OPENBOX_CONFIG_DISCOVERY"] = "off"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LITELLM_MODE"] = "PRODUCTION"

import pytest  # noqa: E402
from db.base import Base, init_engine, close_engine, get_db_session  # noqa: E402
from tests.offline_wuying import pytest_runtest_protocol  # noqa: E402,F401

# The spool is the default recording sink. Unless the shell picked a spool
# directory, whatever tests emit lands in a throwaway one, never under
# backend/.openbox where no worker consumes it.
if not os.environ.get("TRAJECTORY_SPOOL_DIR"):
    _TEST_SPOOL_DIR = tempfile.mkdtemp(prefix="openbox-test-spool-")
    os.environ["TRAJECTORY_SPOOL_DIR"] = _TEST_SPOOL_DIR
    atexit.register(shutil.rmtree, _TEST_SPOOL_DIR, ignore_errors=True)

# Recording switches from the shell that started pytest. Before .env loading was
# switched off above, importing litellm or main.py loaded backend/.env for the rest
# of the run, and TRAJECTORY_RECORDING_ENABLED=true there turned recording on for
# tests written against the default; a test that sets one must not leak it either.
_SHELL_TRAJECTORY_ENV = {k: v for k, v in os.environ.items() if k.startswith("TRAJECTORY_")}


def _production_kernel_extensions():
    """The plugins main.py registers; a test that wants none uses ``agent.extensions.use``."""
    from agent.extensions import KernelExtensions
    from memory.plugin import MemoryCleanup, MemoryPlugin
    from trajectory.plugin import TrajectoryPlugin
    return KernelExtensions().add(TrajectoryPlugin()).add(MemoryCleanup()).add(MemoryPlugin())


@pytest.fixture(autouse=True)
def kernel_extensions():
    """Every test starts with the production plugin set installed and leaves it installed."""
    from agent.extensions import use
    with use(_production_kernel_extensions()) as registry:
        yield registry


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
def config_files(tmp_path, monkeypatch):
    """Opt in to file-based config, found in this test's own directories, never the developer's.

    Discovery of the global and project files is on again: ``global_file`` is
    ``$XDG_CONFIG_HOME/openbox/openbox.json`` and the working directory is ``project_dir``,
    whose ``openbox.json`` is the project file. The test writes whichever it needs (and may
    set OPENBOX_CONFIG or OPENBOX_CONFIG_CONTENT too); the cached config is dropped, so the
    next ``get_config()`` reads them, and restored afterwards.
    """
    import core.config
    home, project = tmp_path / "xdg-config", tmp_path / "project"
    (home / "openbox").mkdir(parents=True)
    project.mkdir()
    monkeypatch.delenv("OPENBOX_CONFIG_DISCOVERY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(core.config, "_config", None)
    return SimpleNamespace(global_file=home / "openbox" / "openbox.json", project_dir=project,
                           project_file=project / "openbox.json")


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
