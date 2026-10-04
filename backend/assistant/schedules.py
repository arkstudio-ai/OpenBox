"""Bounded cron inventory for the private main, without execution or job bodies."""
from sqlalchemy import exists, or_, select

from assistant.commands import _authority, _project, command_digest
from assistant.policy import AssistantError
from assistant.reads import _page
from assistant.transactions import read_session
from cron.reads import owned_jobs_query
from cron.schedule import as_aware_utc
from db.models.cron import CronJob
from db.models.project import Project
from db.models.session import Session


def _inventory_query(user_id, workspace_id):
    # A deleted/transferred project or notify conversation cannot keep acting
    # as a source through a still-live cron row. No Session is ensured here.
    return owned_jobs_query(user_id, workspace_id).join(Project, Project.id == CronJob.project_id).where(
        Project.user_id == user_id, Project.workspace_id == workspace_id, Project.is_deleted.is_(False),
        or_(CronJob.session_id.is_(None), exists(select(Session.id).where(
            Session.id == CronJob.session_id, Session.user_id == user_id,
            Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
        ))))


def _schedule(value):
    """Allowlist clock fields; unknown JSON keys must not carry payload/secrets."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind == "at" and isinstance(value.get("at"), str) and len(value["at"]) <= 128:
        return {"kind": kind, "at": value["at"]}
    if kind == "cron" and isinstance(value.get("expr"), str) and len(value["expr"]) <= 256:
        tz = value.get("tz", "UTC")
        if isinstance(tz, str) and len(tz) <= 128:
            return {"kind": kind, "expr": value["expr"], "tz": tz}
    if kind == "every" and type(value.get("every_ms")) is int and 0 < value["every_ms"] <= 2**53:
        anchor = value.get("anchor_ms")
        if anchor is None or type(anchor) is int and 0 <= anchor <= 2**53:
            return {"kind": kind, "every_ms": value["every_ms"], "anchor_ms": anchor}
    return None


def _view(row):
    return {**{key: getattr(row, key) for key in (
        "id", "name", "project_id", "session_id", "enabled", "total_runs", "total_successes", "total_failures")},
        **{key: as_aware_utc(getattr(row, key)).isoformat() if getattr(row, key) else None
           for key in ("next_run_at", "last_run_at", "created_at", "updated_at")},
        "schedule": _schedule(row.schedule), "running": row.running_at is not None,
        "last_status": row.last_status if row.last_status in {None, "ok", "error", "skipped"} else "unknown"}


async def list_schedules(*, user_id, workspace_id, main_id, project_id=None, query="",
                         enabled=None, limit=50, cursor=None, db=None):
    if (type(limit) is not int or not 1 <= limit <= 50 or not isinstance(query, str) or len(query) > 200
            or enabled is not None and type(enabled) is not bool
            or cursor is not None and (not isinstance(cursor, str) or len(cursor) > 64)):
        raise ValueError("Invalid schedule inventory filter")
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if project_id:
            await _project(db, project_id, user_id, workspace_id)
        statement = _inventory_query(user_id, workspace_id).where(CronJob.id > (cursor or ""))
        if project_id:
            statement = statement.where(CronJob.project_id == project_id)
        if enabled is not None:
            statement = statement.where(CronJob.enabled == enabled)
        if query.strip():
            statement = statement.where(CronJob.name.icontains(query.strip(), autoescape=True))
        rows = list((await db.scalars(statement.order_by(CronJob.id).limit(limit + 1))).all())
        # No task prompt, raw error, history summary, delivery config or URLs.
        # Clock configuration and counters are observations, never TaskResults.
        return _page(rows, limit, _view)


async def sources(db, main, arguments, value):
    resources = []
    if arguments.get("project_id"):
        project = await _project(db, arguments["project_id"], main.user_id, main.workspace_id)
        resources.append({"kind": "project_filter", "id": project.id})
    items = value.get("items")
    if (not isinstance(items, list) or len(items) > 50 or any(not isinstance(item, dict) for item in items)
            or len({item.get("id") for item in items}) != len(items)):
        raise AssistantError(410, "ASSISTANT_BUSINESS_UNVERIFIED", "The schedule observation is unavailable")
    for item in items:
        row = await db.scalar(_inventory_query(main.user_id, main.workspace_id).where(CronJob.id == item.get("id")))
        if row is None:
            raise AssistantError(410, "ASSISTANT_SCHEDULE_UNAVAILABLE", "The original schedule is unavailable")
        scope = {key: getattr(row, key) for key in ("id", "user_id", "workspace_id", "project_id", "session_id")}
        if row.session_id:
            session = await db.get(Session, row.session_id)
            if session is None:
                raise AssistantError(410, "ASSISTANT_SCHEDULE_UNAVAILABLE", "The notify conversation is unavailable")
            scope["notify_scope"] = {key: getattr(session, key) for key in (
                "id", "user_id", "workspace_id", "project_id", "kind", "visibility", "memory_policy")}
        # Renaming, enablement and execution progress do not erase old factual
        # observations. Scope change/revocation does; current reads are refreshed.
        resources.append({"kind": "schedule", "id": row.id, "scope_digest": command_digest(scope)})
    return {"resources": resources, "tasks": []}
