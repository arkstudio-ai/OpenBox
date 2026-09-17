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
        await connection.run_sync(_upgrade_desktop_billing_columns)
        await connection.run_sync(_upgrade_desktop_skill_store_columns)
        await connection.run_sync(_upgrade_desktop_trajectory_columns)
        await connection.run_sync(_retire_desktop_trajectory_tables)
        await connection.run_sync(_index_desktop_metadata_sync)
        await connection.run_sync(_upgrade_desktop_message_center_columns)
        await connection.run_sync(_ensure_single_user_legacy_tables)
        await connection.run_sync(_seed_single_user_scope)
        from agent.schema import _upgrade_sqlite_subagent_schema

        await connection.run_sync(_upgrade_sqlite_subagent_schema)
    log.info(f"Single-user application database at {database_path}")
    return engine


def _upgrade_desktop_trajectory_columns(connection) -> None:
    """Keep existing desktop databases compatible with durable trace contexts."""
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    for table in ("session_executions", "cron_runs"):
        if table not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "trace_context" not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN trace_context TEXT")


#: Retired business trajectory tables and the legacy_trajectory_* names an earlier
#: revision of migration d3b5f7a9c1e2 gave them. Old recordings are not kept, so both
#: are dropped; children come before session_trajectories (SQLite has no CASCADE).
RETIRED_TRAJECTORY_TABLES = (
    "trajectory_exports", "legacy_trajectory_exports",
    "trajectory_checkpoints", "legacy_trajectory_checkpoints",
    "trajectory_session_summaries", "legacy_trajectory_session_summaries",
    "trajectory_records", "legacy_trajectory_records",
    "trajectory_payloads", "legacy_trajectory_payloads",
    "trajectory_events", "legacy_trajectory_events",
    "session_trajectories", "legacy_trajectory_sessions",
)


def _retire_desktop_trajectory_tables(connection) -> None:
    """Drop the retired trajectory tables the way migration d3b5f7a9c1e2 does.

    Desktop databases never run Alembic. Trajectory data now lives in the
    trace database and old recordings are not kept.
    """
    tables = set(sa.inspect(connection).get_table_names())
    for table in RETIRED_TRAJECTORY_TABLES:
        if table in tables:
            connection.exec_driver_sql(f'DROP TABLE "{table}"')


#: The metadata sync cursor indexes of migration e5c7a9b1d3f4.
_METADATA_SYNC_INDEXES = (
    ("ix_sessions_updated_id", "sessions"),
    ("ix_users_updated_id", "users"),
    ("ix_workspaces_updated_id", "workspaces"),
)


def _index_desktop_metadata_sync(connection) -> None:
    """create_all adds indexes only to new tables; existing desktop tables get them here."""
    tables = set(sa.inspect(connection).get_table_names())
    for name, table in _METADATA_SYNC_INDEXES:
        if table in tables:
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {table} (updated_at, id)")


def _upgrade_desktop_billing_columns(connection) -> None:
    """Desktop SQLite uses create_all, which cannot add columns to old orders."""
    columns = {column["name"] for column in sa.inspect(connection).get_columns("payment_orders")}
    if "kind" not in columns:
        connection.exec_driver_sql("ALTER TABLE payment_orders ADD COLUMN kind VARCHAR(24) NOT NULL DEFAULT 'topup'")
    if "product" not in columns:
        connection.exec_driver_sql("ALTER TABLE payment_orders ADD COLUMN product TEXT")
    if "cancelled_at" not in columns:
        connection.exec_driver_sql("ALTER TABLE payment_orders ADD COLUMN cancelled_at DATETIME")
    if "cancellation_reason" not in columns:
        connection.exec_driver_sql("ALTER TABLE payment_orders ADD COLUMN cancellation_reason VARCHAR(32)")


