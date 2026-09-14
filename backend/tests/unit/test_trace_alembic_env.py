"""The trace alembic env migrates only TRAJECTORY_DATABASE_URL, never the business database (SPEC 6.2)."""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND = Path(__file__).resolve().parents[2]


def _config(**kwargs) -> Config:
    config = Config(str(BACKEND / "alembic_trajectory.ini"), **kwargs)
    config.set_main_option("script_location", str(BACKEND / "trajectory" / "store" / "migrations"))
    return config


def _tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _business_chain_database(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
    engine.dispose()


def _refused(config: Config | None = None, sql: bool = False) -> str:
    with pytest.raises(SystemExit) as exc_info:
        command.upgrade(config or _config(output_buffer=io.StringIO()), "head", sql=sql)
    message = str(exc_info.value.code)
    assert message.startswith("Trajectory trace migrations refused:")
    return message


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("TRAJECTORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return monkeypatch


@pytest.mark.parametrize("sql", [False, True])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_trace_url_exits_and_never_falls_back_to_database_url(tmp_path, env, value, sql):
    business = tmp_path / "business.db"
    env.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{business}")
    if value is not None:
        env.setenv("TRAJECTORY_DATABASE_URL", value)
    assert "TRAJECTORY_DATABASE_URL is not set" in _refused(sql=sql)
    assert not business.exists()


def test_trace_url_equal_to_database_url_is_refused(tmp_path, env):
    url = f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}"
    env.setenv("DATABASE_URL", url)
    env.setenv("TRAJECTORY_DATABASE_URL", url)
    assert "business database" in _refused()
    assert not (tmp_path / "shared.db").exists()


@pytest.mark.parametrize("trace_url, business_url", [
    ("sqlite+aiosqlite:///shared.db", "sqlite:///{tmp}/shared.db"),
    ("postgresql+asyncpg://trace:other@127.0.0.1:5432/openbox", "postgresql://openbox:openbox@localhost/openbox"),
    ("postgresql+asyncpg://openbox:openbox@LOCALHOST/openbox?ssl=disable",
     "postgresql+asyncpg://openbox:openbox@localhost:5432/openbox"),
])
def test_urls_naming_the_business_database_are_refused_before_connecting(tmp_path, env, trace_url, business_url):
    # Offline mode: even a broken guard could never connect to a real database.
    env.chdir(tmp_path)
    env.setenv("DATABASE_URL", business_url.format(tmp=tmp_path))
    env.setenv("TRAJECTORY_DATABASE_URL", trace_url)
    assert "business database" in _refused(sql=True)
    assert not (tmp_path / "shared.db").exists()


def test_invalid_trace_url_is_refused(env):
    env.setenv("TRAJECTORY_DATABASE_URL", "not a database url")
    assert "not a valid database URL" in _refused(sql=True)


def test_database_holding_the_business_chain_is_refused(tmp_path, env):
    target = tmp_path / "business-copy.db"
    _business_chain_database(target)
    env.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{target}")
    assert "business migration table alembic_version" in _refused()
    assert _tables(target) == {"alembic_version"}


def test_business_version_table_name_is_refused(tmp_path, env):
    env.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    config = _config()
    config.set_main_option("version_table", "alembic_version")
    assert "version_table" in _refused(config)
    assert not (tmp_path / "trace.db").exists()


def test_distinct_trace_url_migrates_only_the_trace_database(tmp_path, env):
    business = tmp_path / "business.db"
    _business_chain_database(business)
    trace = tmp_path / "trace.db"
    env.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{business}")
    env.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{trace}")
    command.upgrade(_config(), "head")
    assert _tables(business) == {"alembic_version"}
    tables = _tables(trace)
    assert {"trajectory_alembic_version", "session_trajectories", "trajectory_events"} <= tables
    assert "alembic_version" not in tables


def _cli(*args: str, **environment: str) -> subprocess.CompletedProcess:
    child_env = {key: value for key, value in os.environ.items()
                 if key not in {"TRAJECTORY_DATABASE_URL", "DATABASE_URL"}}
    child_env.update(environment)
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic_trajectory.ini", *args],
                          cwd=BACKEND, env=child_env, capture_output=True, text=True, timeout=120)


def test_cli_exits_non_zero_without_trace_url_and_migrates_with_it(tmp_path):
    business = tmp_path / "business.db"
    refused = _cli("upgrade", "head", DATABASE_URL=f"sqlite+aiosqlite:///{business}")
    assert refused.returncode == 1
    assert "TRAJECTORY_DATABASE_URL is not set" in refused.stderr
    assert not business.exists()

    trace = tmp_path / "trace.db"
    migrated = _cli("upgrade", "head", DATABASE_URL=f"sqlite+aiosqlite:///{business}",
                    TRAJECTORY_DATABASE_URL=f"sqlite+aiosqlite:///{trace}")
    assert migrated.returncode == 0, migrated.stderr
    engine = sa.create_engine(f"sqlite:///{trace}")
    with engine.connect() as connection:
        version = connection.exec_driver_sql("SELECT version_num FROM trajectory_alembic_version").scalar_one()
    engine.dispose()
    from alembic.script import ScriptDirectory
    assert version == ScriptDirectory.from_config(_config()).get_current_head()
    assert not business.exists()
