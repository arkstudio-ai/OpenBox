"""Shared Alembic helpers for the migration tests.

Several migration tests assert that upgrading reaches exactly one head. That
claim is about the *shape* of the revision tree, never about a particular
revision id — so pinning a literal made every subsequent migration fail tests
that had nothing to do with it. Resolve the head from the tree instead, in one
place, so adding a migration stays a one-file change.
"""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(database_path: str | Path | None = None) -> Config:
    """An Alembic config pointed at this checkout's migration tree."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option(
        "script_location", str(BACKEND_DIR / "db" / "migrations")
    )
    if database_path is not None:
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def current_head() -> str:
    """Whatever the migration tree currently ends at.

    Asserts single-headedness on the way, which is the property CI enforces and
    the one every caller actually cares about.
    """
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, f"expected a single Alembic head, found {heads}"
    return heads[0]