#: Message-centre additions to ``notifications`` (migration a1c2e3b4d5f6).
#: Desktop SQLite keeps ``workspace_id NOT NULL`` from its original
#: create_all; account-level notices there fall back to the seeded workspace.
_DESKTOP_INBOX_COLUMNS = (
    ("category", "VARCHAR(16) NOT NULL DEFAULT 'system'"),
    ("link", "TEXT"),
    ("source_key", "VARCHAR(255)"),
    ("announcement_id", "VARCHAR(64)"),
    ("resolved_at", "DATETIME"),
    ("expires_at", "DATETIME"),
)


def _upgrade_desktop_message_center_columns(connection) -> None:
    """Retrofit the message centre onto a desktop database create_all cannot touch."""
    columns = {column["name"] for column in sa.inspect(connection).get_columns("notifications")}
    for name, ddl in _DESKTOP_INBOX_COLUMNS:
        if name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE notifications ADD COLUMN {name} {ddl}")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_notifications_user_created ON notifications (user_id, created_at)"
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_source ON notifications (user_id, source_key)"
    )


#: The moderated store's additions to ``user_skills``, with the same defaults
#: migration c8e0a2b4d6f1 writes.
_DESKTOP_SKILL_COLUMNS = (
    ("listing", "VARCHAR(16) NOT NULL DEFAULT 'listed'"),
    ("listing_note", "TEXT"),
    ("listing_changed_by", "VARCHAR(64)"),
    ("listing_changed_at", "DATETIME"),
    ("is_official", "BOOLEAN NOT NULL DEFAULT 0"),
    ("featured", "BOOLEAN NOT NULL DEFAULT 0"),
)


def _upgrade_desktop_skill_store_columns(connection) -> None:
    """Retrofit the skill store onto a desktop database create_all cannot touch.

    Desktop mode never runs Alembic, and ``user_skills`` / ``skill_installs``
    both shipped before the store did: every existing desktop database already
    has them in the pre-store shape, which ``create_all`` leaves exactly as it
    found them. Without this the store's first query — ``skill_installs``
    grouped by ``catalog_id`` — fails with "no such column" and takes browsing,
    publishing and installing down with it.

    This is migration c8e0a2b4d6f1 restated for the one deployment that cannot
    run it; the defaults and the backfill are deliberately identical.
    """
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())

    if "user_skills" in tables:
        columns = {column["name"] for column in inspector.get_columns("user_skills")}
        for name, ddl in _DESKTOP_SKILL_COLUMNS:
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE user_skills ADD COLUMN {name} {ddl}"
                )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_user_skills_listing_published "
            "ON user_skills (listing, published_at)"
        )

    if "skill_installs" not in tables:
        return
    installs = {
        column["name"]: column
        for column in inspector.get_columns("skill_installs")
    }
    if "kind" not in installs:
        connection.exec_driver_sql(
            "ALTER TABLE skill_installs ADD COLUMN kind VARCHAR(8) "
            "NOT NULL DEFAULT 'skill'"
        )
    if "catalog_id" not in installs:
        # Added nullable and then filled: every row that predates the store is
        # a community install, because the column its key derives from used to
        # be NOT NULL.
        connection.exec_driver_sql(
            "ALTER TABLE skill_installs ADD COLUMN catalog_id VARCHAR(96)"
        )
        connection.exec_driver_sql(
            "UPDATE skill_installs SET catalog_id = 'community:' || user_skill_id "
            "WHERE catalog_id IS NULL"
        )

    uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("skill_installs")
    }
    old_shape = (
        not installs["user_skill_id"]["nullable"]
        or "uq_skill_installs_user_kind_dir" not in uniques
    )
    if not old_shape:
        return
    # SQLite can neither relax the NOT NULL that a catalogue install violates
    # (it has no ``user_skills`` row) nor widen a uniqueness key that predates
    # ``kind``. Rebuild from the model instead — the same shape the migration
    # leaves on a server, arrived at the way alembic's batch mode would.
    from db.models.skill_install import SkillInstall

    for index in inspector.get_indexes("skill_installs"):
        if index.get("name"):
            connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{index["name"]}"')
    connection.exec_driver_sql(
        "ALTER TABLE skill_installs RENAME TO skill_installs_legacy"
    )
    SkillInstall.__table__.create(connection)
    connection.exec_driver_sql(
        "INSERT INTO skill_installs (id, user_id, user_skill_id, kind, catalog_id, "
        "name, install_dir, installed_at) "
        "SELECT id, user_id, user_skill_id, kind, catalog_id, name, install_dir, "
        "installed_at FROM skill_installs_legacy"
    )
    connection.exec_driver_sql("DROP TABLE skill_installs_legacy")

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


