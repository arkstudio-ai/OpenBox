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


async def models(*, user_id: str, workspace_id: str, main_id: str) -> dict:
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    from assistant.model_choices import catalogue
    return catalogue()


async def platforms(*, user_id: str, workspace_id: str, main_id: str) -> dict:
    """Bounded authorization-centre snapshot; no cookie/token reads or probes."""
    from db.models.platform_account import PlatformAccount
    from platforms.desktop.sites import list_sites
    from session.browser_pref import get_browser_mode
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        rows = (await db.execute(select(
            PlatformAccount.platform, PlatformAccount.auth_kind, PlatformAccount.status,
            PlatformAccount.nickname, PlatformAccount.last_probe_at, PlatformAccount.updated_at,
        ).where(PlatformAccount.workspace_id == workspace_id, PlatformAccount.deleted_at.is_(None))
          .order_by(PlatformAccount.updated_at.desc()).limit(100))).all()
    sites = {site.key: {"name": site.display, "login_detection": not site.recon_pending}
             for site in list_sites()}
    sites.update({"douyin": {"name": "抖音开放平台授权", "login_detection": True},
                  "wechat_channels": {"name": "视频号", "login_detection": False}})
    mode = await get_browser_mode(user_id)
    return {"status": "ok", "source": "authorization_center", "live_verified": False,
            "connection_conclusion": "当前登录状态未核验，无法判断现在是否连接。",
            "browser_mode": mode,
            "platforms": [{"platform": key, **info, "current_connection": "unverified",
                "record_state": "recorded" if any(row.platform == key for row in rows) else "not_recorded",
                "meaning": "仅授权中心记录，当前是否登录未核验。未记录不能断言已退出或未连接。", "accounts": [
                {"status": row.status, "auth_kind": row.auth_kind, "nickname": row.nickname,
                 "checked_at": _iso(row.last_probe_at), "updated_at": _iso(row.updated_at),
                 "applicable": mode != "remote" or row.auth_kind != "desktop_cookie"}
                for row in rows if row.platform == key]} for key, info in sites.items()],
            "note": "Stored records only. Empty means no recorded authorization, not verified logout. "
                    "Desktop records do not describe the user's local Chrome. Verify in the execution task "
                    "before publishing or advertising. Login grants no publishing, ad-spend or monitoring consent."}


def spoken_platforms(value: dict) -> dict:
    """Keep an incomplete read visibly incomplete for the realtime model.

    Empty rows for every supported site look like negative login results. The
    phone gets actual stored records plus the explicit unknown conclusion,
    instead of an apparent checklist of disconnected accounts.
    """
    records = [{"platform": platform["platform"], "name": platform["name"], **account}
               for platform in value["platforms"] for account in platform["accounts"]]
    return {"status": "login_not_verified", "lookup_completed": True,
            "source": value["source"], "live_verified": False,
            "current_login_state": "unknown", "connection_conclusion": value["connection_conclusion"],
            **({"historical_records": records} if records else {}),
            "record_finding": "本次只读取历史授权信息，没有检查平台当前页面，因此没有连上或未连上的实时结论。",
            "browser_mode": value["browser_mode"],
            "note": "需要实际状态时，应由执行任务查看对应平台页面；用户暂不接入就停在查询，不发起登录或发布。"}


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
