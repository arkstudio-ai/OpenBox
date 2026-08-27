#!/usr/bin/env python3
"""Apply local database migrations before starting the development server."""

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parent.parent
LOCAL_DATABASE_URL = "postgresql+asyncpg://openbox:openbox_dev@localhost:5432/openbox"


def _migrate() -> None:
    alembic_config = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(BACKEND_DIR / "db" / "migrations")
    )
    command.upgrade(alembic_config, "head")


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--migrate-only", action="store_true")
    mode.add_argument("--skip-migrate", action="store_true")
    options, uvicorn_args = parser.parse_known_args()

    os.chdir(BACKEND_DIR)
    load_dotenv(BACKEND_DIR / ".env")
    os.environ.setdefault("DATABASE_URL", LOCAL_DATABASE_URL)

    if not options.skip_migrate:
        _migrate()
    if options.migrate_only:
        return

    os.execv(
        sys.executable,
        [sys.executable, "-m", "uvicorn", "main:app", *uvicorn_args],
    )


if __name__ == "__main__":
    main()
