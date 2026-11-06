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
from trajectory.payload import Resolver, is_ref, require_content
from trajectory.repository import cursor_decode, cursor_encode
from trajectory.store.models import (TrajectoryMetaSession, TrajectoryMetaUser, TrajectoryRecord, TrajectoryTurnSummary)
from core.log import create_logger
from trajectory.types import TrajectoryError, digest, iso

log = create_logger("trajectory.lite")

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


# -- Credits (business backend) --

async def attach_credits(windows: list[tuple[str, dict]], items: list[dict], *, field: str = "credits") -> None:
    """Fill ``items[i][field]`` from the backend's charged credits for ``windows[i]``; a failure leaves nulls.

    ``windows`` pairs each item's key with its usage window (session or user scope plus from/to).
    """
    if not windows:
        return
    from trajectory.auth import get_backend
    try:
        answer = await get_backend().turn_usage([{"key": key, **window} for key, window in windows])
        credits = answer.get("credits", {}) if isinstance(answer, dict) else {}
    except Exception as exc:
        log.debug("Trajectory credits unavailable error_type=%s", type(exc).__name__)
        return
    for (key, _), item in zip(windows, items):
        value = credits.get(key)
        item[field] = str(value) if value is not None else None


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
    # Private sessions the viewer cannot read are left out of a user's numbers and shown activity, so a page is
    # re-ordered by what it shows; the SQL order only decides which users fall on which page.
    page = sorted((payload for payload, _ in items), key=lambda row: (row["last_activity_at"] or "", row["user"]["user_id"]), reverse=True)
    for row in page:
        row["credits"] = None
    window_from = (activity_from or datetime(2000, 1, 1, tzinfo=timezone.utc)).isoformat()
    window_to = (activity_to or at).isoformat()
    await attach_credits([(row["user"]["user_id"], {"user_id": row["user"]["user_id"], "from_at": window_from, "to_at": window_to})
                          for row in page], page)
    return {"items": page, "next_cursor": next_cursor, "has_more": overflow}


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
                          q=None, cursor=None, limit=50, viewer_id=None, order="desc", at: datetime | None = None):
    """One user's turns across sessions, newest first unless ``order="asc"`` (lite plan §6.3
    ``GET /users/{user_id}/turns``)."""
    activity_from, activity_to = _window(activity_from, activity_to)
    at = at or datetime.now(timezone.utc)
    statuses = [status for status in (statuses or []) if status]
    unknown = set(statuses) - set(turns.STATUSES)
    if unknown:
        raise TrajectoryError(f"Unsupported turn status filter: {', '.join(sorted(unknown))}")
    if order not in ("asc", "desc"):
        raise TrajectoryError(f"Unsupported turn order: {order}")
    oldest_first = order == "asc"
    # Only "asc" joins the digest, so cursors issued before ``order`` existed stay valid.
    filters = digest([user_id, sorted(statuses), iso(activity_from), iso(activity_to), session_id, q]
                     + ([viewer_id] if viewer_id else []) + (["asc"] if oldest_first else []))
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
    ordered = query.order_by(*((Turn.started_at, Turn.turn_id) if oldest_first
                               else (Turn.started_at.desc(), Turn.turn_id.desc())))
    visible = _audience_filter(viewer_id)
    rows = []
    for _ in range(SCAN_ROUNDS):
        batch_query = ordered
        if position is not None:
            key = tuple_(Turn.started_at, Turn.turn_id)
            batch_query = batch_query.where(key > position if oldest_first else key < position)
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
    await attach_credits([(turn.turn_id, {"session_id": turn.session_id, **_window_dict(turn, at)}) for turn, _ in page], items)
    next_cursor = None
    if len(rows) > limit and page:
        last, _ = page[-1]
        next_cursor = cursor_encode([filters, turns.instant(last.started_at).isoformat(), last.turn_id])
    return {"items": items, "next_cursor": next_cursor, "has_more": len(rows) > limit}


# -- One turn --

#: A permission the policy answered by itself: resolved in under this many ms, nobody waited.
AUTO_PERMISSION_MS = 1000


