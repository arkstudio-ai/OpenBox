"""Backend lifespan wiring of trajectory recording (SPEC §10 main.py, NOTES decision 8).

The business app starts the spool emitter, metadata sync and, in embedded mode,
the in-process worker, and stops them again. In external mode it neither
imports the trace store or the worker nor serves the admin trajectory routes.
"""
import importlib.abc
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

import main
from core.config import OpenBoxConfig
from trajectory import spool
from trajectory.emitter import get_emitter, reset_emitter_for_tests

BACKEND = Path(__file__).resolve().parents[2]
ADMIN_PREFIXES = ("/api/admin/trajectories", "/ws/admin/trajectories")
GUARDED = ("trajectory.store", "trajectory.worker")


def admin_routes(app) -> list[str]:
    return [route.path for route in app.routes if isinstance(route, (APIRoute, APIWebSocketRoute))
            and route.path.startswith(ADMIN_PREFIXES)]


def guarded(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in GUARDED)


class Service:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    async def stop(self):
        pass


class ImportGuard(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.blocked: list[str] = []

    def find_spec(self, name, path, target=None):
        if guarded(name):
            self.blocked.append(name)
            raise ImportError(f"the business app imported {name}")
        return None


@pytest.fixture
def quiet_backend(monkeypatch):
    """Every lifespan subsystem other than trajectory recording, reduced to no-ops."""
    import sandbox
    from cron.service import cron_service
    from notifications import announcements, providers, runtime
    from question.continuation import question_worker
    from sandbox.desktop_activation import desktop_activation_service
    from sandbox.manager import sandbox_manager
    monkeypatch.setattr(main, "_init_infrastructure", lambda config: None)
    monkeypatch.setattr(main, "_init_agent", lambda: None)
    monkeypatch.setattr(main, "_cleanup_infrastructure", AsyncMock())
    monkeypatch.setattr(sandbox, "provider", types.SimpleNamespace(reconcile=AsyncMock()))
    monkeypatch.setattr(cron_service, "start", AsyncMock())
    monkeypatch.setattr(cron_service, "stop", AsyncMock())
    monkeypatch.setattr("cron.internal_tasks.register_builtin_tasks", lambda: None)
    monkeypatch.setattr(desktop_activation_service, "start", lambda: None)
    monkeypatch.setattr(desktop_activation_service, "stop", AsyncMock())
    monkeypatch.setattr("question.legacy.reconcile_legacy_questions", AsyncMock(return_value=0))
    monkeypatch.setattr(question_worker, "start", lambda: None)
    monkeypatch.setattr(question_worker, "stop", AsyncMock())
    monkeypatch.setattr(providers.PushProviders, "from_env", classmethod(lambda cls: MagicMock()))
    monkeypatch.setattr(runtime, "PushWorker", Service)
    monkeypatch.setattr(announcements, "InboxJanitor", Service)
    monkeypatch.setattr(sandbox_manager, "release_all", AsyncMock())
    monkeypatch.setattr("bus.bus.init_redis_bus", AsyncMock())
    monkeypatch.setattr("bus.bus.close_redis_bus", AsyncMock())
    monkeypatch.setattr("video.job_recovery.schedule_startup_recovery", lambda: None)


@pytest.fixture
def recording(monkeypatch, tmp_path):
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.delenv("TRAJECTORY_WORKER_MODE", raising=False)
    yield tmp_path / "spool"
    reset_emitter_for_tests()


@pytest.fixture
def events(monkeypatch) -> list[str]:
    """``trajectory.meta_sync`` as its interface defines it, recording calls."""
    calls: list[str] = []
    module = types.ModuleType("trajectory.meta_sync")

    def start_meta_sync():
        calls.append("meta_sync.start")
        return None

    async def stop_meta_sync():
        calls.append("meta_sync.stop")

    module.start_meta_sync, module.stop_meta_sync = start_meta_sync, stop_meta_sync
    monkeypatch.setitem(sys.modules, "trajectory.meta_sync", module)
    return calls


def test_worker_mode_defaults_to_external_with_jwt_and_embedded_without(monkeypatch):
    monkeypatch.delenv("TRAJECTORY_WORKER_MODE", raising=False)
    assert main._trajectory_worker_mode(OpenBoxConfig(jwt_secret="set")) == "external"
    assert main._trajectory_worker_mode(OpenBoxConfig(jwt_secret="")) == "embedded"
    for raw, expected in ((" Embedded ", "embedded"), ("OFF", "off"), ("external", "external"),
                          ("sidecar", "external"), ("", "external")):
        monkeypatch.setenv("TRAJECTORY_WORKER_MODE", raw)
        assert main._trajectory_worker_mode(OpenBoxConfig(jwt_secret="set")) == expected


@pytest.mark.parametrize("secret,mode,mounted", [
    ("", None, True), ("set", None, False), ("set", "embedded", True), ("", "off", False), ("", "external", False),
])
def test_admin_trajectory_routes_are_mounted_only_in_embedded_mode(monkeypatch, secret, mode, mounted):
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret=secret))
    if mode is None:
        monkeypatch.delenv("TRAJECTORY_WORKER_MODE", raising=False)
    else:
        monkeypatch.setenv("TRAJECTORY_WORKER_MODE", mode)
    app = main.create_app()
    assert len(admin_routes(app)) == (14 if mounted else 0)
    assert app.state.trajectory_worker_mode == (mode or ("external" if secret else "embedded"))


