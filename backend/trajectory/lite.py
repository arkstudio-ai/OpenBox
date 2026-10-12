"""Reads behind the lite admin view: users, a user's turns, one turn's steps, the label dictionary.

Every function works on the trace database only (``trajectory_turn_summaries``,
``trajectory_records`` and the metadata replicas). Audience filtering follows
``repository.list_sessions``: rows are checked through ``visible_sessions``
before a page or a cursor is released, and a viewer-less call (internal use)
skips the check. docs/SESSION_TRAJECTORY_LITE_PLAN.md §6.3.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import and_, case, func, or_, select, tuple_

from trajectory import turns
from trajectory.payload import require_content
from trajectory.repository import cursor_decode, cursor_encode
from trajectory.store.models import (TrajectoryMetaSession, TrajectoryMetaUser, TrajectoryRecord, TrajectoryTurnSummary)
from trajectory.types import TrajectoryError, digest, iso

LABELS_PATH = Path(__file__).resolve().parent / "lite_labels.json"
#: Bounded scans: how many batches a page may consume before the read is refused (as list_sessions).
SCAN_ROUNDS = 25
BATCH = 200
#: Record kinds the step flow shows; everything else is ``hidden``.
STEP_TYPES = {"user": "user_input", "assistant": "assistant", "tool": "tool", "question": "question",
              "permission": "permission", "takeover": "takeover", "agent": "agent", "job": "job",
              "artifact": "artifact", "gap": "gap", "retry": "retry", "interrupt": "control", "resume": "control"}


# -- Labels --

def labels_path() -> Path:
    configured = (os.getenv("TRAJECTORY_LITE_LABELS_PATH") or "").strip()
    return Path(configured) if configured else LABELS_PATH


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    for key in ("tools", "prefixes", "groups", "statuses", "waiting_kinds"):
        document.setdefault(key, {})
    return document


def labels() -> dict:
    """The dictionary as configured; re-read when the file changes (lite plan §10 item 4)."""
    path = labels_path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"version": 0, "tools": {}, "prefixes": {}, "groups": {}, "statuses": {}, "waiting_kinds": {}}
    return _load(str(path), stat.st_mtime)


def resolve_tool(tool_id: str, document: dict | None = None) -> dict:
    """``{label, group, args, result, source}`` for a tool id: exact id, then the longest prefix, then the id itself."""
    document = document if document is not None else labels()
    entry = document["tools"].get(tool_id)
    if entry is not None:
        return {"label": entry.get("label") or tool_id, "group": entry.get("group"), "args": entry.get("args", []),
                "result": entry.get("result"), "transparent": bool(entry.get("transparent")), "source": "tool"}
    for prefix in sorted(document["prefixes"], key=len, reverse=True):
        if tool_id.startswith(prefix):
            entry = document["prefixes"][prefix]
            return {"label": entry.get("label") or tool_id, "group": entry.get("group"), "args": entry.get("args", []),
                    "result": entry.get("result"), "transparent": False, "source": "prefix"}
    return {"label": tool_id, "group": None, "args": [], "result": None, "transparent": False, "source": "default"}


# -- Users --

def _window(activity_from, activity_to):
    if activity_from is not None and activity_to is not None and activity_from > activity_to:
        raise TrajectoryError("activity_from is after activity_to")
    return activity_from, activity_to


def _audience_filter(viewer_id):
    if not viewer_id:
        async def everything(targets):
            return {target["session_id"] for target in targets}
        return everything
    from trajectory.audience import visible_sessions

    async def visible(targets):
        allowed = set()
        for offset in range(0, len(targets), BATCH):
            allowed |= await visible_sessions(viewer_id, targets[offset:offset + BATCH])
        return allowed
    return visible


def _flag(condition):
    return func.sum(case((condition, 1), else_=0))


def _in_window(query, column, activity_from, activity_to):
    if activity_from is not None:
        query = query.where(column >= activity_from)
    if activity_to is not None:
        query = query.where(column <= activity_to)
    return query


def _user_payload(user: TrajectoryMetaUser | None, user_id: str, sessions: list[dict], at: datetime) -> dict:
    latest = max(sessions, key=lambda row: (row["last_activity_at"], row["session_id"]))
    known = [row for row in sessions if row["input_tokens"] is not None]
    return {
        "user": {"user_id": user_id, "username": user.username if user else None, "email": user.email if user else None},
        "last_activity_at": iso(latest["last_activity_at"]),
        "current_status": turns.effective_status(latest["latest_status"], latest["latest_activity_at"], at=at),
        "turns": sum(row["turns"] for row in sessions),
        "failed_turns": sum(row["failed"] for row in sessions),
        "waiting_turns": sum(row["waiting"] for row in sessions),
        "running_turns": sum(row["running"] for row in sessions),
        "sessions": len(sessions),
        "input_tokens": sum(row["input_tokens"] for row in known) if known else None,
        "output_tokens": sum(row["output_tokens"] for row in known) if known else None,
        "usage_complete": all(row["usage_complete"] for row in sessions),
    }


async def _session_aggregates(db, user_ids: list[str], activity_from, activity_to) -> dict[str, list[dict]]:
    """Per (user, session) counters in the window, grouped by user; bounded by those users' sessions."""
    Turn = TrajectoryTurnSummary
    query = select(Turn.user_id, Turn.session_id, Turn.workspace_id, func.count().label("turns"),
                   _flag(Turn.status.in_(["failed", "completed_with_errors"])).label("failed"),
                   _flag(Turn.status == "waiting").label("waiting"), _flag(Turn.status == "running").label("running"),
                   func.max(Turn.last_activity_at).label("last_activity_at"),
                   func.sum(Turn.input_tokens).label("input_tokens"), func.sum(Turn.output_tokens).label("output_tokens"),
                   func.count(Turn.input_tokens).label("usage_rows"),
                   _flag(Turn.usage_complete.is_(False)).label("usage_incomplete")).where(Turn.user_id.in_(user_ids))
    query = _in_window(query, Turn.started_at, activity_from, activity_to).group_by(Turn.user_id, Turn.session_id, Turn.workspace_id)
    # The newest turn of each session decides the session's current status.
    ranked = _in_window(select(Turn.user_id, Turn.session_id, Turn.status, Turn.last_activity_at,
                               func.row_number().over(partition_by=(Turn.user_id, Turn.session_id),
                                                      order_by=(Turn.last_activity_at.desc(), Turn.turn_id.desc())).label("rank"))
                        .where(Turn.user_id.in_(user_ids)), Turn.started_at, activity_from, activity_to).subquery()
    latest = {(row.user_id, row.session_id): (row.status, row.last_activity_at) for row in (await db.execute(
        select(ranked.c.user_id, ranked.c.session_id, ranked.c.status, ranked.c.last_activity_at).where(ranked.c.rank == 1))).all()}
    grouped: dict[str, list[dict]] = {}
    for row in (await db.execute(query)).all():
        status, activity = latest.get((row.user_id, row.session_id), ("unknown", row.last_activity_at))
        grouped.setdefault(row.user_id, []).append({
            "session_id": row.session_id, "workspace_id": row.workspace_id, "turns": int(row.turns),
            "failed": int(row.failed or 0), "waiting": int(row.waiting or 0), "running": int(row.running or 0),
            "last_activity_at": turns.instant(row.last_activity_at), "latest_status": status,
            "latest_activity_at": turns.instant(activity),
            "input_tokens": int(row.input_tokens) if row.usage_rows else None,
            "output_tokens": int(row.output_tokens) if row.usage_rows else None,
            "usage_complete": not int(row.usage_incomplete or 0)})
    return grouped


