"""Metadata controls and meta-assisted ownership checks (SPEC §3.4, §8.3, §8.5).

The worker never reads the business database. ``*.meta`` controls from the
backend's meta sync keep replicas of sessions, users, workspaces and assets in
the trace database; ingest uses them to reject events for deleted or foreign
sessions and to resolve asset references. Unknown metadata never rejects an
event (the replica may lag the event).

All functions run inside the caller's ingest transaction and never commit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from trajectory.store.models import (TrajectoryMetaAsset, TrajectoryMetaSession, TrajectoryMetaUser,
    TrajectoryMetaWorkspace)
from trajectory.worker.content import AssetView

MAX_ANCESTRY_HOPS = 100
ID_CHARS = 64
QUERY_CHUNK = 500
DELETED, OWNERSHIP = "deleted", "ownership"

#: control type -> (model, record key, {column: max characters or None for unbounded text})
_STRINGS = {
    "session.meta": (TrajectoryMetaSession, "session", {
        "user_id": ID_CHARS, "workspace_id": ID_CHARS, "project_id": ID_CHARS, "parent_id": ID_CHARS,
        "kind": 32, "title": None, "status": 32, "model": 128, "agent": 64}),
    "user.meta": (TrajectoryMetaUser, "user", {"username": 64, "email": 255, "role": 32}),
    "workspace.meta": (TrajectoryMetaWorkspace, "workspace", {"name": 128}),
    "asset.meta": (TrajectoryMetaAsset, "asset", {
        "user_id": ID_CHARS, "workspace_id": ID_CHARS, "session_id": ID_CHARS, "oss_key": None,
        "mime": 128, "status": 32}),
}
_FLAGS = {"session.meta": ("is_deleted",), "user.meta": ("is_active", "is_deleted"), "workspace.meta": (),
          "asset.meta": ("is_deleted",)}
_TIMES = {"session.meta": ("deleted_at", "created_at"), "user.meta": (), "workspace.meta": (),
          "asset.meta": ("deleted_at",)}
_REQUIRED = {"session.meta": ("user_id",), "asset.meta": ("user_id",), "user.meta": (), "workspace.meta": ()}
META_CONTROLS = tuple(_STRINGS)


def parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    try:
        return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    except OverflowError:
        # 9999-12-31T23:00:00-05:00 is past datetime.max in UTC: a value no column can hold, not a crash.
        return None


def _identifier(value) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= ID_CHARS else None


def _string(value, limit: int | None) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", "")
    return text if limit is None else text[:limit]


@dataclass(frozen=True)
class SessionView:
    id: str
    user_id: str
    workspace_id: str | None
    parent_id: str | None
    deleted: bool


def _session_view(row: TrajectoryMetaSession) -> SessionView:
    return SessionView(row.id, row.user_id, row.workspace_id, row.parent_id, bool(row.is_deleted))


def _asset_view(row: TrajectoryMetaAsset) -> AssetView:
    return AssetView(row.id, row.user_id, row.workspace_id, row.oss_key, row.mime, row.size, bool(row.is_deleted))


class MetaCache:
    """Replica rows read during one ingest transaction; ``None`` records a known miss."""

    def __init__(self):
        self.sessions: dict[str, SessionView | None] = {}
        self.assets: dict[str, AssetView | None] = {}

    async def load_sessions(self, db, ids) -> None:
        missing = [value for value in dict.fromkeys(ids) if value and value not in self.sessions]
        for offset in range(0, len(missing), QUERY_CHUNK):
            chunk = missing[offset:offset + QUERY_CHUNK]
            rows = (await db.scalars(select(TrajectoryMetaSession).where(TrajectoryMetaSession.id.in_(chunk)))).all()
            found = {row.id: _session_view(row) for row in rows}
            for value in chunk:
                self.sessions[value] = found.get(value)

    async def load_assets(self, db, ids) -> None:
        missing = [value for value in dict.fromkeys(ids) if value and value not in self.assets]
        for offset in range(0, len(missing), QUERY_CHUNK):
            chunk = missing[offset:offset + QUERY_CHUNK]
            rows = (await db.scalars(select(TrajectoryMetaAsset).where(TrajectoryMetaAsset.id.in_(chunk)))).all()
            found = {row.id: _asset_view(row) for row in rows}
            for value in chunk:
                self.assets[value] = found.get(value)

    async def session(self, db, session_id: str) -> SessionView | None:
        if session_id not in self.sessions:
            await self.load_sessions(db, [session_id])
        return self.sessions.get(session_id)

    def known_assets(self) -> dict[str, AssetView]:
        return {key: value for key, value in self.assets.items() if value is not None}


async def ownership_verdict(db, cache: MetaCache, *, user_id: str, root_session_id: str,
                            source_session_id: str | None) -> str | None:
    """``None`` to accept, ``"deleted"`` or ``"ownership"`` to drop the event."""
    root = await cache.session(db, root_session_id)
    if root is not None:
        if root.deleted:
            return DELETED
        if root.user_id != user_id:
            return OWNERSHIP
    current = source_session_id
    seen: set[str] = set()
    while current and current != root_session_id:
        if current in seen or len(seen) >= MAX_ANCESTRY_HOPS:
            return OWNERSHIP
        seen.add(current)
        view = await cache.session(db, current)
        if view is None:
            return None  # unknown chain: metadata may lag the event
        if view.deleted:
            return DELETED
        if view.user_id != user_id:
            return OWNERSHIP
        if view.parent_id is None:
            return OWNERSHIP  # the known chain ends without reaching the root
        current = view.parent_id
    return None


async def apply_meta(db, cache: MetaCache, control_type: str, control: dict, *, line_time: datetime,
                     now: datetime) -> bool:
    """Upsert one replica row; last writer wins by ``updated_at`` (older records are ignored).

    A record without ``updated_at`` uses the spool line time. On equal
    timestamps the record applies but never clears a deleted flag.
    """
    model, record_key, strings = _STRINGS[control_type]
    record = control.get(record_key)
    if not isinstance(record, dict):
        return False
    identifier = _identifier(record.get("id"))
    if identifier is None:
        return False
    values = {}
    for column, limit in strings.items():
        if column in record:
            values[column] = _identifier(record[column]) if limit == ID_CHARS else _string(record[column], limit)
    if any(not values.get(column) for column in _REQUIRED[control_type]):
        return False
    for column in _FLAGS[control_type]:
        if column in record:
            values[column] = bool(record[column])
    for column in _TIMES[control_type]:
        if column in record:
            values[column] = parse_time(record[column])
    if control_type == "asset.meta" and "size" in record:
        size = record["size"]
        values["size"] = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
    updated_at = parse_time(record.get("updated_at")) or line_time
    row = await db.get(model, identifier)
    if row is None:
        row = model(id=identifier, updated_at=updated_at, synced_at=now, **values)
        db.add(row)
    else:
        previous = parse_time(row.updated_at)
        if previous is not None and updated_at < previous:
            return False
        for column, value in values.items():
            if column == "is_deleted" and row.is_deleted and not value and updated_at == previous:
                continue
            setattr(row, column, value)
        row.updated_at, row.synced_at = updated_at, now
    await db.flush()
    if control_type == "session.meta":
        cache.sessions[identifier] = _session_view(row)
    elif control_type == "asset.meta":
        cache.assets[identifier] = _asset_view(row)
    return True


async def mark_session_deleted(db, cache: MetaCache, *, session_id: str, user_id: str | None,
                               deleted_at: datetime, now: datetime) -> None:
    """``session.deleted``: the replica row turns deleted, created when the sync has not delivered it yet."""
    row = await db.get(TrajectoryMetaSession, session_id)
    if row is None:
        if not _identifier(user_id):
            return
        row = TrajectoryMetaSession(id=session_id, user_id=user_id, is_deleted=True, deleted_at=deleted_at,
                                    updated_at=deleted_at, synced_at=now)
        db.add(row)
    else:
        row.is_deleted = True
        row.deleted_at = row.deleted_at or deleted_at
        previous = parse_time(row.updated_at)
        row.updated_at = deleted_at if previous is None or deleted_at > previous else row.updated_at
        row.synced_at = now
    await db.flush()
    cache.sessions[session_id] = _session_view(row)


async def mark_asset_deleted(db, cache: MetaCache, *, asset_id: str, user_id: str | None, deleted_at: datetime,
                             now: datetime) -> None:
    """``asset.deleted``: the replica row turns deleted, created when the sync has not delivered it yet.

    The payload side of the control (references revoked in every trajectory,
    copies queued for GC) is ``trajectory.lifecycle.revoke_asset``.
    """
    row = await db.get(TrajectoryMetaAsset, asset_id)
    if row is None:
        if not _identifier(user_id):
            return
        row = TrajectoryMetaAsset(id=asset_id, user_id=user_id, is_deleted=True, deleted_at=deleted_at,
                                  updated_at=deleted_at, synced_at=now)
        db.add(row)
    else:
        row.is_deleted = True
        row.deleted_at = row.deleted_at or deleted_at
        row.synced_at = now
    await db.flush()
    cache.assets[asset_id] = _asset_view(row)
