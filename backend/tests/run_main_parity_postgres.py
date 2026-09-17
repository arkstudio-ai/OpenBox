"""Run main's fleet/Skill API contracts on an isolated local PostgreSQL.

PARITY_TEST_DATABASE_URL=postgresql+asyncpg://.../openbox_main_parity_* \
    uv run python tests/run_main_parity_postgres.py

Cloud calls in these suites use fakes. Never reads the application's DATABASE_URL.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from db import base
import db.models  # noqa: F401

url = make_url(os.environ["PARITY_TEST_DATABASE_URL"])
if (url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost"}
        or not (url.database or "").startswith("openbox_main_parity")):
    raise ValueError("Only an explicitly named disposable local parity database is allowed")
engine = create_async_engine(url, poolclass=NullPool)
base._engine = engine
base._session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def prepare():
    async with engine.begin() as connection:
        await connection.run_sync(base.Base.metadata.create_all)


asyncio.run(prepare())
raise SystemExit(pytest.main([
    "tests/unit/test_fleet.py", "tests/unit/test_admin_fleet_api.py",
    "tests/unit/test_wuying_fleet_api.py", "tests/unit/test_wuying_channel.py",
    "tests/unit/test_wuying_provisioning.py", "tests/unit/test_desktop_activation.py",
    "tests/unit/test_desktop_view_matches_sandbox.py",
    "tests/unit/test_user_skill_library.py", "tests/unit/test_user_skill_api.py",
    "tests/unit/test_admin_skills_api.py", "tests/unit/test_admin_skill_management.py",
    "tests/unit/test_sandbox_client_skill_packages.py",
    "-q", "--disable-warnings", "--tb=short", *sys.argv[1:],
]))