def _decode_position(cursor, filters, what):
    cursor_filters, cursor_time, cursor_key = cursor_decode(cursor)
    if cursor_filters != filters:
        raise TrajectoryError(f"{what} cursor belongs to another filter")
    try:
        return turns.instant(str(cursor_time)), str(cursor_key)
    except (TypeError, ValueError) as exc:
        raise TrajectoryError(f"Invalid {what.lower()} cursor") from exc


async def list_users(db, *, q=None, activity_from=None, activity_to=None, has_failures=False, active_only=False,
                     cursor=None, limit=50, viewer_id=None, at: datetime | None = None):
    """Users with recorded turns in the window, most recent activity first (lite plan §6.3 ``GET /users``)."""
    activity_from, activity_to = _window(activity_from, activity_to)
    at = at or datetime.now(timezone.utc)
    filters = digest([q, iso(activity_from), iso(activity_to), has_failures, active_only] + ([viewer_id] if viewer_id else []))
    Turn, User = TrajectoryTurnSummary, TrajectoryMetaUser
    activity = func.max(Turn.last_activity_at).label("last_activity_at")
    query = _in_window(select(Turn.user_id, activity).outerjoin(User, User.id == Turn.user_id),
                       Turn.started_at, activity_from, activity_to)
    if q:
        needle = f"%{q}%"
        query = query.where(or_(User.username.ilike(needle), User.email.ilike(needle), Turn.user_id.ilike(needle)))
    query = query.group_by(Turn.user_id)
    if has_failures:
        query = query.having(_flag(Turn.status.in_(["failed", "completed_with_errors"])) > 0)
    if active_only:
        query = query.having(_flag(Turn.status.in_(["running", "waiting"])) > 0)
    position = _decode_position(cursor, filters, "User") if cursor else None
    ordered = query.order_by(activity.desc(), Turn.user_id.desc())
    visible = _audience_filter(viewer_id)
    items: list[tuple[dict, tuple]] = []
    overflow = False
    for _ in range(SCAN_ROUNDS):
        batch_query = ordered
        if position is not None:
            batch_query = batch_query.having(tuple_(activity, Turn.user_id) < position)
        batch = (await db.execute(batch_query.limit(BATCH))).all()
        if not batch:
            break
        user_ids = [row.user_id for row in batch]
        aggregates = await _session_aggregates(db, user_ids, activity_from, activity_to)
        targets = [{"session_id": row["session_id"], "user_id": user_id, "workspace_id": row["workspace_id"]}
                   for user_id in user_ids for row in aggregates.get(user_id, [])]
        allowed = await visible(targets)
        users = {row.id: row for row in (await db.scalars(select(User).where(User.id.in_(user_ids)))).all()}
        for row in batch:
            sessions = [session for session in aggregates.get(row.user_id, []) if session["session_id"] in allowed]
            if not sessions:
                continue
            if len(items) == limit:
                overflow = True
                break
            items.append((_user_payload(users.get(row.user_id), row.user_id, sessions, at),
                          (turns.instant(row.last_activity_at), row.user_id)))
        last = batch[-1]
        position = (turns.instant(last.last_activity_at), last.user_id)
        if overflow or len(batch) < BATCH:
            break
    else:
        from fastapi import HTTPException
        raise HTTPException(503, detail="Trajectory audience scan exceeds the read budget")
    next_cursor = None
    if overflow and items:
        # The cursor is the SQL position (the user's windowed activity, unfiltered), not the displayed one.
        _, (moment, user_id) = items[-1]
        next_cursor = cursor_encode([filters, moment.isoformat(), user_id])
    return {"items": [payload for payload, _ in items], "next_cursor": next_cursor, "has_more": overflow}