def _seed_single_user_scope(connection) -> None:
    """Create the stable owner, workspace, and project for desktop mode."""
    from datetime import timezone

    from project.workspace import DEFAULT_NAME, DEFAULT_SLUG

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
        VALUES ('default', 'default', 'ws_default', ?, ?, 0, ?, ?)
        """,
        (DEFAULT_NAME, DEFAULT_SLUG, now, now),
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
    "session_executions": frozenset({
        "session_id", "user_id", "generation", "run_id", "run_generation", "lease_until",
        "run_origin", "run_progress", "resume_pending", "resume_error", "next_attempt_at", "updated_at", "trace_context",
    }),
    "cron_runs": frozenset({"id", "trace_context"}),
    # Trajectory data lives in the trace database; the business schema keeps
    # only the identity carriers above.
    "question_checkpoints": frozenset({
        "id", "session_id", "user_id", "generation", "message_id", "part_id", "status",
        "questions", "answers", "draft", "draft_revision", "continuation", "applied",
        "created_at", "updated_at", "expires_at",
    }),
    "desktop_activations": frozenset({
        "workspace_id", "request_id", "user_id", "state", "step", "attempts", "error",
        "next_run_at", "lease_owner", "lease_until", "purchase_kind", "purchase_started_at",
        "purchase_baseline", "created_at", "updated_at",
    }),
    "credit_balances": frozenset({"workspace_id", "balance", "updated_at"}),
    "usage_events": frozenset({"id", "workspace_id", "message_id", "tokens", "credits", "status", "pricing"}),
    "credit_ledger": frozenset({"id", "workspace_id", "idempotency_key", "amount", "balance_after"}),
    "payment_orders": frozenset({"id", "workspace_id", "user_id", "request_key", "provider_payment_id", "credits", "status", "kind", "product", "cancelled_at", "cancellation_reason"}),
    "billing_subscriptions": frozenset({"order_id", "workspace_id", "plan_id", "cycle", "plan", "starts_at", "ends_at"}),
    "payment_order_requests": frozenset({"workspace_id", "request_key", "order_id"}),
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
    "cloud_desktops": frozenset({
        "channel_kind",
        "private_ip",
        "tunnel_port",
        "tunnel_bind",
        "tunnel_pubkey",
        "tunnel_fingerprint",
        "action_api_key_hash",
        "action_api_key_ciphertext",
        "tunnel_state",
        "last_seen_at",
        "channel_error",
        "pool_state",
        "pool",
        "assigned_at",
        "released_at",
        "spec",
        "golden_image_id",
        "last_snapshot_at",
    }),
    "fleet_snapshots": frozenset({"id", "taken_at", "source", "ok", "payload", "error"}),
    "fleet_alerts": frozenset({
        "id", "rule", "severity", "resource_type", "resource_id", "message",
        "detail", "first_seen_at", "last_seen_at", "resolved_at", "acked_by",
        "acked_at", "muted_until",
    }),
    "pool_purchases": frozenset({
        "id", "desktop_id", "unit_price", "currency", "quantity", "request_id",
        "status", "created_by", "created_at", "error",
    }),
    "desktop_events": frozenset({
        "id", "ts", "desktop_id", "container_key", "session_id", "tool_call_id",
        "request_id", "kind", "status", "duration_ms", "summary", "detail", "diag_id",
    }),
    "kv_store": frozenset({"key", "value", "updated_at"}),
    "cron_runs": frozenset(
        {
            "id", "trace_context",
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
            "video_resolution",
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
