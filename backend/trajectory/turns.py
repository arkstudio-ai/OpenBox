"""Per-turn summaries for the lite admin view (docs/SESSION_TRAJECTORY_LITE_PLAN.md §6.1-6.2).

A turn summary is a pure function of the projected record summaries that share
a ``turn_id``: the projection worker recomputes the rows of every turn a batch
touched after it has written the records, and the backfill rebuilds them from
stored rows. Nothing here reads events or blobs.

``status`` as stored is structural. ``running`` only says that no terminal
fact exists yet; ``effective_status`` turns a stale ``running`` into
``unknown`` for readers, so an abandoned turn is never shown as still going.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select
from sqlalchemy.dialects import postgresql, sqlite

from core.log import create_logger
from trajectory.config import integer
from trajectory.projector import contribution
from trajectory.store.models import TrajectoryRecord, TrajectoryTurnSummary

log = create_logger("trajectory.turns")

STATUSES = ("completed", "completed_with_errors", "failed", "waiting", "running", "cancelled", "unknown")
#: Record kinds whose ``waiting`` status holds the whole turn.
WAITING_KINDS = ("question", "permission", "takeover")
#: Tool and job outcomes that make a finished turn ``completed_with_errors``.
STEP_FAILURES = {"failed", "denied", "timed_out"}
#: Chunk size of the record reads behind a refresh.
CHUNK = 500


def grace_minutes() -> int:
    """Minutes a turn may stay ``running`` without new activity before readers show ``unknown``."""
    return integer("TRAJECTORY_LITE_RUNNING_GRACE_MINUTES", 30)


def instant(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seq(value) -> int:
    return int(value) if value is not None else 0


def _has_reference(value) -> bool:
    if isinstance(value, dict):
        return any(key in value for key in ("$payload", "$media")) or any(_has_reference(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_reference(child) for child in value)
    return False


def tool_id_of(record: dict) -> str:
    """The tool a tool record ran: ``tool_id`` when the caller attached it, else the title (fixtures, old rows)."""
    return str(record.get("tool_id") or record.get("title") or "tool")


#: Record kinds whose ``data`` a refresh reads: the user's attachments, a tool's id.
DATA_KINDS = ("user", "tool")


def with_data(summary: dict, data) -> dict:
    """A summary plus what the lite view needs from the record's data (lite plan §11)."""
    if not isinstance(data, dict):
        return summary
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    row = {**summary}
    if summary.get("kind") == "user":
        row["data"] = inner
    if summary.get("kind") == "tool":
        tool = inner.get("tool") or inner.get("name")
        if tool:
            row["tool_id"] = str(tool)[:128]
    return row


def _text_preview(record: dict) -> str | None:
    preview = record.get("preview")
    return preview if isinstance(preview, str) and preview.strip() and preview != "tool calls" else None


