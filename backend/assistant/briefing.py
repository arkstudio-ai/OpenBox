"""The personal assistant's daily briefing (V2 P5, design 8.2 work log).

On the user's request the assistant sends a short briefing every day at a
local time: what finished, what waits for the user, what runs today, what it
newly remembers. The cron tick queues one platform input into the main
conversation per local day; that turn may only read (BRIEFING_TOOLS). The
briefing is part of the one assistant conversation, like any other answer.
"""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, update

from assistant.commands import _authority
from assistant.policy import AssistantError
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.assistant_briefing import AssistantBriefing

log = create_logger("assistant.briefing")

ENTRYPOINT = "daily_briefing"
PROMPT = "请给我今天的简报。"
DEFAULT_TIME = "08:30"
DEFAULT_ZONE = "Asia/Shanghai"
#: A briefing turn reads and reports; it never starts, sends, answers or changes anything.
BRIEFING_TOOLS = frozenset({
    "status.briefing", "status.credits", "status.resources", "status.skills", "status.publishing",
    "tasks.list", "tasks.get", "results.read", "history.read", "sessions.list", "requests.list",
    "requests.get", "projects.list", "projects.brief.read", "schedules.list", "memory.search", "memory.read",
})
SYSTEM = ("This turn is the user's daily briefing, started by their schedule, not a message from them. "
          "Read status.briefing (and other read tools only if needed), then write a short briefing in the "
          "user's language, like a secretary's morning note: one friendly opening line, then what finished "
          "and how it went, what waits for the user (with links), what runs today, and anything you newly "
          "remember about them. Plain words only, no IDs or status codes. Skip empty sections; say so in one "
          "line if nothing happened. Do not start, send, answer or change anything.")


def is_briefing(origin_ref) -> bool:
    return isinstance(origin_ref, dict) and origin_ref.get("entrypoint") == ENTRYPOINT


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


def _view(row: AssistantBriefing | None) -> dict:
    if row is None:
        return {"enabled": False, "time": DEFAULT_TIME, "time_zone": DEFAULT_ZONE, "last_sent_on": None}
    return {"enabled": row.enabled, "time": row.local_time, "time_zone": row.time_zone,
            "last_sent_on": row.last_sent_on, "revision": row.revision}


async def configure(ctx, *, enabled: bool, time: str | None = None, time_zone: str | None = None) -> dict:
    """Turn the daily briefing on or off, or move it, on the user's request."""
    if time is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time):
        raise AssistantError(400, "ASSISTANT_BRIEFING_INVALID", "Use a 24-hour local time such as 08:30")
    if time_zone is not None:
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AssistantError(400, "ASSISTANT_BRIEFING_INVALID", "Unknown time zone") from exc
    now = _now()
    async with get_db_session() as db:
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        row = await db.scalar(select(AssistantBriefing).where(AssistantBriefing.user_id == ctx.user_id,
            AssistantBriefing.workspace_id == ctx.workspace_id).with_for_update())
        if row is None:
            row = AssistantBriefing(id=ascending("briefing"), user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                enabled=enabled, local_time=time or DEFAULT_TIME, time_zone=time_zone or DEFAULT_ZONE,
                revision=1, created_at=now, updated_at=now)
            # Turning it on after today's time does not send a briefing right away.
            local = now.astimezone(ZoneInfo(row.time_zone))
            if local.strftime("%H:%M") >= row.local_time:
                row.last_sent_on = local.date().isoformat()
            db.add(row)
        else:
            changed = (row.enabled != enabled or (time and time != row.local_time)
                       or (time_zone and time_zone != row.time_zone))
            row.enabled = enabled
            row.local_time = time or row.local_time
            row.time_zone = time_zone or row.time_zone
            if changed:
                row.revision += 1
                row.updated_at = now
        await db.flush()
        return {"state": "saved", **_view(row),
                "note": "The briefing arrives in this conversation every day at that local time."
                        if row.enabled else "Daily briefings are off."}


async def setting(*, user_id: str, workspace_id: str) -> dict:
    async with get_db_session() as db:
        return _view(await db.scalar(select(AssistantBriefing).where(
            AssistantBriefing.user_id == user_id, AssistantBriefing.workspace_id == workspace_id)))


