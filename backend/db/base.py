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


def init_engine(
    database_url: str, pool_size: int = 10, pool_overflow: int = 20
) -> AsyncEngine:
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
    log.info(
        f"Database engine initialized: {database_url.split('@')[-1] if '@' in database_url else database_url}"
    )
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
        await connection.run_sync(_ensure_single_user_legacy_tables)
        await connection.run_sync(_seed_single_user_scope)
        from agent.schema import _upgrade_sqlite_subagent_schema

        await connection.run_sync(_upgrade_sqlite_subagent_schema)
    log.info(f"Single-user application database at {database_path}")
    return engine


_SINGLE_USER_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # Keep these declarations byte-for-byte compatible with Alembic revision
    # c7d9e1f3a5b7. SQLite fills every existing row with the DEFAULT value when
    # adding this NOT NULL column, which is equivalent to that revision's
    # expand/backfill/contract sequence.
    "sessions": {
        "tool_exposure_state": "TEXT NOT NULL DEFAULT '{}'",
        "variant": "VARCHAR(32)",
    },
    "parts": {
        "stream_seq": "INTEGER",
        "canonical_tool_id": "VARCHAR(128)",
        "wire_tool_name": "VARCHAR(128)",
        "provider_binding_digest": "VARCHAR(64)",
        "provider_dialect": "VARCHAR(64)",
    },
    "user_skills": {
        "lifecycle_state": "VARCHAR(16) NOT NULL DEFAULT 'active'",
        "lifecycle_generation": "INTEGER NOT NULL DEFAULT 1",
    },
    "agent_inbox_items": {
        "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
        "delivery_last_error": "TEXT",
    },
}


def _ensure_single_user_legacy_tables(connection) -> None:
    """Upgrade the persistent desktop SQLite store without Alembic.

    ``kv_store`` predates the ORM and therefore can never be created by
    ``Base.metadata.create_all()``. ``create_all`` also cannot add columns or
    indexes to an existing table, so stores created before exposure revision
    c7 need a narrow additive bridge for ``sessions`` and ``parts``. New ORM
    tables, including ``internal_parts``, are created immediately before this
    helper runs. PostgreSQL deployments continue to use Alembic exclusively.
    """
    if connection.dialect.name != "sqlite":
        return
    connection.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS kv_store ("
        "key TEXT PRIMARY KEY NOT NULL, "
        "value TEXT NOT NULL, "
        "updated_at DATETIME)"
    )

    tables = set(sa.inspect(connection).get_table_names())
    for table, columns in _SINGLE_USER_ADDITIVE_COLUMNS.items():
        if table not in tables:
            continue
        present = {
            column["name"] for column in sa.inspect(connection).get_columns(table)
        }
        for name, ddl_type in columns.items():
            if name not in present:
                connection.exec_driver_sql(
                    f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl_type}'
                )

    if "parts" in tables:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_parts_message_stream "
            "ON parts (message_id, stream_seq)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_parts_canonical_tool "
            "ON parts (session_id, canonical_tool_id)"
        )
    if "user_skills" in tables:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_user_skills_owner_lifecycle "
            "ON user_skills (owner_id, lifecycle_state, updated_at)"
        )


