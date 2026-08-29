"""Shared runtime bootstrap + the dev-only embedded worker.

Both roles (standalone worker_main and the embedded dev worker) go through
ensure_job_engine so single-user mode gets a real ledger: without JWT_SECRET
the web process initializes no SQL engine at all, so we stand up a local
SQLite file and create the schema — same repository, same claim semantics
(§4.3). Multi-user mode reuses the PostgreSQL engine and alembic-owned schema.
"""
from __future__ import annotations

from pathlib import Path

from core.log import create_logger

log = create_logger("skill_runtime.embedded")

_worker = None
_reconciler = None
_outbox = None


async def ensure_job_engine(config) -> None:
    from db import base as db_base

    if db_base._engine is not None:
        return
    if config.jwt_secret:
        db_base.init_engine(config.database_url, config.db_pool_size, config.db_pool_overflow)
        return
    # Single-user mode: cwd-scoped SQLite ledger (same convention as
    # .openbox/tools). The alembic chain is PostgreSQL-only, so the schema
    # comes from create_all here.
    data_dir = Path.cwd() / ".openbox"
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = db_base.init_engine(f"sqlite+aiosqlite:///{data_dir / 'skill_jobs.db'}")
    import db.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(db_base.Base.metadata.create_all)
        await conn.run_sync(_upgrade_sqlite_skill_jobs)
        await conn.run_sync(_seed_single_user_scope)
    log.info(f"Single-user skill job ledger at {data_dir / 'skill_jobs.db'}")


def _upgrade_sqlite_skill_jobs(connection) -> None:
    """Additive schema bridge for pre-existing embedded SQLite ledgers.

    Alembic owns PostgreSQL production. ``create_all`` does not alter an old
    SQLite table, so without this bridge a desktop upgraded in place would
    start the new worker against columns that only exist in model metadata.
    """
    from sqlalchemy import inspect

    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    if "skill_jobs" not in table_names:
        return
    existing = {column["name"] for column in inspector.get_columns("skill_jobs")}
    additions = {
        "output_schema": "TEXT NOT NULL DEFAULT '{}'",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "invocation_timeout_seconds": "INTEGER NOT NULL DEFAULT 120",
        "max_external_wait_seconds": "INTEGER NOT NULL DEFAULT 86400",
        "user_input_timeout_seconds": "INTEGER",
        "cancel_requires_handler": "BOOLEAN NOT NULL DEFAULT 0",
        "continue_agent_on_success": "BOOLEAN NOT NULL DEFAULT 0",
        "external_wait_seconds": "INTEGER NOT NULL DEFAULT 0",
        "external_wait_started_at": "DATETIME",
    }
    for name, ddl in additions.items():
        if name not in existing:
            connection.exec_driver_sql(f"ALTER TABLE skill_jobs ADD COLUMN {name} {ddl}")
    if "session_inbox" in table_names:
        inbox_columns = {
            column["name"] for column in inspector.get_columns("session_inbox")
        }
        if "claim_token" not in inbox_columns:
            connection.exec_driver_sql(
                "ALTER TABLE session_inbox ADD COLUMN claim_token VARCHAR(64)"
            )
        connection.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS ix_session_inbox_claim_recovery
                ON session_inbox (status, consumed_at)
            """
        )
    if "messages" in table_names:
        message_indexes = {
            index["name"]
            for index in inspector.get_indexes("messages")
            if index.get("name")
        }
        connection.exec_driver_sql("DROP INDEX IF EXISTS uq_messages_receipt_marker")
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX uq_messages_receipt_marker
                ON messages (session_id, client_message_id)
             WHERE client_message_id LIKE 'sjr:%'
               AND role = 'assistant'
               AND finish = 'skill_job_receipt'
            """
        )
        if "uq_messages_inbox_marker" not in message_indexes:
            # The public API did not reserve this namespace before the inbox
            # index existed. Clear those historical client-chosen ids exactly
            # once, while installing the index. Running this on every startup
            # would erase legitimate durable continuation markers and could
            # make a restarted dispatcher create a second Agent turn.
            connection.exec_driver_sql(
                "UPDATE messages SET client_message_id = NULL "
                "WHERE client_message_id LIKE 'sji:%'"
            )
            connection.exec_driver_sql(
                """
                CREATE UNIQUE INDEX uq_messages_inbox_marker
                    ON messages (session_id, client_message_id)
                 WHERE client_message_id LIKE 'sji:%'
                """
            )
    connection.exec_driver_sql(
        """
        UPDATE skill_jobs
           SET cancel_requires_handler = 1,
               max_external_wait_seconds = 7200,
               invocation_timeout_seconds = CASE
                   WHEN operation = 'segment.transcribe' THEN 600 ELSE 120 END
         WHERE skill_key = 'builtin:video-production'
           AND operation IN ('segment.generate', 'segment.transcribe', 'production.render')
        """
    )


def _seed_single_user_scope(connection) -> None:
    """Create the stable owner/project required by relational Session rows.

    Multi-user registration owns this bootstrap in PostgreSQL. Desktop mode
    has no registration flow, but the same models still carry non-null user
    and project foreign keys.
    """
    from datetime import datetime, timezone

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


async def start_embedded(config) -> None:
    """Start worker + reconciler + outbox inside the web process (dev only)."""
    global _worker, _reconciler, _outbox
    if _worker is not None:
        return
    if config.jwt_secret:
        log.warning(
            "Embedded skill worker in multi-user mode: fine for development, "
            "run the standalone worker role in production (§4.3)"
        )
    await ensure_job_engine(config)

    from skill_runtime import registry
    from skill_runtime.outbox import OutboxPublisher
    from skill_runtime.reconciler import Reconciler
    from skill_runtime.worker import SkillJobWorker

    registry.load_builtin_handlers()
    registry.validate_runtime_dependencies(config)
    _worker = SkillJobWorker(
        queues=tuple(q.strip() for q in config.skill_worker_queues.split(",") if q.strip()),
        concurrency=config.skill_worker_concurrency,
        lease_seconds=config.skill_worker_lease_seconds,
        per_user_limit=config.skill_worker_per_user_concurrency,
        invocation_timeout=config.skill_worker_invocation_timeout,
    )
    _reconciler = Reconciler()
    _outbox = OutboxPublisher()
    _worker.start()
    _reconciler.start()
    _outbox.start()
    log.info("Embedded skill worker started")


async def stop_embedded() -> None:
    global _worker, _reconciler, _outbox
    if _worker is not None:
        await _worker.stop()
        _worker = None
    if _reconciler is not None:
        await _reconciler.stop()
        _reconciler = None
    if _outbox is not None:
        await _outbox.stop()
        _outbox = None


def notify_worker() -> None:
    """Poke the embedded worker after a local admission. Best-effort by
    contract: a no-op under the standalone worker role, and never raises —
    the due scan is the correctness backstop."""
    try:
        if _worker is not None:
            _worker.notify()
        if _outbox is not None:
            _outbox.notify()
    except Exception:
        pass