async def facts(*, user_id: str, workspace_id: str, main_id: str, hours: int = 24) -> dict:
    """What happened and what waits, for the briefing (read-only)."""
    from db.models.assistant import AssistantTask, TaskResult
    from db.models.cron import CronJob
    from db.models.memory import UserMemory
    from db.models.project import Project
    from memory.policy import active_memory_predicates
    from assistant.request_answers import list_waiting
    from assistant.task_context import task_context
    hours = max(1, min(int(hours), 168))
    now = _now()
    since = now - timedelta(hours=hours)
    async with get_db_session() as db:
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        results = (await db.execute(select(TaskResult, AssistantTask.title, Project.name,
            AssistantTask.execution_session_id).join(AssistantTask, AssistantTask.id == TaskResult.task_id).join(
            Project, Project.id == AssistantTask.project_id).where(
            AssistantTask.assistant_session_id == main_id, AssistantTask.user_id == user_id,
            TaskResult.created_at >= since).order_by(TaskResult.created_at.desc()).limit(20))).all()
        watched = await task_context(db, main)
        memories = list((await db.scalars(select(UserMemory).where(UserMemory.user_id == user_id,
            UserMemory.workspace_id == workspace_id, *active_memory_predicates(),
            or_(UserMemory.created_at >= since, UserMemory.updated_at >= since))
            .order_by(UserMemory.updated_at.desc()).limit(10))).all())
        schedules = list((await db.scalars(select(CronJob).where(CronJob.assistant_session_id == main_id,
            CronJob.enabled.is_(True), CronJob.is_deleted.is_(False), CronJob.next_run_at.is_not(None),
            CronJob.next_run_at <= now + timedelta(hours=24)).order_by(CronJob.next_run_at).limit(10))).all())
    waiting = await list_waiting(user_id=user_id, workspace_id=workspace_id, main_id=main_id, limit=10)
    return {
        "window": {"since": _iso(since), "until": _iso(now), "hours": hours},
        "finished": [{"task": title, "project": project, "outcome": result.outcome,
                      "summary": (result.summary or "")[:400], "at": _iso(result.created_at),
                      "link": f"/app/s/{session_id}"} for result, title, project, session_id in results],
        "watched_now": [{key: item.get(key) for key in ("title", "observed_state", "pending_questions",
                                                       "session_id")} for item in watched["items"]],
        "waiting_for_user": [{"conversation": item["session_title"], "project": item.get("project_name"),
                              "question": item["questions"][0]["question"] if item["questions"] else "",
                              "assistant_may_answer": item["assistant_may_answer"], "link": item["link"]}
                             for item in waiting],
        "remembered": [{"summary": (memory.value or {}).get("summary", "")[:200],
                        "scope": "project" if memory.project_id else "personal"} for memory in memories],
        "scheduled_next_24h": [{"name": job.name, "next_run_at": _iso(job.next_run_at)} for job in schedules],
        "untrusted_data": True,
    }


async def dispatch_due_briefings(*, now=None, limit: int = 200) -> int:
    """Queue today's briefing input for every user whose local time has come."""
    from agent.inbox import accept_inbox_item, schedule_inbox_wake
    from assistant import service
    now = now or _now()
    async with get_db_session() as db:
        rows = list((await db.scalars(select(AssistantBriefing).where(AssistantBriefing.enabled.is_(True))
                                      .order_by(AssistantBriefing.id).limit(limit))).all())
    sent = 0
    for row in rows:
        try:
            local = now.astimezone(ZoneInfo(row.time_zone))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        today = local.date().isoformat()
        if row.last_sent_on == today or local.strftime("%H:%M") < row.local_time:
            continue
        async with get_db_session() as db:
            # One briefing per local day, even with several schedulers.
            claimed = await db.execute(update(AssistantBriefing).where(AssistantBriefing.id == row.id,
                AssistantBriefing.enabled.is_(True), or_(AssistantBriefing.last_sent_on.is_(None),
                AssistantBriefing.last_sent_on != today)).values(last_sent_on=today)
                .execution_options(synchronize_session=False))
            if claimed.rowcount != 1:
                continue
        try:
            main = await service.get_main_session(user_id=row.user_id, workspace_id=row.workspace_id)
            if main is None:
                continue
            await accept_inbox_item(session_id=main.id, user_id=row.user_id, delivery="followup", prompt=PROMPT,
                client_id=f"briefing:{row.id}:{today}", agent="assistant", origin="system_recovery",
                origin_ref={"entrypoint": ENTRYPOINT, "date": today, "actor_user_id": row.user_id})
            schedule_inbox_wake(main.id, row.user_id)
            sent += 1
        except Exception:
            log.exception("Daily briefing was not queued briefing=%s", row.id)
    return sent