def classify(summary: dict, document: dict, main_agent_id: str | None = None) -> dict:
    """``step_type``, ``label`` and ``group`` of one record summary for the step flow (lite plan §4.3)."""
    kind = summary.get("kind")
    step_type = STEP_TYPES.get(kind, "hidden")
    base = {"step_type": step_type, "label": None, "group": None, "collapsible": False, "transparent": False,
            "tool_id": None}
    if kind == "tool":
        tool_id = turns.tool_id_of(summary)
        resolved = resolve_tool(tool_id, document)
        group = resolved["group"]
        collapse = bool(group and document["groups"].get(group, {}).get("collapse"))
        return {**base, "label": resolved["label"], "group": group, "tool_id": tool_id,
                "collapsible": collapse and summary.get("status") not in turns.STEP_FAILURES,
                "transparent": resolved["transparent"]}
    if kind == "permission" and summary.get("status") == "completed" and (summary.get("duration_ms") or 0) < AUTO_PERMISSION_MS:
        # Policy-approved on the spot: the operator never saw a confirmation card.
        return {**base, "step_type": "hidden"}
    if kind == "agent" and main_agent_id and summary.get("agent_id") == main_agent_id:
        # The main agent's own record (cron runs spawn it explicitly) is the turn, not a subtask.
        return {**base, "step_type": "hidden"}
    return base


async def get_turn(db, trajectory, turn_id: str, *, at: datetime | None = None) -> dict:
    """The summary and classified step list of one turn (lite plan §6.3 ``GET .../turns/{turn_id}``)."""
    require_content(trajectory)
    row = await db.get(TrajectoryTurnSummary, (trajectory.id, turn_id))
    if row is None:
        raise LookupError("Turn not found")
    session = await db.get(TrajectoryMetaSession, trajectory.session_id)
    document = labels()
    rows = (await db.execute(select(TrajectoryRecord.summary, case((TrajectoryRecord.kind.in_(turns.DATA_KINDS), TrajectoryRecord.data),
                                                                    else_=None)).where(
        TrajectoryRecord.trajectory_id == trajectory.id, TrajectoryRecord.turn_id == turn_id)
        .order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id))).all()
    steps = []
    for summary, data in rows:
        enriched = turns.with_data(summary, data)
        enriched.pop("data", None)  # step rows stay summaries; the detail endpoint carries content
        steps.append({**enriched, **classify(enriched, document, row.main_agent_id)})
    summary = turns.payload(row, session_title=session.title if session else None, at=at)
    await attach_credits([(row.turn_id, {"session_id": row.session_id, **_window_dict(row, at)})], [summary])
    prompts = [prompt for prompt in (media_prompt(summary_row, data) for summary_row, data in rows) if prompt]
    prompts = await _expand(db, trajectory, prompts)
    return {"summary": summary, "steps": steps, "through_seq": str(trajectory.projected_seq),
            "labels_version": document.get("version", 0), "media_prompts": prompts}


def _window_dict(row, at) -> dict:
    from_at, to_at = turns.usage_window(row, at=at)
    return {"from_at": from_at, "to_at": to_at}


# -- Generation prompts (lite plan §15) --

#: Tools whose ``prompt`` argument goes verbatim to a generation model, and the settings shown beside it.
#: video_generate sends ``args.prompt`` unchanged (tool/video_production.py ``_resolve_open_submission``);
#: model and resolution left out of the call follow the person's composer choice.
PROMPT_TOOLS = {
    "video_generate": {"medium": "video", "settings": ("model", "resolution", "ratio", "duration", "generate_audio", "seed")},
    "image_gen": {"medium": "image", "settings": ("size", "quality", "n", "output_format")},
}


def _arguments(inner: dict) -> dict | None:
    for key in ("effective_arguments", "requested_arguments", "arguments"):
        value = inner.get(key)
        if isinstance(value, dict) and value:
            return value
    return None


