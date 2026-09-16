"""Async engine and sessions for the dedicated trajectory trace database.

The trace database has its own engine, declarative base and alembic chain and
never touches ``db.base``'s business engine, so no trace statement can run
inside, or wait on, a business transaction.
"""
from contextlib import asynccontextmanager
from datetime import datetime
import json
from typing import AsyncIterator

import orjson
from sqlalchemy import DateTime, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.log import create_logger
from db.base import JSONType

log = create_logger("trajectory.store")

SUPPORTED_DIALECTS = ("postgresql", "sqlite")

#: Settings for request-serving PostgreSQL connections. Background jobs that
#: need longer statements use ``SET LOCAL statement_timeout`` per transaction.
PG_SERVER_SETTINGS = {"statement_timeout": "5000", "application_name": "openbox-trace"}
#: Statement timeout of the trace migration connection: DDL on populated tables (t0002 rewrites a column type)
#: outlasts the trace role's 5 s default (wave-3 contract 2).
MIGRATION_STATEMENT_TIMEOUT = "15min"
#: Read-pool connections kept beyond the HTTP read slots (TRAJECTORY_READ_CONCURRENCY): WebSocket subscription
#: headers read with at most this many at once (worker/ws.py), so busy admin reads never make them wait for one.
READ_POOL_RESERVE = 2


def json_serializer(value) -> str:
    """JSONB bind text: orjson, and for what it refuses (integers beyond 64 bits, non-string keys) the stdlib
    ``json.dumps`` the engines used before."""
    try:
        return orjson.dumps(value).decode()
    except TypeError:
        return json.dumps(value)


#: JSONB codecs of every PostgreSQL trace engine, the writer's and the TraceReader's: event data, hints,
#: records and checkpoints are bound and read through these instead of the stdlib ``json``.
PG_JSON_ARGUMENTS = {"json_serializer": json_serializer, "json_deserializer": orjson.loads}


class TraceEngineNotInitialized(RuntimeError):
    """No trace engine is open: never initialized, already closed, or an embedded worker that failed to start."""


class TraceBase(DeclarativeBase):
    """Declarative base of the trace database models (never ``db.base.Base``)."""
    type_annotation_map = {
        dict: JSONType,
        datetime: DateTime(timezone=True),
    }


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_read_engine: AsyncEngine | None = None
_read_factory: async_sessionmaker[AsyncSession] | None = None


def init_trace_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5) -> AsyncEngine:
    """Create and store the trace engine singleton.

    PostgreSQL connections are pre-pinged and carry the statement timeout and
    application name above. SQLite gets no pool arguments; its connections
    enforce foreign keys (``ON DELETE CASCADE`` then behaves as on PostgreSQL)
    and use WAL so the embedded worker's readers do not block its writer.
    """
    global _engine, _session_factory, _read_engine, _read_factory
    parsed = make_url(url)
    dialect = parsed.get_backend_name()
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(f"Unsupported trace database dialect: {dialect}")
    if _engine is not None:
        log.warning("Replacing an open trace database engine; close_trace_engine() was not called")
    # The read pool belongs to the engine being replaced: the next read builds one for the new URL.
    _read_engine = _read_factory = None
    if dialect == "sqlite":
        engine = create_async_engine(url, echo=False)
        event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    else:
        engine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            connect_args={"server_settings": dict(PG_SERVER_SETTINGS)},
            echo=False,
            **PG_JSON_ARGUMENTS,
        )
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    log.info("Trace database engine initialized: %s", parsed.render_as_string(hide_password=True))
    return engine


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def allow_long_migrations(connection) -> None:
    """PostgreSQL: give a synchronous migration connection MIGRATION_STATEMENT_TIMEOUT for its whole session.

    Call it before the migrations begin their transaction: it commits, so the
    setting outlives that transaction and any commit a migration makes. SQLite:
    nothing to do.
    """
    if connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql(f"SET statement_timeout = '{MIGRATION_STATEMENT_TIMEOUT}'")
    connection.commit()