# -- Turns --

def _status_clause(statuses: list[str], at: datetime):
    """SQL for requested effective statuses: ``running``/``unknown`` split the stored ``running`` by activity age."""
    Turn = TrajectoryTurnSummary
    threshold = at - timedelta(minutes=turns.grace_minutes())
    wanted = set(statuses)
    clauses = []
    stored = wanted - {"running", "unknown"}
    if stored:
        clauses.append(Turn.status.in_(sorted(stored)))
    if "running" in wanted:
        clauses.append(and_(Turn.status == "running", Turn.last_activity_at > threshold))
    if "unknown" in wanted:
        clauses.append(and_(Turn.status == "running", Turn.last_activity_at <= threshold))
    return or_(*clauses) if clauses else None


async def list_user_turns(db, user_id: str, *, statuses=None, activity_from=None, activity_to=None, session_id=None,
                          q=None, cursor=None, limit=50, viewer_id=None, at: datetime | None = None):
    """One user's turns across sessions, newest first (lite plan §6.3 ``GET /users/{user_id}/turns``)."""
    activity_from, activity_to = _window(activity_from, activity_to)
    at = at or datetime.now(timezone.utc)
    statuses = [status for status in (statuses or []) if status]
    unknown = set(statuses) - set(turns.STATUSES)
    if unknown:
        raise TrajectoryError(f"Unsupported turn status filter: {', '.join(sorted(unknown))}")
    filters = digest([user_id, sorted(statuses), iso(activity_from), iso(activity_to), session_id, q]
                     + ([viewer_id] if viewer_id else []))
    Turn, Session = TrajectoryTurnSummary, TrajectoryMetaSession
    query = _in_window(select(Turn, Session.title).outerjoin(Session, Session.id == Turn.session_id)
                       .where(Turn.user_id == user_id), Turn.started_at, activity_from, activity_to)
    if session_id:
        query = query.where(Turn.session_id == session_id)
    if q:
        needle = f"%{q}%"
        query = query.where(or_(Turn.user_preview.ilike(needle), Turn.assistant_preview.ilike(needle)))
    if statuses:
        clause = _status_clause(statuses, at)
        if clause is not None:
            query = query.where(clause)
    position = _decode_position(cursor, filters, "Turn") if cursor else None
    ordered = query.order_by(Turn.started_at.desc(), Turn.turn_id.desc())
    visible = _audience_filter(viewer_id)
    rows = []
    for _ in range(SCAN_ROUNDS):
        batch_query = ordered
        if position is not None:
            batch_query = batch_query.where(tuple_(Turn.started_at, Turn.turn_id) < position)
        batch = (await db.execute(batch_query.limit(BATCH))).all()
        if not batch:
            break
        seen = {}
        for turn, _ in batch:
            seen.setdefault(turn.session_id, {"session_id": turn.session_id, "user_id": turn.user_id,
                                               "workspace_id": turn.workspace_id})
        allowed = await visible(list(seen.values()))
        rows.extend((turn, title) for turn, title in batch if turn.session_id in allowed)
        last = batch[-1][0]
        position = (turns.instant(last.started_at), last.turn_id)
        if len(rows) > limit or len(batch) < BATCH:
            break
    else:
        from fastapi import HTTPException
        raise HTTPException(503, detail="Trajectory audience scan exceeds the read budget")
    page = rows[:limit]
    items = [turns.payload(turn, session_title=title, at=at) for turn, title in page]
    next_cursor = None
    if len(rows) > limit and page:
        last, _ = page[-1]
        next_cursor = cursor_encode([filters, turns.instant(last.started_at).isoformat(), last.turn_id])
    return {"items": items, "next_cursor": next_cursor, "has_more": len(rows) > limit}


