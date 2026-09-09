"""Opt-in integration runner. Only a disposable named test database is allowed.

ADMIN_TEST_DATABASE_URL=postgresql+asyncpg://.../openbox_admin_skill_... \
    uv run python tests/run_admin_skill_postgres.py
Never accepts the application's normal DATABASE_URL.
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

url = make_url(os.environ["ADMIN_TEST_DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg", "PostgreSQL only"
assert (url.database or "").startswith("openbox_admin_skill_"), (
    "Disposable test database only"
)
engine = create_async_engine(url, poolclass=NullPool)
base._engine = engine
base._session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def prepare():
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)


asyncio.run(prepare())
raise SystemExit(
    pytest.main(
        [
            "tests/unit/test_admin_skill_management.py",
            "-q",
            "--disable-warnings",
            "--maxfail=2",
        ]
    )
)