def get_trace_engine() -> AsyncEngine:
    """Get the trace engine (must be initialized first)."""
    if _engine is None:
        raise TraceEngineNotInitialized("Trace database engine not initialized. Call init_trace_engine() first.")
    return _engine


@asynccontextmanager
async def trace_session() -> AsyncIterator[AsyncSession]:
    """Short-lived trace session: commit on success, rollback on exception, always close."""
    if _session_factory is None:
        raise TraceEngineNotInitialized("Trace database engine not initialized. Call init_trace_engine() first.")

    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


class TraceReader:
    """Buffered SELECTs, each with its own short transaction.

    Repository code may await object storage between calls. No connection or
    transaction remains checked out then; loaded ORM values are detached.
    The fixed event watermark still bounds every repository read.
    """

    @property
    def bind(self):
        return get_trace_engine()

    def _factory(self):
        global _read_engine, _read_factory
        engine = get_trace_engine()
        if engine.dialect.name == "sqlite":
            # In-memory SQLite must use the same engine. Queries return their
            # connection immediately, including when the worker shares it.
            return _session_factory
        if _read_factory is None:
            from trajectory.config import integer
            pool_size = integer("TRAJECTORY_READ_DB_POOL_SIZE",
                                integer("TRAJECTORY_READ_CONCURRENCY", 2) + READ_POOL_RESERVE)
            _read_engine = create_async_engine(
                engine.url, pool_size=pool_size, max_overflow=0,
                pool_timeout=1, pool_pre_ping=True, **PG_JSON_ARGUMENTS,
                connect_args={"server_settings": {
                    **PG_SERVER_SETTINGS, "application_name": "openbox-trace-read",
                    "default_transaction_read_only": "on", "lock_timeout": "1000",
                    "idle_in_transaction_session_timeout": "5000", "work_mem": "4MB",
                    "max_parallel_workers_per_gather": "0",
                }},
            )
            _read_factory = async_sessionmaker(_read_engine, expire_on_commit=False)
        return _read_factory

    async def execute(self, statement, *args, **kwargs):
        if not getattr(statement, "is_select", False) or getattr(statement, "_for_update_arg", None) is not None:
            raise ValueError("TraceReader accepts unlocked SELECT statements only")
        async with self._factory()() as session:
            async with session.begin():
                result = await session.execute(statement, *args, **kwargs)
                buffered = result.freeze()
        return buffered()

    async def scalars(self, statement, *args, **kwargs):
        return (await self.execute(statement, *args, **kwargs)).scalars()

    async def scalar(self, statement, *args, **kwargs):
        return (await self.execute(statement, *args, **kwargs)).scalar()

    async def get(self, entity, ident, **kwargs):
        if kwargs.get("with_for_update") is not None and kwargs.get("with_for_update") is not False:
            raise ValueError("TraceReader does not acquire row locks")
        async with self._factory()() as session:
            async with session.begin():
                result = await session.get(entity, ident, **kwargs)
        return result


@asynccontextmanager
async def trace_read_session() -> AsyncIterator[TraceReader]:
    """Read facade; the context itself never holds a database connection."""
    get_trace_engine()
    yield TraceReader()


async def close_trace_engine() -> None:
    """Dispose the trace engine and reset the singletons; safe to call twice."""
    global _engine, _session_factory, _read_engine, _read_factory
    reader, _read_engine, _read_factory = _read_engine, None, None
    if reader is not None:
        await reader.dispose()
    if _engine is None:
        return
    engine, _engine, _session_factory = _engine, None, None
    await engine.dispose()
    log.info("Trace database engine closed")


def trace_dialect() -> str:
    """``"postgresql"`` or ``"sqlite"``: the dialect of the initialized trace engine."""
    return get_trace_engine().dialect.name
