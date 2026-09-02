#!/usr/bin/env python3
"""Apply local database migrations before starting the development server."""

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wuying_env import load_environment


BACKEND_DIR = Path(__file__).resolve().parent.parent
LOCAL_DATABASE_URL = "postgresql+asyncpg://openbox:openbox_dev@localhost:5432/openbox"

#: httpx and boto both honour these by default, so a VPN client that exports
#: them globally silently reroutes every backend call — the video relay, OSS
#: transfers, DashScope, the WUYING tunnel. All of those are mainland-direct
#: and want no proxy; a flaky tunnel there surfaces as "ConnectError" from a
#: provider that is in fact perfectly healthy (observed 2026-09-01: two paid
#: generations finished upstream while our transfer failed).
_PROXY_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
)


def _drop_inherited_proxy() -> None:
    """Start the dev server on a direct connection.

    Set OPENBOX_KEEP_PROXY=1 for the rare deployment that genuinely reaches
    its providers through one.
    """
    if os.environ.get("OPENBOX_KEEP_PROXY") == "1":
        return
    dropped = [name for name in _PROXY_VARS if os.environ.pop(name, None)]
    if dropped:
        print(f"[entrypoint] ignoring inherited proxy: {', '.join(sorted(dropped))}")


def _uses_relational_store() -> bool:
    """Mirror ``db.base.ensure_engine``: only an authenticated deployment.

    Desktop/single-user mode keeps its whole application store in
    ``.openbox/skill_jobs.db`` and never opens ``DATABASE_URL``, so migrating
    PostgreSQL there is not merely wasted work — it fails the launch outright
    whenever that database is on a revision this checkout does not carry (a
    branch, or a rollback). Read the same value the application will read, so
    the two cannot drift apart.
    """
    from core.config import get_config

    return bool(get_config().jwt_secret)


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
    env_file = load_environment()
    if env_file is not None:
        print(f"OpenBox configuration: {env_file.name}", file=sys.stderr)
    _drop_inherited_proxy()
    os.environ.setdefault("DATABASE_URL", LOCAL_DATABASE_URL)

    if not options.skip_migrate:
        if _uses_relational_store():
            _migrate()
        else:
            print(
                "[entrypoint] single-user mode: the application store is "
                ".openbox/skill_jobs.db; skipping PostgreSQL migrations",
                file=sys.stderr,
            )
    if options.migrate_only:
        return

    os.execv(
        sys.executable,
        [sys.executable, "-m", "uvicorn", "main:app", *uvicorn_args],
    )


if __name__ == "__main__":
    main()
