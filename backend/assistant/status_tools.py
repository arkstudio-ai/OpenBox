"""Read-only status of the user's other OpenBox resources (V2 P4, design 12).

The assistant can tell the user where things stand: credits, cloud desktop and
browser, installed skills, recent publishing. It never starts, stops, buys,
installs or publishes anything; those stay with the user's own actions in the
product. Values are current reads, not remembered facts.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select

from assistant.commands import _authority
from db.base import get_db_session

MONTH_ZONE = "Asia/Shanghai"


def _iso(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


async def credits(*, user_id: str, workspace_id: str, main_id: str) -> dict:
    """Workspace balance (as on the billing page) and the user's own usage this month."""
    from billing.service import billing_mode, lock_balance
    from billing.subscriptions import ensure_period_allowance
    from db.models.billing import UsageEvent
    from session.policy import usage_audience
    zone = ZoneInfo(MONTH_ZONE)
    now = datetime.now(zone)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        account = await lock_balance(db, workspace_id)
        await ensure_period_allowance(db, account)
        totals = (await db.execute(select(
            func.coalesce(func.sum(UsageEvent.total_tokens), 0),
            func.coalesce(func.sum(case((UsageEvent.status == "charged", UsageEvent.credits), else_=0)), 0),
        ).where(UsageEvent.workspace_id == workspace_id, UsageEvent.user_id == user_id,
                UsageEvent.created_at >= start, usage_audience(user_id, UsageEvent)))).one()
        return {"workspace_balance": str(account.balance), "billing_mode": billing_mode(),
                "this_month": {"since": start.astimezone(zone).date().isoformat(), "time_zone": MONTH_ZONE,
                               "my_tokens": int(totals[0] or 0), "my_charged_credits": str(totals[1] or 0)},
                "note": "The billing page has plans, orders and the full ledger; buying is done there."}


async def resources(*, user_id: str, workspace_id: str, main_id: str) -> dict:
    """The workspace cloud desktop's provisioning state and the user's browser mode."""
    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    from session.browser_pref import get_browser_mode
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    record = await cloud_desktop_repo.get_for_workspace(workspace_id)
    desktop = {"state": "not_provisioned"}
    if record:
        desktop = {"state": "assigning" if record.get("pool_state") == "assigning" else record.get("status"),
                   "updated_at": _iso(record.get("updated_at"))}
        if record.get("status") == "failed" and record.get("error"):
            desktop["error"] = str(record["error"])[:300]
    return {"cloud_desktop": desktop, "browser_preference": await get_browser_mode(user_id),
            "note": "Starting, stopping or buying resources is done by the user in the product (云桌面 / 资源中心)."}


async def skills(*, user_id: str, workspace_id: str, main_id: str) -> dict:
    """Skills available to the user's agents: platform skills and their own library."""
    from skill.skill import list_skills as host_skills
    from skill.user_library import list_owned_skills
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    owned = await list_owned_skills(user_id, workspace_id)
    platform = await host_skills()
    return {"my_skills": [{"name": item["name"], "description": str(item.get("description") or "")[:200]}
                          for item in owned[:50]],
            "platform_skills": [{"name": item.name, "description": str(item.description or "")[:200]}
                                for item in platform[:50]],
            "note": "Installing or removing skills is done by the user in 技能中心."}


async def publishing(*, user_id: str, workspace_id: str, main_id: str, limit: int = 10) -> dict:
    """The user's most recent publishing jobs and their state."""
    from db.models.publish_job import PublishJob
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        rows = list((await db.scalars(select(PublishJob).where(
            PublishJob.user_id == user_id, PublishJob.workspace_id == workspace_id)
            .order_by(PublishJob.created_at.desc()).limit(limit))).all())
    return {"jobs": [{"platform": row.platform, "title": row.title, "status": row.status,
                      "created_at": _iso(row.created_at), "published_at": _iso(row.published_at),
                      **({"error": str(row.error)[:300]} if row.error else {})} for row in rows],
            "note": "Publishing is carried out by a task conversation under its own approvals."}
