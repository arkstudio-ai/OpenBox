"""SQLAlchemy async engine, session factory, and base model."""
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import sqlalchemy as sa
from sqlalchemy import TypeDecorator, Text
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
    AsyncEngine,
)
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from core.log import create_logger

log = create_logger("db")

# ---------------------------------------------------------------------------
# Cross-database JSONB type
# ---------------------------------------------------------------------------

class JSONType(TypeDecorator):
    """JSONB on PostgreSQL, JSON-as-TEXT on SQLite / others."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is not None and dialect.name != "postgresql":
            return json.dumps(value, ensure_ascii=False)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and isinstance(value, str):
            return json.loads(value)
        return value


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Base class for all ORM models."""
    type_annotation_map = {
        dict: JSONType,
        datetime: sa.DateTime(timezone=True),
    }


# ---------------------------------------------------------------------------
# Engine & session factory (module-level singletons, lazily initialized)
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, pool_size: int = 10, pool_overflow: int = 20) -> AsyncEngine:
    """Create and store the async engine singleton."""
    global _engine, _session_factory

    connect_args = {}
    # SQLite doesn't support pool_size / pool_overflow
    if "sqlite" in database_url:
        _engine = create_async_engine(database_url, echo=False)
    else:
        _engine = create_async_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=pool_overflow,
            echo=False,
        )

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    log.info(f"Database engine initialized: {database_url.split('@')[-1] if '@' in database_url else database_url}")
    return _engine


async def ensure_engine(config: Any) -> AsyncEngine:
    """Initialize the shared application database once.

    Authenticated deployments use the configured PostgreSQL database, which
    the infrastructure bootstrap normally initializes first. Desktop mode has
    no registration/bootstrap process, so it keeps using the historical
    ``.openbox/skill_jobs.db`` path for compatibility with existing sessions
    and projects. The filename is legacy; the database is now the general
    application store.
    """
    if _engine is not None:
        return _engine
    if config.jwt_secret:
        return init_engine(
            config.database_url,
            config.db_pool_size,
            config.db_pool_overflow,
        )

    data_dir = Path.cwd() / ".openbox"
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path = data_dir / "skill_jobs.db"
    engine = init_engine(f"sqlite+aiosqlite:///{database_path}")
    import db.models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_seed_single_user_scope)
    log.info(f"Single-user application database at {database_path}")
    return engine


def _seed_single_user_scope(connection) -> None:
    """Create the stable owner, workspace, and project for desktop mode."""
    from datetime import timezone

    now = datetime.now(timezone.utc).isoformat()
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO users
            (id, username, role, is_active, failed_login_count, is_deleted,
             created_at, updated_at)
        VALUES ('default', 'default', 'admin', 1, 0, 0, ?, ?)
        """,
        (now, now),
    )
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO workspaces
            (id, name, owner_user_id, kind, is_deleted, created_at, updated_at)
        VALUES ('ws_default', 'Default', 'default', 'personal', 0, ?, ?)
        """,
        (now, now),
    )
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO workspace_members
            (workspace_id, user_id, role, status, created_at, updated_at)
        VALUES ('ws_default', 'default', 'owner', 'active', ?, ?)
        """,
        (now, now),
    )
    connection.exec_driver_sql(
        "UPDATE users SET default_workspace_id = 'ws_default' WHERE id = 'default'"
    )
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO projects
            (id, user_id, workspace_id, name, slug, is_deleted, created_at, updated_at)
        VALUES ('default', 'default', 'ws_default', 'Default', 'default', 0, ?, ?)
        """,
        (now, now),
    )


def get_engine() -> AsyncEngine:
    """Get the current engine (must be initialized first)."""
    if _engine is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return _engine


# The HTTP readiness probe must cover additive columns/tables that ORM
# ``create_all`` cannot retrofit into an existing database.  Keeping this
# small, explicit list beside the engine avoids reporting a healthy service
# whose first session query will fail with UndefinedColumnError.
_READINESS_SCHEMA: dict[str, frozenset[str]] = {
    "sessions": frozenset({"tool_exposure_state"}),
    "parts": frozenset({
        "stream_seq",
        "canonical_tool_id",
        "wire_tool_name",
        "provider_binding_digest",
        "provider_dialect",
    }),
    "internal_parts": frozenset({
        "id",
        "session_id",
        "message_id",
        "user_id",
        "kind",
        "capability_key_digest",
        "response_chain_id",
        "stream_seq",
        "origin_seq",
        "dedupe_key",
        "data",
        "created_at",
    }),
}


def _missing_readiness_schema(connection) -> tuple[str, ...]:
    """Return stable table/column identifiers missing from one SQL database."""
    inspector = sa.inspect(connection)
    available_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table, required_columns in _READINESS_SCHEMA.items():
        if table not in available_tables:
            missing.append(table)
            continue
        available_columns = {
            column["name"] for column in inspector.get_columns(table)
        }
        missing.extend(
            f"{table}.{column}"
            for column in sorted(required_columns - available_columns)
        )
    return tuple(missing)


async def database_schema_ready() -> bool:
    """Check connectivity and the minimum schema required by this release."""
    try:
        engine = get_engine()
        async with engine.connect() as connection:
            missing = await connection.run_sync(_missing_readiness_schema)
        if missing:
            log.error("Database schema is not ready missing=%s", ",".join(missing))
            return False
        return True
    except Exception as exc:
        log.error("Database readiness check failed error_type=%s", type(exc).__name__)
        return False


@asynccontextmanager
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Short-lived async session context manager.

    Usage:
        async with get_db_session() as session:
            result = await session.execute(...)
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")

    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def close_engine() -> None:
    """Close the engine and release all connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        log.info("Database engine closed")
