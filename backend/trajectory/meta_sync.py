"""Metadata replicas for the trajectory worker (SPEC §5.8).

The worker keeps its own copies of sessions, users, workspaces and file assets
so that its admin views and ownership checks never read the business
database. This task feeds them from the backend process as content-free
``*.meta`` controls: a snapshot paged by primary key when it starts, then the
rows changed since a cursor. Each interval sends at most four small indexed,
paged queries, also while a snapshot, a rescan or a backlog is pending: every
table with work gets one, and the ones left over continue the tables that
sent a full page.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, tuple_

from trajectory.config import integer, sink
from trajectory.types import iso

log = logging.getLogger(__name__)

PAGE_ROWS = 500
#: file_assets has no updated_at column, so it is rescanned in full instead.
RESCAN_SECONDS = 600
#: Business database queries per TRAJECTORY_META_SYNC_SECONDS interval (SPEC §5.8).
QUERIES_PER_INTERVAL = 4
#: Incremental reads skip rows younger than this: a transaction that stamped
#: updated_at may still be committing, and the cursor must not pass it.
SETTLE_SECONDS = 10

# Outcomes of one table query.
_IDLE = "idle"  # Caught up.
_MORE = "more"  # A full page went out, so more rows are likely pending.
_REFUSED = "refused"  # The emitter refused a row; the rest waits for the next interval.
_FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _value(value):
    if isinstance(value, datetime):
        return iso(value)
    return value


class _Table:
    """Progress of one replicated table."""

    def __init__(self, name: str, control: str, key: str, columns: tuple[str, ...], *, incremental: bool):
        self.name = name
        self.control = control
        self.key = key
        self.columns = columns
        self.incremental = incremental
        # Primary-key position of a running snapshot or rescan; None when idle.
        self.scan_after: str | None = ""
        self.scan_started_at: datetime | None = None
        self.scan_started: float | None = None
        # (updated_at, id) of the last row sent incrementally.
        self.cursor: tuple[datetime, str] | None = None

    def model(self):
        from db.models.file_asset import FileAsset
        from db.models.session import Session
        from db.models.user import User
        from db.models.workspace import Workspace
        return {"sessions": Session, "users": User, "workspaces": Workspace, "file_assets": FileAsset}[self.name]


def _tables() -> list[_Table]:
    return [
        _Table("sessions", "session.meta", "session",
               ("id", "user_id", "workspace_id", "project_id", "parent_id", "kind", "title", "status",
                "model", "agent", "is_deleted", "deleted_at", "created_at", "updated_at"), incremental=True),
        _Table("users", "user.meta", "user",
               ("id", "username", "email", "role", "is_active", "is_deleted", "updated_at"), incremental=True),
        _Table("workspaces", "workspace.meta", "workspace", ("id", "name", "updated_at"), incremental=True),
        _Table("file_assets", "asset.meta", "asset",
               ("id", "user_id", "workspace_id", "session_id", "oss_key", "mime", "size", "status",
                "is_deleted", "deleted_at"), incremental=False),
    ]


class MetaSync:
    def __init__(self, *, page_rows: int = PAGE_ROWS, rescan_seconds: float = RESCAN_SECONDS,
                 clock=time.monotonic, now=_utcnow, sleep=asyncio.sleep):
        self.page_rows = page_rows
        self.rescan_seconds = rescan_seconds
        self.clock = clock
        self.now = now
        self.sleep = sleep
        self.tables = _tables()

    @staticmethod
    def interval() -> int:
        return integer("TRAJECTORY_META_SYNC_SECONDS", 30)

    async def run(self) -> None:
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Trajectory metadata sync failed error_type=%s", type(exc).__name__)
            # Pending rows wait for the next interval too: the query bound holds while catching up.
            await self.sleep(self.interval())

    async def cycle(self) -> bool:
        """One interval's queries, at most ``QUERIES_PER_INTERVAL``; True while rows are pending."""
        from trajectory.emitter import get_emitter
        emitter = get_emitter()
        if emitter is None:
            return False
        budget = QUERIES_PER_INTERVAL
        outcomes: dict[str, str] = {}
        # Every table with work gets a query first, so a snapshot or a long
        # rescan never holds back the changed rows of the other tables.
        for table in self.tables:
            if budget and self._due(table):
                budget -= 1
                outcomes[table.name] = await self._sync(table, emitter)
        # Queries left over continue the tables that sent a full page.
        while budget:
            behind = [table for table in self.tables if outcomes.get(table.name) == _MORE]
            if not behind:
                break
            for table in behind[:budget]:
                budget -= 1
                outcomes[table.name] = await self._sync(table, emitter)
        return any(outcome in (_MORE, _REFUSED) for outcome in outcomes.values())

    def _due(self, table: _Table) -> bool:
        """Incremental tables and running scans query every interval; file_assets when its rescan is due."""
        if table.incremental or table.scan_after is not None:
            return True
        return table.scan_started is None or self.clock() - table.scan_started >= self.rescan_seconds

    async def _sync(self, table: _Table, emitter) -> str:
        try:
            if table.scan_after is None and not table.incremental:
                table.scan_after = ""
            if table.scan_after is not None:
                return await self._scan_page(table, emitter)
            return await self._changed_page(table, emitter)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One unreadable table (say an old desktop schema) must not
            # stall the others; its position is kept for the next interval.
            log.warning("Trajectory metadata sync of %s failed error_type=%s", table.name, type(exc).__name__)
            return _FAILED

    async def _scan_page(self, table: _Table, emitter) -> str:
        from db.base import get_db_session
        model = table.model()
        if table.scan_after == "":
            table.scan_started, table.scan_started_at = self.clock(), self.now()
        read_at = self.now()
        async with get_db_session() as db:
            rows = (await db.execute(select(*(getattr(model, column) for column in table.columns))
                                     .where(model.id > table.scan_after)
                                     .order_by(model.id).limit(self.page_rows))).all()
        for row in rows:
            if not self._emit(table, emitter, row._mapping, read_at):
                return _REFUSED
            table.scan_after = row.id
        if len(rows) == self.page_rows:
            return _MORE
        table.scan_after = None
        if table.incremental and table.cursor is None:
            # Rows changed while the snapshot was paging are sent again.
            table.cursor = (table.scan_started_at - timedelta(seconds=SETTLE_SECONDS), "")
        return _IDLE

    async def _changed_page(self, table: _Table, emitter) -> str:
        from db.base import get_db_session
        model = table.model()
        horizon = self.now() - timedelta(seconds=SETTLE_SECONDS)
        async with get_db_session() as db:
            rows = (await db.execute(select(*(getattr(model, column) for column in table.columns))
                                     .where(tuple_(model.updated_at, model.id) > tuple_(*table.cursor),
                                            model.updated_at <= horizon)
                                     .order_by(model.updated_at, model.id).limit(self.page_rows))).all()
        for row in rows:
            if not self._emit(table, emitter, row._mapping, None):
                return _REFUSED
            table.cursor = (row.updated_at, row.id)
        return _MORE if len(rows) == self.page_rows else _IDLE

    @staticmethod
    def _emit(table: _Table, emitter, row, read_at: datetime | None) -> bool:
        body = {column: _value(row[column]) for column in table.columns}
        if not table.incremental:
            # Without an updated_at column the read time orders replicas:
            # a later rescan supersedes an earlier one, never the reverse.
            body["updated_at"] = iso(read_at)
        return emitter.emit_control({"type": table.control, table.key: body})


_task: asyncio.Task | None = None


def start_meta_sync() -> asyncio.Task | None:
    """Start the process task; ``None`` unless the sink is the spool."""
    global _task
    if sink() != "spool":
        return None
    if _task is not None and not _task.done():
        return _task
    try:
        _task = asyncio.get_running_loop().create_task(MetaSync().run(), name="trajectory-meta-sync")
    except RuntimeError:
        log.warning("Trajectory metadata sync needs a running event loop")
        return None
    return _task


async def stop_meta_sync() -> None:
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task
