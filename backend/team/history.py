"""Indexed run history and billing-ledger aggregates; never replay journals."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import JSON, and_, case, func, or_, select, type_coerce

from db.base import get_db_session
from db.models.billing import UsageEvent
from db.models.session import Session
from db.models.team import TeamRun
from team.errors import TeamError
from team.journal import owned_run


def usage_query(run_ids, actor):
    from team.usage import CATEGORIES
    metadata = type_coerce(UsageEvent.pricing, JSON)["team_attribution"]
    category = case((and_(metadata["run_id"].as_string() == TeamRun.id,
        metadata["member_id"].as_string() == UsageEvent.session_id,
        metadata["category"].as_string().in_(CATEGORIES)), metadata["category"].as_string()),
        else_="unattributed")
    return select(TeamRun.id.label("run_id"), UsageEvent.session_id, UsageEvent.model_id, UsageEvent.kind,
        category.label("category"),
        func.coalesce(func.sum(UsageEvent.credits), 0).label("credits"),
        func.count().label("calls"), func.coalesce(func.sum(UsageEvent.total_tokens), 0).label("tokens"),
        func.count().filter(UsageEvent.credits.is_(None), UsageEvent.status != "pending").label("unpriced"),
        func.count().filter(UsageEvent.status == "pending").label("pending"),
    ).select_from(TeamRun).join(Session, or_(Session.id == TeamRun.root_session_id,
        and_(Session.parent_id == TeamRun.root_session_id, Session.kind == "team_member"))) .join(UsageEvent, UsageEvent.session_id == Session.id).where(
        TeamRun.id.in_(run_ids), TeamRun.owner_user_id == actor.owner_user_id, TeamRun.workspace_id == actor.workspace_id,
        Session.user_id == actor.owner_user_id, Session.workspace_id == actor.workspace_id,
        UsageEvent.user_id == actor.owner_user_id, UsageEvent.workspace_id == actor.workspace_id,
        UsageEvent.created_at >= TeamRun.created_at,
        or_(TeamRun.ended_at.is_(None), UsageEvent.created_at <= TeamRun.ended_at),
    ).group_by(TeamRun.id, UsageEvent.session_id, UsageEvent.model_id, UsageEvent.kind, category)


def totals(rows):
    return {"credits": str(sum((row.credits for row in rows), Decimal(0))),
        "tokens": sum(row.tokens for row in rows), "calls": sum(row.calls for row in rows),
        "unpriced": sum(row.unpriced for row in rows), "pending": sum(row.pending for row in rows)}


def breakdown(rows):
    from team.usage import CATEGORIES
    return [{"category": category, **totals([row for row in rows if row.category == category])}
        for category in (*CATEGORIES, "unattributed")]


async def usage(run_id, actor):
    async with get_db_session() as db:
        await owned_run(db, run_id, actor)
        rows = (await db.execute(usage_query([run_id], actor))).all()
    return {**totals(rows), "categories": breakdown(rows),
        "items": [{"member_id": row.session_id, "model": row.model_id, "kind": row.kind, "category": row.category,
        **totals([row])} for row in rows]}


async def list_runs(actor, *, status=None, project_id=None, template_id=None, session_id=None, cursor=None, limit=50):
    from team.budget import live_model_request
    unknown_cost = select(UsageEvent.id).join(Session, Session.id == UsageEvent.session_id).where(
        or_(Session.id == TeamRun.root_session_id,
            and_(Session.parent_id == TeamRun.root_session_id, Session.kind == "team_member")),
        UsageEvent.user_id == actor.owner_user_id, UsageEvent.workspace_id == actor.workspace_id,
        Session.user_id == actor.owner_user_id, Session.workspace_id == actor.workspace_id,
        UsageEvent.created_at >= TeamRun.created_at,
        or_(TeamRun.ended_at.is_(None), UsageEvent.created_at <= TeamRun.ended_at),
        UsageEvent.credits.is_(None), ~live_model_request(),
    ).correlate(TeamRun).exists()
    attention = case((or_(TeamRun.state == "paused", unknown_cost,
        type_coerce(TeamRun.summary, JSON)["needs_attention"].as_boolean().is_(True)), 1), else_=0)
    query = select(TeamRun, attention.label("attention")).where(TeamRun.owner_user_id == actor.owner_user_id, TeamRun.workspace_id == actor.workspace_id)
    for field, value in ((TeamRun.state, status), (TeamRun.project_id, project_id), (TeamRun.template_id, template_id), (TeamRun.root_session_id, session_id)):
        if value:
            query = query.where(field == value)
    if cursor:
        try:
            rank, identifier = cursor.split(":", 1)
            if rank not in {"0", "1"} or not identifier or len(identifier) > 64:
                raise ValueError("Invalid cursor")
        except ValueError as exc:
            raise TeamError("INVALID_CURSOR", "Reload the run list before continuing.", status=422) from exc
        query = query.where(or_(attention < int(rank), and_(attention == int(rank), TeamRun.id < identifier)))
    async with get_db_session() as db:
        rows = (await db.execute(query.order_by(attention.desc(), TeamRun.id.desc()).limit(limit + 1))).all()
        page = rows[:limit]
        ledger = (await db.execute(usage_query([run.id for run, _ in page], actor))).all() if page else []
        items = []
        for run, rank in page:
            summary = {key: value for key, value in (run.summary or {}).items() if key not in {"team_configuration", "confirmation_id"}}
            summary.update(needs_attention=bool(rank), pause_reason=run.pause_reason)
            items.append({"id": run.id, "title": run.title, "root_session_id": run.root_session_id, "project_id": run.project_id,
                "template_id": run.template_id, "state": run.state, "revision": run.revision, "seq": run.last_seq,
                "summary": summary, "usage": {**totals([row for row in ledger if row.run_id == run.id]),
                    "categories": breakdown([row for row in ledger if row.run_id == run.id])},
                "created_at": run.created_at.isoformat(), "ended_at": run.ended_at.isoformat() if run.ended_at else None})
        return {"items": items, "next_cursor": f"{page[-1].attention}:{page[-1][0].id}" if len(rows) > limit else None}