@pytest.mark.parametrize("failure", ["raises", "missing"])
def test_admin_routes_that_cannot_be_mounted_never_keep_the_business_app_from_building(monkeypatch, failure):
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret=""))
    monkeypatch.delenv("TRAJECTORY_WORKER_MODE", raising=False)
    if failure == "missing":
        monkeypatch.setitem(sys.modules, "trajectory.worker.embedded", None)
    else:
        from trajectory.worker import embedded
        monkeypatch.setattr(embedded, "mount_admin_routers", MagicMock(side_effect=ImportError("zstandard")))
    app = main.create_app()
    assert app.state.trajectory_worker_mode == "embedded" and admin_routes(app) == []
    assert "/api/internal/trajectory/viewer" in {route.path for route in app.routes}


async def test_embedded_lifespan_runs_emitter_meta_sync_and_worker(quiet_backend, recording, events, monkeypatch):
    from trajectory.worker import embedded
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret=""))

    async def start_worker(app):
        assert get_emitter() is not None
        events.append("worker.start")

    async def stop_worker():
        events.append("worker.stop")

    monkeypatch.setattr(embedded, "start_embedded_worker", start_worker)
    monkeypatch.setattr(embedded, "stop_embedded_worker", stop_worker)
    removed = AsyncMock(side_effect=AssertionError("no longer part of the backend lifespan"))
    monkeypatch.setattr("trajectory.payload.start_archive_worker", removed, raising=False)
    monkeypatch.setattr("trajectory.export.resume_exports", removed, raising=False)
    app = main.create_app()
    assert len(admin_routes(app)) == 14
    async with main.lifespan(app):
        emitter = get_emitter()
        assert emitter.stats()["state"] == "running" and emitter.producer_dir.is_dir()
        assert events == ["meta_sync.start", "worker.start"]
    assert events == ["meta_sync.start", "worker.start", "meta_sync.stop", "worker.stop"]
    assert emitter.stats()["state"] == "closed"
    lines = [spool.decode_line(line) for path in sorted(emitter.producer_dir.glob("*.jsonl"))
             for line in path.read_bytes().splitlines()]
    assert lines and lines[-1]["k"] == "control" and lines[-1]["control"]["type"] == "producer.goodbye"
    removed.assert_not_called()


async def test_off_mode_runs_no_emitter_meta_sync_or_worker(quiet_backend, recording, events, monkeypatch):
    from trajectory.worker import embedded
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret=""))
    monkeypatch.setenv("TRAJECTORY_WORKER_MODE", "off")
    monkeypatch.setattr(embedded, "start_embedded_worker", AsyncMock(side_effect=AssertionError("worker")))
    app = main.create_app()
    assert app.state.trajectory_worker_mode == "off" and admin_routes(app) == []
    async with main.lifespan(app):
        assert get_emitter() is None and "meta_sync.start" not in events
    assert not recording.exists()