def _inputs(medium: str, args: dict) -> list[dict]:
    if medium == "video":
        return [{"asset_id": str(item.get("asset_id")), "role": item.get("role")}
                for item in args.get("input_assets") or [] if isinstance(item, dict) and item.get("asset_id")]
    inputs = [{"asset_id": str(item), "role": None} for item in args.get("input_images") or [] if isinstance(item, str)]
    if isinstance(args.get("mask_image"), str):
        inputs.append({"asset_id": args["mask_image"], "role": "mask"})
    return inputs


def media_prompt(summary: dict, data) -> dict | None:
    """The generation request a tool record sent: the full prompt and its settings; None for any other record.

    Only a video ``submit`` reaches the model (``validate``/``status``/``wait`` do not). The prompt may still be a
    ``$ref`` envelope when it was stored out of line; ``_expand`` resolves it.
    """
    if summary.get("kind") != "tool" or not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    tool = str(inner.get("tool") or inner.get("name") or summary.get("tool_id") or "")
    spec = PROMPT_TOOLS.get(tool)
    args = _arguments(inner) if spec else None
    if not args or (spec["medium"] == "video" and args.get("action") != "submit"):
        return None
    prompt = args.get("prompt")
    if not prompt or not isinstance(prompt, (str, dict)):
        return None
    settings = {}
    for key in spec["settings"]:
        value = args.get(key)
        if isinstance(value, bool) or value not in (None, "", 0):  # 0 and "" are a schema's unset fields
            settings[key] = value
    return {"record_id": summary.get("record_id"), "tool_id": tool, "medium": spec["medium"],
            "agent_id": summary.get("agent_id"), "status": summary.get("status"),
            "status_reason": summary.get("status_reason"), "started_at": summary.get("started_at"),
            "shot": args.get("shot") if isinstance(args.get("shot"), int) else None,
            "prompt": prompt, "settings": settings, "inputs": _inputs(spec["medium"], args),
            "result_preview": summary.get("result_preview")}


