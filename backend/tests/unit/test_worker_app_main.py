"""``python -m trajectory.worker``: schema gate, uvicorn on TRAJECTORY_WORKER_PORT, 20 s WebSocket pings."""
import runpy

import pytest
from fastapi import FastAPI

import trajectory.worker.__main__ as entry
from trajectory.worker import app as worker_app


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(entry, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", "sqlite+aiosqlite:///worker.db")
    for key in ("TRAJECTORY_WORKER_HOST", "TRAJECTORY_WORKER_PORT"):
        monkeypatch.delenv(key, raising=False)
    return calls


async def current(url):
    return True


def test_serves_the_worker_app_with_20_second_pings(served, monkeypatch):
    checked = []

    async def check(url):
        checked.append(url)
        return True

    monkeypatch.setattr(worker_app, "check_schema", check)
    assert entry.main() == 0
    (app, kwargs), = served
    assert checked == ["sqlite+aiosqlite:///worker.db"]
    assert isinstance(app, FastAPI) and {"/health", "/metrics", "/ws/admin/trajectories"} <= {
        route.path for route in app.routes}
    assert kwargs == {"host": "0.0.0.0", "port": 8090, "ws_ping_interval": 20.0, "ws_ping_timeout": 20.0,
                      "lifespan": "on"}


@pytest.mark.parametrize("host,port,expected", [("127.0.0.1", "9123", ("127.0.0.1", 9123)),
                                                ("", "not-a-port", ("0.0.0.0", 8090)),
                                                ("  ", "0", ("0.0.0.0", 8090))])
def test_host_and_port_come_from_the_environment(served, monkeypatch, host, port, expected):
    monkeypatch.setattr(worker_app, "check_schema", current)
    monkeypatch.setenv("TRAJECTORY_WORKER_HOST", host)
    monkeypatch.setenv("TRAJECTORY_WORKER_PORT", port)
    assert entry.main() == 0
    (_, kwargs), = served
    assert (kwargs["host"], kwargs["port"]) == expected


def test_refuses_to_serve_without_a_current_trace_schema(served, monkeypatch, capsys):
    monkeypatch.delenv("TRAJECTORY_DATABASE_URL")
    assert entry.main() == 2
    assert "TRAJECTORY_DATABASE_URL is not set" in capsys.readouterr().err

    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", "sqlite+aiosqlite:///worker.db")

    async def behind(url):
        return False

    async def unreachable(url):
        raise OSError("connection refused")

    monkeypatch.setattr(worker_app, "check_schema", behind)
    assert entry.main() == 3
    assert "alembic -c alembic_trajectory.ini upgrade head" in capsys.readouterr().err
    monkeypatch.setattr(worker_app, "check_schema", unreachable)
    assert entry.main() == 3
    assert "OSError" in capsys.readouterr().err
    assert served == []


def test_module_entry_point_exits_with_the_main_status(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("TRAJECTORY_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("trajectory.worker", run_name="__main__")
    assert exited.value.code == 2
