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
    log.info(f"Single-user skill job ledger at {data_dir / 'skill_jobs.db'}")


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
    """Poke the embedded worker after a local admission (wake may be lost —
    the due scan is the correctness backstop)."""
    if _worker is not None:
        _worker.notify()
    if _outbox is not None:
        _outbox.notify()