async def test_emitter_starts_before_serving_and_closes_with_five_seconds(quiet_backend, events, monkeypatch):
    calls = []

    class Emitter:
        def close(self, timeout):
            calls.append(("close", timeout))

    def get(**kwargs):
        calls.append("get")
        return fake

    fake = Emitter()
    monkeypatch.setattr("trajectory.emitter.get_emitter", get)
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret="set"))
    monkeypatch.delenv("TRAJECTORY_WORKER_MODE", raising=False)
    app = main.create_app()
    async with main.lifespan(app):
        assert calls == ["get"]
    assert calls == ["get", "get", ("close", 5.0)]


async def test_external_lifespan_never_imports_the_trace_store_or_worker(quiet_backend, recording, events,
                                                                         monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret="external-secret"))
    for name in [name for name in sys.modules if guarded(name)]:
        monkeypatch.delitem(sys.modules, name)
    guard = ImportGuard()
    sys.meta_path.insert(0, guard)
    try:
        app = main.create_app()
        async with main.lifespan(app):
            assert get_emitter() is not None
    finally:
        sys.meta_path.remove(guard)
    assert guard.blocked == []
    assert admin_routes(app) == [] and app.state.trajectory_worker_mode == "external"
    assert events == ["meta_sync.start", "meta_sync.stop"]


@pytest.mark.parametrize("failure", ["raises", "missing"])
async def test_trajectory_failures_never_keep_the_backend_from_starting_or_stopping(quiet_backend, recording,
                                                                                  monkeypatch, failure):
    from trajectory.worker import embedded
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(jwt_secret=""))
    if failure == "missing":
        monkeypatch.setitem(sys.modules, "trajectory.meta_sync", None)
    else:
        module = types.ModuleType("trajectory.meta_sync")

        def start_meta_sync():
            raise RuntimeError("business database unavailable")

        async def stop_meta_sync():
            raise RuntimeError("still unavailable")

        module.start_meta_sync, module.stop_meta_sync = start_meta_sync, stop_meta_sync
        monkeypatch.setitem(sys.modules, "trajectory.meta_sync", module)
    monkeypatch.setattr(embedded, "start_embedded_worker", AsyncMock(side_effect=OSError("disk full")))
    monkeypatch.setattr(embedded, "stop_embedded_worker", AsyncMock(side_effect=OSError("disk full")))
    app = main.create_app()
    async with main.lifespan(app):
        assert get_emitter() is not None
    main._cleanup_infrastructure.assert_awaited_once()
    embedded.stop_embedded_worker.assert_awaited_once()


@pytest.mark.parametrize("mode", ["external", "embedded"])
def test_fresh_business_app_loads_the_worker_only_in_embedded_mode(mode, tmp_path):
    script = (
        "import json, sys\n"
        "import main\n"
        "app = main.create_app()\n"
        "loaded = sorted(name for name in sys.modules if name.split('.')[:2] in (['trajectory', 'store'], ['trajectory', 'worker']))\n"
        "paths = sorted({route.path for route in app.routes if route.path.startswith(('/api/admin/trajectories', '/ws/admin/trajectories'))})\n"
        "print(json.dumps({'modules': loaded, 'routes': paths}))\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("TRAJECTORY_")}
    env.update(JWT_SECRET="assembly-test-secret", TRAJECTORY_WORKER_MODE=mode,
               DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'business.db'}")
    result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=env, capture_output=True, text=True,
                            timeout=180)
    assert result.returncode == 0, result.stderr[-3000:]
    body = json.loads(result.stdout.strip().splitlines()[-1])
    if mode == "external":
        assert body == {"modules": [], "routes": []}
    else:
        assert {"trajectory.worker.routes", "trajectory.worker.ws", "trajectory.store.database"} <= set(body["modules"])
        assert len(body["routes"]) == 14
