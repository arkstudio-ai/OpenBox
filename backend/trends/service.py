"""Collect hot lists through the workspace's cloud desktop, once per key per day.

Cache semantics (plan §7 A3): the key is (source, board, window, category,
Asia/Shanghai day). A successful snapshot younger than `cache_hours` is
served to every workspace. `refresh=True` may recollect only after
`min_interval_seconds`, and the deployment never collects more than
`max_fetches_per_day` times per source. A failed collection is stored too,
so the next caller within the interval sees the error instead of hammering
the desktop. Two concurrent callers of the same key share one collection
(process-local lock, then a re-read of the table).

Only metadata and links are stored. The direct media URL a source hands out
(热点宝 `item_url`) is kept on the item and mirrored into `hot_media_links`;
for public items `resolve_media` loads the video page once and extracts the
douyinvod link.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from core.identifier import ascending as generate_id
from core.log import create_logger
from db.base import get_db_session
from db.models.hot_trend import HotMediaLink, HotTrendSnapshot
from trends import desktop_page
from trends.schema import HotItem
from trends.sources import AUTO_ORDER, HotSource, SourceError, get_source, pick_media_url, resolve_plan

log = create_logger("trends")

SHANGHAI = ZoneInfo("Asia/Shanghai")
_locks: dict[str, asyncio.Lock] = {}


class TrendsRefusal(Exception):
    """A reason the person can act on (authorise the site, wait, pick another source)."""


@dataclass
class TrendResult:
    source: str
    board: str
    window_hours: int
    category: str | None
    items: list[HotItem]
    cached: bool
    fetched_at: datetime
    snapshot_id: str
    credits: Decimal | None = None
    taxonomy: list[dict] | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Caller:
    workspace_id: str
    user_id: str
    session_id: str | None = None
    tool_call_id: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def day_key(when: datetime | None = None) -> str:
    return (when or _now()).astimezone(SHANGHAI).strftime("%Y-%m-%d")


def cache_key(source: str, board: str, window: int, category: str | None, day: str) -> str:
    return f"{source}:{board}:{window}h:{category or '*'}:{day}"


def _aware(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# ── entitlement ────────────────────────────────────────────────────────────
async def site_status(workspace_id: str, site_key: str) -> str:
    """bound | expired | unknown | desktop_offline | revoked | none — from the auth-center rows only."""
    from platforms import service as platform_service
    from platforms.desktop import service as desktop_service

    for row in await platform_service.list_accounts(workspace_id):
        if row.platform == site_key and row.auth_kind == desktop_service.AUTH_KIND:
            return row.status
    return "none"


async def pick_source(workspace_id: str, requested: str) -> HotSource:
    """`auto` → the first source in AUTO_ORDER whose site is bound (or needs none)."""
    if requested != "auto":
        source = get_source(requested)
        if source.requires_site:
            status = await site_status(workspace_id, source.requires_site)
            if status != "bound":
                raise TrendsRefusal(
                    f"{source.label} 需要先在授权中心绑定「{source.requires_site}」站点（当前状态 {status}）；"
                    f"让用户在云电脑上登录一次，或改用 source=douyin_public。"
                )
        return source
    for key in AUTO_ORDER:
        source = get_source(key)
        if not source.requires_site or await site_status(workspace_id, source.requires_site) == "bound":
            return source
    raise TrendsRefusal("no hot source available")


# ── cache reads ────────────────────────────────────────────────────────────
async def _get_snapshot(key: str) -> HotTrendSnapshot | None:
    async with get_db_session() as db:
        return (await db.execute(select(HotTrendSnapshot).where(HotTrendSnapshot.cache_key == key))).scalar_one_or_none()


async def _fetches_today(source: str) -> int:
    async with get_db_session() as db:
        return int((await db.execute(
            select(func.count()).select_from(HotTrendSnapshot).where(
                HotTrendSnapshot.source == source, HotTrendSnapshot.day == day_key(),
            )
        )).scalar_one() or 0)


async def latest_taxonomy(source: str) -> list[dict] | None:
    async with get_db_session() as db:
        rows = (await db.execute(
            select(HotTrendSnapshot.extra).where(
                HotTrendSnapshot.source == source, HotTrendSnapshot.status == "ok",
            ).order_by(HotTrendSnapshot.fetched_at.desc()).limit(20)
        )).scalars().all()
    for extra in rows:
        if isinstance(extra, dict) and extra.get("taxonomy"):
            return extra["taxonomy"]
    return None


def _result(row: HotTrendSnapshot, *, cached: bool, credits: Decimal | None = None, limit: int | None = None) -> TrendResult:
    items = [HotItem.model_validate(i) for i in (row.items or [])]
    if limit:
        items = items[:limit]
    extra = dict(row.extra or {})
    return TrendResult(
        source=row.source, board=row.board, window_hours=row.window_hours, category=row.category,
        items=items, cached=cached, fetched_at=_aware(row.fetched_at), snapshot_id=row.id,
        credits=credits if credits is not None else (Decimal(str(extra["credits"])) if extra.get("credits") else None),
        taxonomy=extra.pop("taxonomy", None), extra=extra,
    )


# ── desktop execution ──────────────────────────────────────────────────────
async def run_page(caller: Caller, *, url: str, expression: str, wait_resource: str | None, settle_s: float,
                   summary: str) -> dict:
    """Load `url` in the workspace desktop's Chrome and evaluate `expression`. Returns the script's JSON."""
    from core.config import get_config
    from platforms.desktop import service as desktop_service

    cfg = get_config().hot_trends
    record = await desktop_service.workspace_desktop(caller.workspace_id)
    if not record:
        raise TrendsRefusal("当前工作空间没有云电脑，无法采集热点（热点页要在云电脑浏览器里打开）。")
    payload = desktop_page.build_payload(
        url=url, expression=expression, wait_resource=wait_resource,
        timeout_s=cfg.page_timeout_seconds, settle_s=settle_s, eval_timeout_s=25,
    )
    data = await desktop_service.run_command_on_desktop(
        record, desktop_page.build_command(payload), parse=desktop_page.parse_output,
        summary=summary, operation="hot-trends", lease=True,
        timeout=cfg.page_timeout_seconds + 40, lease_ttl=float(cfg.page_timeout_seconds + 60),
        span_kind="platform.trends", session_id=caller.session_id or "hot-trends",
        tool_call_id=caller.tool_call_id,
    )
    if not data.get("ok"):
        raise SourceError(str(data.get("error") or "desktop page script failed")[:220])
    return data


# ── collection ─────────────────────────────────────────────────────────────
def _applicable(source: HotSource, board: str | None, window_hours: int | None, category: str | None):
    """(board, window) for this source, or None when the request cannot mean this source."""
    try:
        brd = source.board(board) if board else source.default_board()
    except KeyError:
        return None
    window = window_hours if window_hours is not None else source.default_window()
    if window not in source.windows_hours:
        return None
    if category and not source.supports_category:
        return None
    return brd, window


async def _fresh_snapshot(key: str, cache_hours: int) -> HotTrendSnapshot | None:
    row = await _get_snapshot(key)
    if row is not None and row.status == "ok" and _now() - _aware(row.fetched_at) <= timedelta(hours=cache_hours):
        return row
    return None


async def get_trends(caller: Caller, *, source_key: str = "auto", board: str | None = None, window_hours: int | None = None,
                     category: str | None = None, limit: int = 20, refresh: bool = False) -> TrendResult:
    from core.config import get_config

    cfg = get_config().hot_trends
    candidates = [get_source(source_key)] if source_key != "auto" else [get_source(k) for k in AUTO_ORDER]
    day = day_key()
    # Shared cache first: a board someone already collected today is everyone's,
    # whether or not this workspace could have collected it itself (plan §7 A3).
    if not refresh:
        for source in candidates:
            fit = _applicable(source, board, window_hours, category)
            if fit is None:
                continue
            brd, window = fit
            row = await _fresh_snapshot(cache_key(source.key, brd.key, window, category, day), cfg.cache_hours)
            if row is not None:
                return _result(row, cached=True, limit=limit)
    # Live collection needs entitlement: the first candidate this workspace may drive.
    source = await pick_source(caller.workspace_id, source_key)
    fit = _applicable(source, board, window_hours, category)
    if fit is None:
        try:
            brd = source.board(board) if board else source.default_board()
        except KeyError:
            raise TrendsRefusal(f"{source.label} 没有榜单 {board!r}；可选：{', '.join(b.key for b in source.boards)}")
        window = window_hours if window_hours is not None else source.default_window()
        if window not in source.windows_hours:
            raise TrendsRefusal(f"{source.label} 支持的时间窗（小时）：{', '.join(map(str, source.windows_hours))}")
        raise TrendsRefusal(f"{source.label} 不支持按类目筛选；去掉 category 或改用 source=douhot。")
    brd, window = fit
    key = cache_key(source.key, brd.key, window, category, day)

    async def _serve_cached() -> TrendResult | None:
        row = await _get_snapshot(key)
        if row is None:
            return None
        age = _now() - _aware(row.fetched_at)
        if row.status == "ok" and not refresh and age <= timedelta(hours=cfg.cache_hours):
            return _result(row, cached=True, limit=limit)
        if age < timedelta(seconds=cfg.min_interval_seconds):
            if row.status == "ok":
                return _result(row, cached=True, limit=limit)  # refresh asked too soon: the fresh copy is what we have
            raise TrendsRefusal(
                f"这一榜单 {int(age.total_seconds())} 秒前刚采集失败（{row.error}），"
                f"{cfg.min_interval_seconds} 秒内不会重试。"
            )
        return None

    served = await _serve_cached()
    if served:
        return served
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        served = await _serve_cached()
        if served:
            return served
        if await _fetches_today(source.key) >= cfg.max_fetches_per_day:
            raise TrendsRefusal(f"{source.label} 今天的采集次数已达上限 {cfg.max_fetches_per_day}，明天再试或读取已有榜单。")
        return await _collect(caller, source, brd, window, category, key, limit=limit, size=cfg.fetch_size)


async def _collect(caller: Caller, source: HotSource, brd, window: int, category: str | None, key: str, *,
                   limit: int, size: int) -> TrendResult:
    from autopilot import ledger as _autopilot
    from billing.media import quote_hot_trends, settle_hot_trends

    try:
        _autopilot.guard_paid_step(caller.session_id or "", kind="hot_trends", credits=quote_hot_trends(source.key).credits or 0,
                                   note=f"{source.key} {brd.key} live")
    except _autopilot.BudgetStop as exc:
        raise TrendsRefusal(str(exc)) from exc
    plan = source.plan(source, brd, window, category, size)
    now = _now()
    items: list[HotItem] = []
    extra: dict[str, Any] = {}
    error: str | None = None
    try:
        data = await run_page(caller, url=plan.url, expression=plan.expression, wait_resource=plan.wait_resource,
                              settle_s=plan.settle_s, summary=f"hot_trends {source.key} {brd.key} {window}h {category or '*'}")
        items, extra = source.parse(source, brd, data.get("value"))
        extra.update({"page_title": data.get("title"), "final_url": data.get("final_url"), "load_s": data.get("load_s")})
    except SourceError as exc:
        error = str(exc)[:400]
    except TrendsRefusal:
        raise
    except Exception as exc:  # desktop unavailable/busy, transport
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    row = await _upsert_snapshot(key, source, brd, window, category, items, extra, error, caller, now)
    if error:
        log.warning(f"hot_trends collection failed source={source.key} board={brd.key}: {error}")
        raise TrendsRefusal(f"采集 {source.label}·{brd.label} 失败：{error}")
    credits = await settle_hot_trends(snapshot_id=row.id, source=source.key, workspace_id=caller.workspace_id,
                                      user_id=caller.user_id, session_id=caller.session_id, item_count=len(items))
    if credits is not None:
        async with get_db_session() as db:
            db_row = await db.get(HotTrendSnapshot, row.id)
            if db_row is not None:
                db_row.extra = {**(db_row.extra or {}), "credits": str(credits)}
    await _mirror_media_links(source.key, items, caller.user_id)
    return _result(row, cached=False, credits=credits, limit=limit)


async def _upsert_snapshot(key, source, brd, window, category, items, extra, error, caller, now) -> HotTrendSnapshot:
    async with get_db_session() as db:
        row = (await db.execute(select(HotTrendSnapshot).where(HotTrendSnapshot.cache_key == key))).scalar_one_or_none()
        if row is None:
            row = HotTrendSnapshot(id=generate_id("trend"), cache_key=key, source=source.key, board=brd.key,
                                   window_hours=window, category=category, day=day_key(now), created_at=now)
            db.add(row)
        row.status = "failed" if error else "ok"
        row.items = [i.model_dump(mode="json", exclude_none=True) for i in items]
        row.item_count = len(items)
        row.extra = extra
        row.error = error
        row.fetched_by_workspace = caller.workspace_id
        row.fetched_by_user = caller.user_id
        row.fetched_at = now
        await db.flush()
        db.expunge(row)
        return row


async def _mirror_media_links(source: str, items: list[HotItem], user_id: str) -> None:
    from core.config import get_config

    ttl = timedelta(hours=get_config().hot_trends.media_link_ttl_hours)
    now = _now()
    async with get_db_session() as db:
        for item in items:
            if item.kind != "video" or not item.media_url:
                continue
            link = await db.get(HotMediaLink, item.id)
            if link is None:
                db.add(HotMediaLink(video_id=item.id, source=source, media_url=item.media_url, page_url=item.url,
                                    resolved_by_user=user_id, resolved_at=now, expires_at=now + ttl))
            else:
                link.source, link.media_url, link.page_url = source, item.media_url, item.url
                link.resolved_at, link.expires_at, link.resolved_by_user = now, now + ttl, user_id


# ── media resolution ───────────────────────────────────────────────────────
def video_id_from(ref: str) -> str | None:
    """`7681818700668891647` or any douyin URL containing `/video/<id>`."""
    import re

    ref = (ref or "").strip()
    if re.fullmatch(r"\d{15,20}", ref):
        return ref
    m = re.search(r"/video/(\d{15,20})", ref)
    return m.group(1) if m else None


async def resolve_media(caller: Caller, ref: str) -> dict:
    """Direct media URL for a hot video: cached link, else load the public page once."""
    from core.config import get_config

    vid = video_id_from(ref)
    if not vid:
        raise TrendsRefusal(f"{ref!r} 不是抖音视频 id 或视频页链接。")
    now = _now()
    async with get_db_session() as db:
        link = await db.get(HotMediaLink, vid)
        if link is not None and _aware(link.expires_at) > now:
            return {"video_id": vid, "media_url": link.media_url, "page_url": link.page_url, "cached": True,
                    "expires_at": _aware(link.expires_at).isoformat(), "source": link.source}
    plan = resolve_plan(vid)
    try:
        data = await run_page(caller, url=plan.url, expression=plan.expression, wait_resource=None,
                              settle_s=plan.settle_s, summary=f"hot_trends resolve {vid}")
    except SourceError as exc:
        raise TrendsRefusal(f"打开视频页失败：{exc}") from exc
    media_url = pick_media_url(data.get("value"))
    if not media_url:
        raise TrendsRefusal(f"视频 {vid} 的页面里没有可用直链（可能需要登录或页面结构变化）；换一条视频，或用热点宝榜单自带的直链。")
    ttl = timedelta(hours=get_config().hot_trends.media_link_ttl_hours)
    async with get_db_session() as db:
        link = await db.get(HotMediaLink, vid)
        if link is None:
            db.add(HotMediaLink(video_id=vid, source="douyin_public", media_url=media_url, page_url=plan.url,
                                resolved_by_user=caller.user_id, resolved_at=now, expires_at=now + ttl))
        else:
            link.media_url, link.page_url, link.source = media_url, plan.url, "douyin_public"
            link.resolved_at, link.expires_at, link.resolved_by_user = now, now + ttl, caller.user_id
    return {"video_id": vid, "media_url": media_url, "page_url": plan.url, "cached": False,
            "expires_at": (now + ttl).isoformat(), "source": "douyin_public", "page_title": (data.get("value") or {}).get("title")}