def _seed_single_user_scope(connection) -> None:
    """Create the stable owner/project required by relational desktop data."""
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
        INSERT OR IGNORE INTO projects
            (id, user_id, name, slug, is_deleted, created_at, updated_at)
        VALUES ('default', 'default', 'Default', 'default', 0, ?, ?)
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
    "kv_store": frozenset({"key", "value", "updated_at"}),
    "cron_jobs": frozenset(
        {
            "run_generation",
            "run_token",
            "run_owner",
            "lease_expires_at",
            "heartbeat_at",
        }
    ),
    "cron_runs": frozenset(
        {
            "claim_token",
            "claim_generation",
            "claim_owner",
        }
    ),
    "cron_delivery_outbox": frozenset(
        {
            "id",
            "run_id",
            "job_id",
            "user_id",
            "project_id",
            "session_id",
            "kind",
            "payload",
            "state",
            "attempts",
            "available_at",
            "claim_token",
            "claim_owner",
            "claim_expires_at",
            "delivered_at",
            "last_error",
            "created_at",
            "updated_at",
        }
    ),
    "agent_driver_states": frozenset(
        {
            "session_id",
            "user_id",
            "generation",
            "run_id",
            "owner_id",
            "phase",
            "trigger_message_id",
            "lease_expires_at",
            "abort_requested_at",
            "started_at",
            "updated_at",
        }
    ),
    "agent_inbox_items": frozenset(
        {
            "id",
            "user_id",
            "project_id",
            "session_id",
            "client_id",
            "request_digest",
            "delivery",
            "target",
            "prompt",
            "attachments",
            "agent",
            "model",
            "video_model",
            "variant",
            "output_format",
            "state",
            "message_id",
            "result_message_id",
            "run_id",
            "generation",
            "turn_id",
            "step_id",
            "claim_token",
            "claim_owner",
            "claim_expires_at",
            "outcome",
            "error",
            "accepted_at",
            "claimed_at",
            "canceled_at",
            "settled_at",
            "delivery_attempts",
            "delivery_last_error",
            "created_at",
            "updated_at",
        }
    ),
    "external_effects": frozenset(
        {
            "id",
            "tenant_id",
            "project_id",
            "session_id",
            "run_id",
            "run_generation",
            "adapter",
            "provider",
            "operation",
            "idempotency_key",
            "request_hash",
            "safe_context",
            "state",
            "attempt_count",
            "reconcile_count",
            "claim_generation",
            "claim_kind",
            "claim_token",
            "claim_owner",
            "claim_expires_at",
            "provider_handle",
            "provider_receipt",
            "projection",
            "last_error",
            "reconcile_after",
            "prepared_at",
            "submitting_at",
            "accepted_at",
            "completed_at",
            "created_at",
            "updated_at",
        }
    ),
    "external_effect_evidence": frozenset(
        {
            "id",
            "effect_id",
            "sequence",
            "claim_generation",
            "phase",
            "evidence",
            "created_at",
        }
    ),
    "session_surface_events": frozenset(
        {
            "id",
            "session_id",
            "user_id",
            "sequence",
            "kind",
            "anchor_message_id",
            "replacement_run_id",
            "replacement_generation",
            "hidden_message_ids",
            "public_snapshot",
            "created_at",
        }
    ),
    "agent_events": frozenset(
        {
            "id",
            "session_id",
            "user_id",
            "sequence",
            "event_key",
            "kind",
            "run_id",
            "generation",
            "turn_id",
            "step_id",
            "message_id",
            "part_id",
            "tool_call_id",
            "payload",
            "created_at",
        }
    ),
    "task_handoffs": frozenset(
        {
            "id",
            "user_id",
            "parent_session_id",
            "parent_message_id",
            "parent_part_id",
            "parent_run_id",
            "parent_generation",
            "child_session_id",
            "child_trigger_message_id",
            "child_run_id",
            "child_generation",
            "state",
            "task_title",
            "subagent_type",
            "result_payload",
            "completed_at",
            "rejoined_at",
            "created_at",
            "updated_at",
        }
    ),
    "subagent_descriptors": frozenset(
        {
            "id",
            "user_id",
            "project_id",
            "parent_session_id",
            "child_session_id",
            "root_session_id",
            "parent_descriptor_id",
            "depth",
            "subagent_type",
            "lifecycle",
            "state",
            "generation",
            "active_activation_id",
            "interrupt_requested_generation",
            "interrupt_applied_generation",
            "created_at",
            "updated_at",
            "settled_at",
        }
    ),
    "subagent_activations": frozenset(
        {
            "id",
            "descriptor_id",
            "user_id",
            "project_id",
            "parent_session_id",
            "parent_message_id",
            "parent_part_id",
            "parent_run_id",
            "parent_generation",
            "descriptor_generation",
            "kind",
            "child_session_id",
            "child_trigger_message_id",
            "child_run_id",
            "child_generation",
            "state",
            "claim_token",
            "claim_owner",
            "claim_expires_at",
            "task_title",
            "created_at",
            "updated_at",
            "completed_at",
        }
    ),
    "subagent_outbox": frozenset(
        {
            "activation_id",
            "descriptor_id",
            "user_id",
            "project_id",
            "parent_session_id",
            "parent_message_id",
            "parent_part_id",
            "state",
            "outcome",
            "result_payload",
            "created_at",
            "updated_at",
            "ready_at",
            "delivered_at",
        }
    ),
    "user_skills": frozenset(
        {
            "lifecycle_state",
            "lifecycle_generation",
        }
    ),
    "sessions": frozenset({"tool_exposure_state", "variant"}),
    "parts": frozenset(
        {
            "stream_seq",
            "canonical_tool_id",
            "wire_tool_name",
            "provider_binding_digest",
            "provider_dialect",
        }
    ),
    "internal_parts": frozenset(
        {
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
        }
    ),
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
        available_columns = {column["name"] for column in inspector.get_columns(table)}
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