# -- One turn --

def classify(summary: dict, document: dict) -> dict:
    """``step_type``, ``label`` and ``group`` of one record summary for the step flow (lite plan §4.3)."""
    kind = summary.get("kind")
    step_type = STEP_TYPES.get(kind, "hidden")
    if kind == "tool":
        resolved = resolve_tool(str(summary.get("title") or "tool"), document)
        group = resolved["group"]
        collapse = bool(group and document["groups"].get(group, {}).get("collapse"))
        return {"step_type": "tool", "label": resolved["label"], "group": group,
                "collapsible": collapse and summary.get("status") not in turns.STEP_FAILURES,
                "transparent": resolved["transparent"]}
    return {"step_type": step_type, "label": None, "group": None, "collapsible": False, "transparent": False}


async def get_turn(db, trajectory, turn_id: str, *, at: datetime | None = None) -> dict:
    """The summary and classified step list of one turn (lite plan §6.3 ``GET .../turns/{turn_id}``)."""
    require_content(trajectory)
    row = await db.get(TrajectoryTurnSummary, (trajectory.id, turn_id))
    if row is None:
        raise LookupError("Turn not found")
    session = await db.get(TrajectoryMetaSession, trajectory.session_id)
    document = labels()
    summaries = (await db.scalars(select(TrajectoryRecord.summary).where(
        TrajectoryRecord.trajectory_id == trajectory.id, TrajectoryRecord.turn_id == turn_id)
        .order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id))).all()
    steps = [{**summary, **classify(summary, document)} for summary in summaries]
    return {"summary": turns.payload(row, session_title=session.title if session else None, at=at),
            "steps": steps, "through_seq": str(trajectory.projected_seq),
            "labels_version": document.get("version", 0)}
