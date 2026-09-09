"""Human-paced limits and the per-account breaker for desktop publishing (plan §7 C3/C4).

Pure decisions live here so they can be unit tested without a desktop:
which route applies (`resolve_mode`), whether an account may post now
(`check_budget`), and whether page text is a risk signal (`risk_signal`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.base import get_db_session
from db.models.platform_account import PlatformAccount
from db.models.publish_job import PublishJob

SHANGHAI = ZoneInfo("Asia/Shanghai")
#: `publish_jobs.platform` for the desktop route (the QR route uses "douyin").
DESKTOP_PLATFORM = "douyin_creator"
#: Job statuses that count against the day's budget.
COUNTED = ("pending", "published")


@dataclass(frozen=True)
class Budget:
    allowed: bool
    reason: str
    today_count: int
    daily_limit: int
    next_allowed_at: datetime | None = None


def _aware(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def resolve_mode(requested: str | None, account: PlatformAccount | None, default_mode: str) -> tuple[str, str]:
    """(mode, reason). The breaker wins over everything; then the caller's
    request (template/tool arg); then the deployment default."""
    if account is not None and account.auto_publish_disabled_at is not None:
        return "package", f"该账号的自动发布已停用（{account.auto_publish_disabled_reason or '风控'}），改产扫码投稿包"
    if requested in ("auto", "package"):
        return requested, "按模版/参数指定"
    return default_mode, "按部署默认"


def in_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    hour = now.astimezone(SHANGHAI).hour
    return start_hour <= hour < end_hour


def next_window_start(now: datetime, start_hour: int, end_hour: int) -> datetime:
    local = now.astimezone(SHANGHAI)
    start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if local.hour >= end_hour or local >= start:
        start = start + timedelta(days=1)
    return start.astimezone(timezone.utc)


async def check_budget(*, workspace_id: str, account_id: str | None, now: datetime, daily_limit: int,
                       min_interval_minutes: int, window_start_hour: int, window_end_hour: int) -> Budget:
    """Today's desktop posts for this account, the last one's time, and the hours window."""
    day_start_local = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_start_local.astimezone(timezone.utc)
    async with get_db_session() as db:
        conds = [PublishJob.workspace_id == workspace_id, PublishJob.platform == DESKTOP_PLATFORM,
                 PublishJob.status.in_(COUNTED), PublishJob.created_at >= day_start]
        if account_id:
            conds.append(PublishJob.platform_account_id == account_id)
        rows = (await db.execute(select(PublishJob.created_at, PublishJob.published_at).where(*conds))).all()
    today = len(rows)
    last = max((_aware(p or c) for c, p in rows), default=None)
    if today >= daily_limit:
        return Budget(False, f"今天已通过云电脑发布 {today} 条，达到每日上限 {daily_limit}", today, daily_limit,
                      next_allowed_at=next_window_start(now, window_start_hour, window_end_hour))
    if last is not None and now - last < timedelta(minutes=min_interval_minutes):
        nxt = last + timedelta(minutes=min_interval_minutes)
        return Budget(False, f"距上一条发布不足 {min_interval_minutes} 分钟，最早 {nxt.astimezone(SHANGHAI):%H:%M} 再发", today, daily_limit, next_allowed_at=nxt)
    if not in_window(now, window_start_hour, window_end_hour):
        nxt = next_window_start(now, window_start_hour, window_end_hour)
        return Budget(False, f"当前不在发布时段 {window_start_hour:02d}:00–{window_end_hour:02d}:00（上海时间）", today, daily_limit, next_allowed_at=nxt)
    return Budget(True, "ok", today, daily_limit)


def risk_signal(text: str, patterns: list[str]) -> str | None:
    """The first risk pattern present in page text, or None."""
    low = (text or "").lower()
    for pat in patterns:
        if pat.lower() in low:
            return pat
    return None


async def disable_auto(account_id: str, reason: str, now: datetime) -> None:
    async with get_db_session() as db:
        row = await db.get(PlatformAccount, account_id)
        if row is not None and row.auto_publish_disabled_at is None:
            row.auto_publish_disabled_at = now
            row.auto_publish_disabled_reason = reason[:400]


async def enable_auto(account_id: str) -> bool:
    async with get_db_session() as db:
        row = await db.get(PlatformAccount, account_id)
        if row is None:
            return False
        row.auto_publish_disabled_at = None
        row.auto_publish_disabled_reason = None
        return True
