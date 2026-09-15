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
#: How far ahead the creator center lets a post be scheduled; bounds how far back
#: a job may have been created and still land on the day being budgeted.
MAX_SCHEDULE_DAYS = 14


@dataclass(frozen=True)
class Budget:
    allowed: bool
    reason: str
    #: Posts already on the day being budgeted (today, or the scheduled day).
    today_count: int
    daily_limit: int
    next_allowed_at: datetime | None = None
    #: The day being budgeted, "MM-DD" Asia/Shanghai.
    day: str | None = None
    #: True when the budget was judged for a `schedule_at` slot rather than now.
    scheduled: bool = False


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


def parse_schedule_at(value: str | None) -> datetime | None:
    """'YYYY-MM-DD HH:mm' in Asia/Shanghai → aware UTC datetime; None when absent or malformed."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI).astimezone(timezone.utc)
    except ValueError:
        return None


def effective_time(created_at: datetime, published_at: datetime | None, details: dict | None) -> datetime:
    """When a counted job appears (or will appear) on the account: its scheduled
    slot when it was queued with `schedule_at`, else its publish time, else creation."""
    return parse_schedule_at((details or {}).get("schedule_at")) or _aware(published_at or created_at)


def _day_start(when: datetime) -> datetime:
    return when.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def check_budget(*, workspace_id: str, account_id: str | None, now: datetime, daily_limit: int,
                       min_interval_minutes: int, window_start_hour: int, window_end_hour: int,
                       schedule_at: str | None = None) -> Budget:
    """Whether this account may post at the slot: now, or the time named by
    `schedule_at`. A scheduled post is budgeted against the day it will appear
    and spaced against every other post's effective time, so a person can queue
    several days of posts in one sitting without the queueing itself tripping
    the daily cap, the interval, or the posting window."""
    scheduled = bool(schedule_at)
    slot = parse_schedule_at(schedule_at) if scheduled else now
    if slot is None:
        return Budget(False, "schedule_at 格式应为 YYYY-MM-DD HH:mm（上海时间）", 0, daily_limit, scheduled=True)
    slot_local = slot.astimezone(SHANGHAI)
    day = f"{slot_local:%m-%d}"
    if scheduled and slot <= now:
        return Budget(False, f"定时时间 {slot_local:%m-%d %H:%M} 已过，请改为将来的时刻", 0, daily_limit, day=day, scheduled=True)
    slot_day = _day_start(slot)
    lookback = min(slot_day, _day_start(now)) - timedelta(days=MAX_SCHEDULE_DAYS)
    async with get_db_session() as db:
        conds = [PublishJob.workspace_id == workspace_id, PublishJob.platform == DESKTOP_PLATFORM,
                 PublishJob.status.in_(COUNTED), PublishJob.created_at >= lookback]
        if account_id:
            conds.append(PublishJob.platform_account_id == account_id)
        rows = (await db.execute(select(PublishJob.created_at, PublishJob.published_at, PublishJob.details).where(*conds))).all()
    times = sorted(effective_time(c, p, d) for c, p, d in rows)
    count = sum(1 for t in times if _day_start(t) == slot_day)
    if count >= daily_limit:
        nxt = (slot_local + timedelta(days=1)).replace(hour=window_start_hour, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        reason = (f"{day} 已安排 {count} 条，达到每日上限 {daily_limit}，请改到 {nxt.astimezone(SHANGHAI):%m-%d} 之后" if scheduled
                  else f"今天已通过云电脑发布 {count} 条，达到每日上限 {daily_limit}")
        return Budget(False, reason, count, daily_limit, next_allowed_at=nxt, day=day, scheduled=scheduled)
    gap = timedelta(minutes=min_interval_minutes)
    near = [t for t in times if abs(slot - t) < gap]
    if near:
        nxt = max(near) + gap
        reason = (f"定时 {slot_local:%m-%d %H:%M} 与另一条（{max(near).astimezone(SHANGHAI):%m-%d %H:%M}）相隔不足 {min_interval_minutes} 分钟，"
                  f"请改到 {nxt.astimezone(SHANGHAI):%H:%M} 之后" if scheduled
                  else f"距上一条发布不足 {min_interval_minutes} 分钟，最早 {nxt.astimezone(SHANGHAI):%H:%M} 再发")
        return Budget(False, reason, count, daily_limit, next_allowed_at=nxt, day=day, scheduled=scheduled)
    if not in_window(slot, window_start_hour, window_end_hour):
        nxt = next_window_start(slot, window_start_hour, window_end_hour)
        window = f"{window_start_hour:02d}:00–{window_end_hour:02d}:00（上海时间）"
        reason = (f"定时 {slot_local:%m-%d %H:%M} 不在发布时段 {window}" if scheduled else f"当前不在发布时段 {window}")
        return Budget(False, reason, count, daily_limit, next_allowed_at=nxt, day=day, scheduled=scheduled)
    return Budget(True, "ok", count, daily_limit, day=day, scheduled=scheduled)

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
