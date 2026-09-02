"""Only an authenticated deployment migrates PostgreSQL at launch.

Desktop/single-user mode keeps everything in ``.openbox/skill_jobs.db``. It
still inherits a ``DATABASE_URL`` — from ``backend/.env``, or from the
entrypoint's own default — that points at a database it never opens. Running
Alembic against that database is not a harmless extra step: when the local
PostgreSQL sits on a revision this checkout does not carry, ``upgrade head``
raises ``Can't locate revision`` and the server never starts at all.
"""
import importlib.util
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "backend_entrypoint", BACKEND_DIR / "scripts" / "backend_entrypoint.py"
)
entrypoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entrypoint)


class _Config:
    def __init__(self, jwt_secret: str) -> None:
        self.jwt_secret = jwt_secret


@pytest.fixture
def config(monkeypatch):
    """Serve one jwt_secret through the same accessor the application uses."""
    from core import config as config_module

    def _install(jwt_secret: str):
        monkeypatch.setattr(
            config_module, "get_config", lambda: _Config(jwt_secret)
        )

    return _install


def test_single_user_mode_does_not_touch_postgresql(config):
    config("")

    assert entrypoint._uses_relational_store() is False


def test_an_authenticated_deployment_still_migrates(config):
    config("a-real-secret")

    assert entrypoint._uses_relational_store() is True


def test_launch_skips_the_migration_it_would_crash_on(monkeypatch, capsys):
    """The regression: a stale local database must not block a dev launch."""
    monkeypatch.setattr(entrypoint, "_uses_relational_store", lambda: False)
    monkeypatch.setattr(
        entrypoint,
        "_migrate",
        lambda: pytest.fail("single-user launch must not run Alembic"),
    )
    monkeypatch.setattr(entrypoint, "load_environment", lambda: None)
    monkeypatch.setattr(entrypoint.os, "chdir", lambda _path: None)
    monkeypatch.setattr("sys.argv", ["backend_entrypoint.py", "--migrate-only"])

    entrypoint.main()

    assert "skipping PostgreSQL migrations" in capsys.readouterr().err


def test_an_authenticated_launch_runs_the_migration(monkeypatch):
    ran = []
    monkeypatch.setattr(entrypoint, "_uses_relational_store", lambda: True)
    monkeypatch.setattr(entrypoint, "_migrate", lambda: ran.append("migrated"))
    monkeypatch.setattr(entrypoint, "load_environment", lambda: None)
    monkeypatch.setattr(entrypoint.os, "chdir", lambda _path: None)
    monkeypatch.setattr("sys.argv", ["backend_entrypoint.py", "--migrate-only"])

    entrypoint.main()

    assert ran == ["migrated"]


def test_skip_migrate_does_not_even_ask_which_store_is_in_use(monkeypatch):
    """``--skip-migrate`` is an explicit override; it short-circuits first."""
    launched = []
    monkeypatch.setattr(
        entrypoint,
        "_uses_relational_store",
        lambda: pytest.fail("--skip-migrate must not consult the config"),
    )
    monkeypatch.setattr(
        entrypoint, "_migrate", lambda: pytest.fail("--skip-migrate must not migrate")
    )
    monkeypatch.setattr(entrypoint, "load_environment", lambda: None)
    monkeypatch.setattr(entrypoint.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        entrypoint.os, "execv", lambda _exe, argv: launched.append(argv)
    )
    monkeypatch.setattr(
        "sys.argv", ["backend_entrypoint.py", "--skip-migrate", "--port", "8080"]
    )

    entrypoint.main()

    assert launched and launched[0][-2:] == ["--port", "8080"]
