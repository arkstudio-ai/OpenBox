"""Single-writer lock of the trace database (SPEC §8.1).

Exactly one worker ingests, projects and archives per trace database. On
PostgreSQL the writer holds a session-level advisory lock on a dedicated
connection for its lifetime: the lock disappears with the session, so a
worker whose connection died loses it and must stop writing (``verify``). On
SQLite an ``flock`` on a file next to the database plays the same role; an
in-memory database can only be shared inside one process, so a process-local
lock suffices there.

``acquire`` never waits: a worker without the lock serves reads and retries.
"""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.log import create_logger

log = create_logger("trajectory.worker.lock")

LOCK_NAME = "openbox-trajectory-writer"
LOCK_FILE_SUFFIX = ".writer.lock"


class WriterLock:
    """``acquire`` tries once; ``verify`` confirms the lock is still held; ``release`` is idempotent."""

    held: bool = False

    async def acquire(self) -> bool:
        raise NotImplementedError

    async def verify(self) -> bool:
        return self.held

    async def release(self) -> None:
        raise NotImplementedError


class PostgresWriterLock(WriterLock):
    """``pg_try_advisory_lock(hashtext(name))`` on its own autocommit connection (NullPool)."""

    def __init__(self, url: URL | str, *, name: str = LOCK_NAME):
        self.url = make_url(url) if isinstance(url, str) else url
        self.name = name
        self.held = False
        self._engine: AsyncEngine | None = None
        self._connection: AsyncConnection | None = None

    async def acquire(self) -> bool:
        if self.held and await self.verify():
            return True
        await self._close()
        engine = create_async_engine(self.url, poolclass=NullPool, connect_args={"server_settings": {
            "application_name": "openbox-trace-writer", "statement_timeout": "5000"}})
        connection = None
        try:
            connection = await engine.connect()
            # Autocommit: the lock belongs to the session, and an idle open
            # transaction would pin old row versions for the process lifetime.
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            acquired = bool((await connection.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": self.name})).scalar())
        except Exception as exc:
            log.warning("Trace writer lock unavailable error_type=%s", type(exc).__name__)
            acquired = False
        if not acquired:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    pass
            await engine.dispose()
            return False
        self._engine, self._connection, self.held = engine, connection, True
        log.info("Acquired the trace writer lock")
        return True

    async def verify(self) -> bool:
        if not self.held or self._connection is None:
            return False
        try:
            count = (await self._connection.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted"
            ))).scalar()
        except Exception as exc:
            log.warning("Trace writer lock connection lost error_type=%s", type(exc).__name__)
            count = 0
        if not count:
            self.held = False
            await self._close()
            return False
        return True

    async def release(self) -> None:
        if self.held and self._connection is not None:
            try:
                await self._connection.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": self.name})
            except Exception as exc:
                log.warning("Trace writer lock release failed error_type=%s", type(exc).__name__)
        self.held = False
        await self._close()

    async def _close(self) -> None:
        connection, engine = self._connection, self._engine
        self._connection = self._engine = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass
        if engine is not None:
            await engine.dispose()


class FileWriterLock(WriterLock):
    """An exclusive, non-blocking ``flock`` held on an open descriptor (``msvcrt`` on Windows)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.held = False
        self._fd: int | None = None

    async def acquire(self) -> bool:
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            log.warning("Trace writer lock file unavailable error_type=%s", type(exc).__name__)
            return False
        if not _lock_descriptor(fd):
            os.close(fd)
            return False
        self._fd, self.held = fd, True
        log.info("Acquired the trace writer lock %s", self.path)
        return True

    async def verify(self) -> bool:
        return self._fd is not None

    async def release(self) -> None:
        fd, self._fd, self.held = self._fd, None, False
        if fd is None:
            return
        try:
            _unlock_descriptor(fd)
        finally:
            os.close(fd)


def _lock_descriptor(fd: int) -> bool:
    try:
        import fcntl
    except ImportError:  # Windows desktop
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        # flock locks belong to the open file description, so a second lock
        # object in the same process conflicts too (fcntl record locks would not).
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_descriptor(fd: int) -> None:
    try:
        import fcntl
    except ImportError:
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class ProcessWriterLock(WriterLock):
    """For in-memory SQLite: one holder per database name inside this process."""

    _registry: dict[str, threading.Lock] = {}
    _registry_lock = threading.Lock()

    def __init__(self, key: str):
        self.key = key
        self.held = False
        with self._registry_lock:
            self._lock = self._registry.setdefault(key, threading.Lock())

    async def acquire(self) -> bool:
        if not self.held:
            self.held = self._lock.acquire(blocking=False)
        return self.held

    async def release(self) -> None:
        if self.held:
            self.held = False
            self._lock.release()


def writer_lock_for(engine: AsyncEngine) -> WriterLock:
    """The lock kind that fits the trace engine's database."""
    url = engine.url
    if engine.dialect.name == "postgresql":
        return PostgresWriterLock(url)
    database = url.database or ""
    if not database or database == ":memory:" or "mode=memory" in database or database.startswith("file::memory:"):
        return ProcessWriterLock(url.render_as_string(hide_password=True))
    path = Path(database).expanduser().resolve()
    return FileWriterLock(path.with_name(path.name + LOCK_FILE_SUFFIX))


class ObjectGuard:
    """Keeps blob deletion by the GC queue apart from ingest batches that store or reuse blobs.

    ``RetentionService.process_gc_queue`` checks that no available payload row references a key and then
    deletes the object, without a database lock across the two steps; an ingest batch that stored or reused
    the object and committed a reference in between would point at a deleted object. The worker therefore
    deletes a blob key only inside ``exclusive()``, checking again right before the delete
    (``services.GuardedGcBlobStore``), and an ingest batch holds ``shared()`` from its check of queued GC
    keys through its commit (``ingest.IngestService``). Each delete then sees either the committed reference
    or no batch in flight. GC and ingest run only in the process that holds the writer lock, so a
    process-local guard is enough.

    ``exclusive()`` waits for the current holders while new ``shared()`` callers wait behind it, so deletes
    cannot starve; it covers one check and one delete, so a batch waits at most that long. Leaving either
    side never awaits, so a cancelled holder cannot keep the guard. A task that holds the guard must not
    enter it again.
    """

    def __init__(self):
        self._gate = asyncio.Lock()
        self._holders = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def holders(self) -> int:
        return self._holders

    @asynccontextmanager
    async def shared(self):
        async with self._gate:
            self._holders += 1
            self._idle.clear()
        try:
            yield
        finally:
            self._holders -= 1
            if not self._holders:
                self._idle.set()

    @asynccontextmanager
    async def exclusive(self):
        async with self._gate:
            await self._idle.wait()
            yield