def _has_ref(value) -> bool:
    if isinstance(value, dict):
        return is_ref(value) or any(_has_ref(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_ref(child) for child in value)
    return False


async def _expand(db, trajectory, values: list) -> list:
    """``$ref`` values inside already-trimmed read results replaced by their content (one batched read)."""
    if not values or not _has_ref(values):
        return values
    return await Resolver(db, trajectory.id, through_seq=trajectory.projected_seq).expand_refs(values)


# -- Session replay (lite plan §15) --

REPLAY_KINDS = ("user", "assistant", "question", "tool", "artifact")
#: Characters of one replayed text; longer content stays readable in the turn page and the professional view.
REPLAY_TEXT_CHARS = 20_000


def _clip(text) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= REPLAY_TEXT_CHARS else text[:REPLAY_TEXT_CHARS - 1] + "…"


def _reply_text(data: dict) -> dict | str | None:
    blocks = data.get("blocks")
    if isinstance(blocks, dict):  # stored out of line
        return blocks
    if not isinstance(blocks, list):
        return None
    texts = [block.get("text") for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
    if any(isinstance(text, dict) for text in texts):
        return {"$blocks": texts}
    return "".join(text for text in texts if isinstance(text, str)) or None


def _finish_reply(value) -> str | None:
    if isinstance(value, list):  # resolved out-of-line blocks
        value = "".join(block.get("text") or "" for block in value
                        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
    elif isinstance(value, dict) and isinstance(value.get("$blocks"), list):
        value = "".join(text for text in value["$blocks"] if isinstance(text, str))
    return _clip(value)


def replay_events(rows: list[tuple[dict, object]], main_agent_id: str | None) -> list[dict]:
    """What the person saw in one turn, in order: their message, the main agent's replies, questions, files sent,
    plus every generation prompt (sub-agents' too, marked with ``agent_id``). Values may still hold ``$ref``."""
    events = []
    for summary, data in sorted(rows, key=lambda row: (turns._seq(row[0].get("start_seq")), row[0]["record_id"])):
        data = data if isinstance(data, dict) else {}
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        kind = summary.get("kind")
        base = {"record_id": summary["record_id"], "at": summary.get("started_at"), "status": summary.get("status")}
        main = not main_agent_id or summary.get("agent_id") == main_agent_id
        if kind == "user" and summary.get("status") == "accepted":
            events.append({**base, "type": "user", "text": inner.get("text") or summary.get("preview"),
                           "attachments": turns._has_reference(inner), "source_kind": inner.get("source_kind")})
        elif kind == "assistant" and main:
            text = _reply_text(data)
            if text is None and summary.get("status") != "failed":
                continue
            events.append({**base, "type": "reply", "text": text, "status_reason": summary.get("status_reason")})
        elif kind == "question" and main:
            events.append({**base, "type": "question", "questions": inner.get("questions") or summary.get("preview"),
                           "answers": inner.get("answers")})
        elif kind == "artifact":
            events.append({**base, "type": "artifact", "title": summary.get("title"), "preview": summary.get("preview")})
        elif kind == "tool":
            prompt = media_prompt(summary, data)
            if prompt:
                events.append({**base, "type": "media_prompt", **prompt})
    return events


def _finish_events(events: list[dict]) -> list[dict]:
    for event in events:
        if event["type"] in ("user",):
            event["text"] = _clip(event["text"]) if isinstance(event["text"], str) else None
        elif event["type"] == "reply":
            event["text"] = _finish_reply(event["text"])
        elif event["type"] == "media_prompt" and not isinstance(event["prompt"], str):
            event["prompt"] = None  # content no longer available
    return events


async def session_replay(db, trajectory, *, cursor=None, limit=10, at: datetime | None = None) -> dict:
    """A session's turns oldest first, each as what the person saw (lite plan §15 ``GET /sessions/{id}/replay``)."""
    require_content(trajectory)
    at = at or datetime.now(timezone.utc)
    filters = digest([trajectory.id, "replay"])
    Turn = TrajectoryTurnSummary
    query = select(Turn).where(Turn.trajectory_id == trajectory.id)
    if cursor:
        moment, key = _decode_position(cursor, filters, "Replay")
        query = query.where(tuple_(Turn.started_at, Turn.turn_id) > (moment, key))
    page = (await db.scalars(query.order_by(Turn.started_at, Turn.turn_id).limit(limit + 1))).all()
    has_more = len(page) > limit
    page = page[:limit]
    total = await db.scalar(select(func.count()).select_from(Turn).where(Turn.trajectory_id == trajectory.id))
    session = await db.get(TrajectoryMetaSession, trajectory.session_id)
    user = await db.get(TrajectoryMetaUser, trajectory.user_id) if trajectory.user_id else None
    grouped: dict[str, list] = {}
    if page:
        records = (await db.execute(select(TrajectoryRecord.summary, TrajectoryRecord.data).where(
            TrajectoryRecord.trajectory_id == trajectory.id, TrajectoryRecord.turn_id.in_([row.turn_id for row in page]),
            TrajectoryRecord.kind.in_(REPLAY_KINDS)))).all()
        for summary, data in records:
            grouped.setdefault(summary["turn_id"], []).append((summary, data))
    events = [replay_events(grouped.get(row.turn_id, []), row.main_agent_id) for row in page]
    events = await _expand(db, trajectory, events)
    items = [{"summary": turns.payload(row, session_title=session.title if session else None, at=at),
              "events": _finish_events(turn_events)} for row, turn_events in zip(page, events)]
    await attach_credits([(row.turn_id, {"session_id": row.session_id, **_window_dict(row, at)}) for row in page],
                         [item["summary"] for item in items])
    next_cursor = None
    if has_more and page:
        next_cursor = cursor_encode([filters, turns.instant(page[-1].started_at).isoformat(), page[-1].turn_id])
    return {"session": {"session_id": trajectory.session_id, "title": session.title if session else None,
                        "user_id": trajectory.user_id, "workspace_id": trajectory.workspace_id,
                        "username": user.username if user else None, "email": user.email if user else None},
            "total_turns": int(total or 0), "items": items, "next_cursor": next_cursor, "has_more": has_more,
            "through_seq": str(trajectory.projected_seq)}