def derive(records: list[dict]) -> dict | None:
    """The summary columns of one turn from its record summaries; None when the list is empty.

    Rules (lite plan §6.2), first match wins:

    1. a question, permission or takeover still waiting -> ``waiting``
    2. the latest run failed, or the last AI output failed -> ``failed``
    3. the latest run cancelled or interrupted without a resume -> ``cancelled``
    4. the turn or its latest run finished -> ``completed`` or, with a failed,
       denied or timed-out tool or job, ``completed_with_errors``
    5. otherwise ``running`` (readers age it into ``unknown``)
    """
    rows = sorted((row for row in records if row.get("turn_id")), key=lambda row: (_seq(row.get("start_seq")), row["record_id"]))
    if not rows:
        return None
    by_kind: dict[str, list[dict]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
    runs = by_kind.get("run", [])
    latest_run = runs[-1] if runs else None
    turn = by_kind.get("turn", [None])[-1]
    users = by_kind.get("user", [])
    user = next((row for row in users if row.get("status") == "accepted"), users[0] if users else None)
    assistants = by_kind.get("assistant", [])
    spoken = [row for row in assistants if _text_preview(row)]
    last_assistant = assistants[-1] if assistants else None

    waiting = [row for kind in WAITING_KINDS for row in by_kind.get(kind, [])
               if row.get("status") in ({"waiting"} if kind != "takeover" else {"pending", "running", "waiting"})]
    tools = by_kind.get("tool", []) + by_kind.get("job", [])
    failed_steps = [row for row in tools if row.get("status") in STEP_FAILURES]
    interrupts = by_kind.get("interrupt", [])
    resumes = by_kind.get("resume", [])
    resumed_after = bool(interrupts and resumes and _seq(resumes[-1]["start_seq"]) > _seq(interrupts[-1]["start_seq"]))

    status_reason = None
    waiting_kind = None
    if waiting:
        status, waiting_kind = "waiting", waiting[-1]["kind"]
    elif latest_run is not None and latest_run.get("status") == "waiting":
        status, waiting_kind = "waiting", "question"
    elif (latest_run is not None and latest_run.get("status") == "failed") or (
            last_assistant is not None and last_assistant.get("status") == "failed"):
        status = "failed"
        source = latest_run if latest_run is not None and latest_run.get("status") == "failed" else last_assistant
        status_reason = source.get("status_reason") or next((row.get("status_reason") for row in failed_steps
                                                             if row.get("status_reason")), None)
    elif (latest_run is not None and latest_run.get("status") in {"cancelled", "interrupted"}) or (
            interrupts and not resumed_after):
        status = "cancelled"
    elif (turn is not None and turn.get("end_seq")) or (latest_run is not None and latest_run.get("end_seq")):
        status = "completed_with_errors" if failed_steps else "completed"
        if failed_steps:
            status_reason = next((row.get("status_reason") for row in failed_steps if row.get("status_reason")), None)
    else:
        status = "running"

    # Keyed by the tool id (``data.tool``); a record's title is the tool's result title ("exit code: 0").
    tool_counts: dict[str, dict[str, int]] = {}
    for row in by_kind.get("tool", []):
        entry = tool_counts.setdefault(tool_id_of(row), {"total": 0, "failed": 0})
        entry["total"] += 1
        entry["failed"] += int(row.get("status") in STEP_FAILURES)
    main_agent_id = (turn or (user if user else rows[0])).get("agent_id")
    # The main agent may have an ``agent`` record of its own (cron runs); only other agents are sub-agents.
    sub_agents = [row for row in by_kind.get("agent", []) if row.get("agent_id") != main_agent_id]

    totals = contribution(None)
    for row in rows:
        for key, value in contribution(row).items():
            totals[key] += value
    request_count = totals["request_count"]
    usage_known = request_count > totals["usage_missing"]

    started = [instant(row.get("started_at")) for row in rows if row.get("started_at")]
    finished = [instant(row.get("finished_at")) for row in rows if row.get("finished_at")]
    started_at = min(started) if started else None
    terminal = status not in {"running", "waiting"}
    finished_at = max(finished) if terminal and finished else None
    last_activity = max(started + finished) if started or finished else None
    duration_ms = int((finished_at - started_at).total_seconds() * 1000) if started_at and finished_at else None

    return {
        "turn_id": rows[0]["turn_id"],
        "started_at": started_at, "finished_at": finished_at, "last_activity_at": last_activity or started_at,
        "duration_ms": max(0, duration_ms) if duration_ms is not None else None,
        "status": status, "status_reason": status_reason, "waiting_kind": waiting_kind,
        "user_preview": _text_preview(user) if user else None,
        "assistant_preview": _text_preview(spoken[-1]) if spoken else None,
        "user_message_id": user.get("message_id") if user else None,
        "assistant_message_id": last_assistant.get("message_id") if last_assistant else None,
        "main_agent_id": main_agent_id,
        "has_attachments": bool(user and _has_reference(user.get("data"))) if user and "data" in user else False,
        "tool_counts": tool_counts,
        "tool_failures": sum(entry["failed"] for entry in tool_counts.values()) + sum(
            1 for row in by_kind.get("job", []) if row.get("status") in STEP_FAILURES),
        "agent_count": len(sub_agents), "job_count": len(by_kind.get("job", [])),
        "artifact_count": len(by_kind.get("artifact", [])), "retry_count": len(by_kind.get("retry", [])),
        "request_count": request_count,
        "input_tokens": totals["input_tokens"] if usage_known else None,
        "output_tokens": totals["output_tokens"] if usage_known else None,
        "usage_complete": totals["usage_missing"] == 0,
        "first_seq": _seq(rows[0]["start_seq"]),
        "last_seq": max(max(_seq(row.get("as_of_seq")), _seq(row.get("end_seq")), _seq(row["start_seq"])) for row in rows),
    }


def effective_status(status: str, last_activity_at, *, at: datetime | None = None, grace: int | None = None) -> str:
    """``unknown`` for a ``running`` turn without activity for ``grace`` minutes (default from the environment)."""
    if status != "running":
        return status
    moment = at or datetime.now(timezone.utc)
    activity = instant(last_activity_at)
    minutes = grace_minutes() if grace is None else grace
    return "unknown" if activity is None or moment - activity > timedelta(minutes=minutes) else "running"


def _insert(db, model):
    return (postgresql.insert if db.bind.dialect.name == "postgresql" else sqlite.insert)(model)


async def refresh(db, trajectory, turn_ids, *, applied_seq: int | None = None) -> int:
    """Recompute and upsert the summary rows of ``turn_ids`` from the stored records; returns rows written.

    A turn whose records vanished (content purge) is deleted. Idempotent: a
    rerun over the same records writes the same row.
    """
    ids = sorted({turn_id for turn_id in turn_ids if turn_id})
    if not ids:
        return 0
    written = 0
    head = applied_seq if applied_seq is not None else trajectory.projected_seq
    for offset in range(0, len(ids), CHUNK):
        chunk = ids[offset:offset + CHUNK]
        # Only user and tool records' data is read (attachments, the tool id); all else comes from the summary.
        rows = (await db.execute(select(TrajectoryRecord.summary, case((TrajectoryRecord.kind.in_(DATA_KINDS), TrajectoryRecord.data),
                                                                        else_=None)).where(
            TrajectoryRecord.trajectory_id == trajectory.id, TrajectoryRecord.turn_id.in_(chunk)))).all()
        grouped: dict[str, list[dict]] = {}
        for summary, data in rows:
            grouped.setdefault(summary["turn_id"], []).append(with_data(summary, data))
        values = []
        for turn_id in chunk:
            derived = derive(grouped.get(turn_id, []))
            if derived is None:
                await db.execute(TrajectoryTurnSummary.__table__.delete().where(
                    TrajectoryTurnSummary.trajectory_id == trajectory.id, TrajectoryTurnSummary.turn_id == turn_id))
                continue
            values.append({"trajectory_id": trajectory.id, "session_id": trajectory.session_id,
                           "user_id": trajectory.user_id, "workspace_id": trajectory.workspace_id,
                           "applied_seq": head, **derived})
        if values:
            statement = _insert(db, TrajectoryTurnSummary).values(values)
            columns = {name: statement.excluded[name] for name in values[0] if name not in {"trajectory_id", "turn_id"}}
            await db.execute(statement.on_conflict_do_update(index_elements=["trajectory_id", "turn_id"], set_=columns))
            written += len(values)
    return written


def payload(row: TrajectoryTurnSummary, *, session_title: str | None = None, at: datetime | None = None,
            grace: int | None = None) -> dict:
    """The API shape of one turn summary (lite plan §6.3 ``TurnSummary``)."""
    from trajectory.types import iso
    return {
        "turn_id": row.turn_id, "session_id": row.session_id, "session_title": session_title,
        "user_id": row.user_id, "workspace_id": row.workspace_id,
        "started_at": iso(row.started_at), "finished_at": iso(row.finished_at),
        "last_activity_at": iso(row.last_activity_at), "duration_ms": row.duration_ms,
        "status": effective_status(row.status, row.last_activity_at, at=at, grace=grace),
        "status_reason": row.status_reason, "waiting_kind": row.waiting_kind,
        "user_preview": row.user_preview, "assistant_preview": row.assistant_preview,
        "user_message_id": row.user_message_id, "assistant_message_id": row.assistant_message_id,
        "main_agent_id": row.main_agent_id, "has_attachments": bool(row.has_attachments),
        "tool_counts": row.tool_counts or {}, "tool_failures": row.tool_failures,
        "agent_count": row.agent_count, "job_count": row.job_count, "artifact_count": row.artifact_count,
        "retry_count": row.retry_count, "request_count": row.request_count,
        "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
        "usage_complete": bool(row.usage_complete),
        "first_seq": str(row.first_seq), "last_seq": str(row.last_seq), "through_seq": str(row.applied_seq),
        # Filled by lite.attach_credits from the business backend; null until then or when nothing was charged.
        "credits": None,
    }


#: Seconds of slack around a turn when matching charges by time: a charge is written just after the call it bills.
USAGE_SLACK_SECONDS = 60
ALERT_BATCH = 50


def usage_window(row: TrajectoryTurnSummary, *, at: datetime | None = None) -> tuple[str, str]:
    """``(from, to)`` ISO instants of the charges a turn can own: its session from start to end, with slack."""
    moment = at or datetime.now(timezone.utc)
    start = instant(row.started_at) or moment
    end = instant(row.finished_at) or instant(row.last_activity_at) or moment
    if row.status in {"running", "waiting"}:
        end = max(end, moment)
    return ((start - timedelta(seconds=5)).isoformat(), (end + timedelta(seconds=USAGE_SLACK_SECONDS)).isoformat())


async def deliver_alerts(db, backend, *, limit: int = ALERT_BATCH, at: datetime | None = None) -> int:
    """Announce newly failed turns to the platform admins through the business backend; returns turns announced.

    Oldest pending first; a backend refusal of the batch (4xx) still stamps the
    rows so a bad turn cannot block the queue, a transport failure leaves them
    for the next pass.
    """
    from sqlalchemy import update
    from trajectory.store.models import TrajectoryMetaSession, TrajectoryMetaUser
    rows = (await db.execute(select(TrajectoryTurnSummary, TrajectoryMetaSession.title, TrajectoryMetaUser.username)
        .outerjoin(TrajectoryMetaSession, TrajectoryMetaSession.id == TrajectoryTurnSummary.session_id)
        .outerjoin(TrajectoryMetaUser, TrajectoryMetaUser.id == TrajectoryTurnSummary.user_id)
        .where(TrajectoryTurnSummary.status == "failed", TrajectoryTurnSummary.alerted_at.is_(None))
        .order_by(TrajectoryTurnSummary.started_at).limit(limit))).all()
    if not rows:
        return 0
    payload = [{"turn_id": row.turn_id, "session_id": row.session_id, "user_id": row.user_id,
                "workspace_id": row.workspace_id, "username": username, "session_title": title,
                "reason": (row.status_reason or "")[:500] or None,
                "started_at": instant(row.started_at).isoformat() if row.started_at else None}
               for row, title, username in rows]
    try:
        await backend.turn_alerts(payload)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None or status >= 500:
            raise
        log.warning("Trajectory turn alerts refused status=%s turns=%d", status, len(payload))
    stamp = at or datetime.now(timezone.utc)
    for row, _, _ in rows:
        await db.execute(update(TrajectoryTurnSummary).where(
            TrajectoryTurnSummary.trajectory_id == row.trajectory_id, TrajectoryTurnSummary.turn_id == row.turn_id)
            .values(alerted_at=stamp))
    return len(rows)
