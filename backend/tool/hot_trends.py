"""hot_trends: what is hot on douyin right now, from the shared daily cache.

Sources are pluggable (`trends.sources`); the tool only knows `auto`.
Collection happens in the workspace's cloud desktop browser, once per
(source, board, window, category) per day for the whole deployment, and only
metadata and links are stored. `resolve` turns a hot video into a direct
media URL that `video_analyze` can sample.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tool.tool import ToolContext, ToolResult, define_tool
from trends import service as trends_service
from trends.sources import AUTO_ORDER, SOURCES, get_source


class HotTrendsArgs(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["list", "sources", "resolve"] = "list"
    #: auto | douhot | douyin_public
    source: str = Field(default="auto", max_length=32)
    #: Board key from `sources` (default: the source's first board).
    board: str | None = Field(default=None, max_length=32)
    #: Time window in hours (default: the source's default).
    window_hours: int | None = Field(default=None, ge=1, le=720)
    #: Category label from `sources` taxonomy (热点宝 only), e.g. 美食 / 美食教程.
    category: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=20, ge=1, le=50)
    #: Recollect even if today's snapshot exists (rate limited).
    refresh: bool = False
    #: resolve: a douyin video id or video page URL.
    video: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _by_action(self) -> "HotTrendsArgs":
        if self.action == "resolve" and not self.video:
            raise ValueError("resolve requires video")
        if self.source != "auto" and self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}; known: auto, {', '.join(SOURCES)}")
        if self.board and self.source != "auto":
            try:
                get_source(self.source).board(self.board)
            except KeyError:
                raise ValueError(f"unknown board {self.board!r} for {self.source}; see action=sources") from None
        return self


def _caller(ctx: ToolContext) -> trends_service.Caller:
    return trends_service.Caller(workspace_id=ctx.workspace_id, user_id=ctx.user_id,
                                 session_id=ctx.session_id or None, tool_call_id=ctx.part_id or "")


def _item_line(i) -> dict:
    d = {"rank": i.rank, "id": i.id, "title": i.title[:80]}
    for k in ("url", "author", "author_followers", "duration_sec", "published_at", "play_count", "like_count", "heat", "topics", "cover_url"):
        v = getattr(i, k)
        if v not in (None, [], ""):
            d[k] = v
    d["has_media_url"] = bool(i.media_url)
    if i.extra.get("post_type"):
        d["post_type"] = i.extra["post_type"]
    return d


async def _list(args: HotTrendsArgs, ctx: ToolContext) -> ToolResult:
    res = await trends_service.get_trends(
        _caller(ctx), source_key=args.source, board=args.board, window_hours=args.window_hours,
        category=args.category, limit=args.limit, refresh=args.refresh,
    )
    source = get_source(res.source)
    lines = [
        f"source={res.source} board={res.board} window_hours={res.window_hours} category={res.category or '*'}",
        f"items={len(res.items)} cached={'true' if res.cached else 'false'} fetched_at={res.fetched_at.isoformat()}",
    ]
    if res.credits is not None and not res.cached:
        lines.append(f"credits={format(res.credits.normalize(), 'f')}")
    if res.extra.get("total") is not None:
        lines.append(f"source_total={res.extra['total']}")
    lines.append("")
    lines.append("Items (rank, id, title, metrics; has_media_url=true means video_analyze can take source=<url from resolve or media_url>):")
    lines.append(json.dumps([_item_line(i) for i in res.items], ensure_ascii=False))
    if source.requires_site is None:
        lines.append("")
        lines.append("Public source: no play counts; run action=resolve with the video id before video_analyze.")
    return ToolResult(
        title=f"Hot trends · {source.label} · {source.board(res.board).label}",
        output="\n".join(lines),
        metadata={
            "source": res.source, "board": res.board, "window_hours": res.window_hours, "category": res.category,
            "cached": res.cached, "snapshot_id": res.snapshot_id, "fetched_at": res.fetched_at.isoformat(),
            "credits": format(res.credits.normalize(), "f") if res.credits is not None else None,
            "items": [i.model_dump(mode="json", exclude_none=True) for i in res.items],
        },
    )


async def _sources(args: HotTrendsArgs, ctx: ToolContext) -> ToolResult:
    lines = []
    meta = []
    for key in AUTO_ORDER:
        s = get_source(key)
        status = await trends_service.site_status(ctx.workspace_id, s.requires_site) if s.requires_site else "public"
        usable = status in ("bound", "public")
        lines.append(f"- {key} · {s.label} · {'可用' if usable else f'不可用（站点 {s.requires_site} 状态 {status}）'}")
        lines.append(f"  boards: " + ", ".join(f"{b.key}({b.label})" for b in s.boards))
        lines.append(f"  window_hours: {', '.join(map(str, s.windows_hours))}; category: {'yes' if s.supports_category else 'no'}")
        entry = {"source": key, "label": s.label, "usable": usable, "site": s.requires_site, "site_status": status,
                 "boards": [{"key": b.key, "label": b.label, "kind": b.kind} for b in s.boards],
                 "window_hours": list(s.windows_hours), "supports_category": s.supports_category}
        if s.supports_category:
            taxonomy = await trends_service.latest_taxonomy(key)
            if taxonomy:
                lines.append("  categories: " + "; ".join(f"{t['label']}[{','.join(t.get('children') or [])}]" for t in taxonomy)[:1500])
                entry["categories"] = taxonomy
            else:
                lines.append("  categories: 首次采集后可见（先 action=list 一次）")
        if s.notes:
            lines.append(f"  {s.notes}")
        meta.append(entry)
    return ToolResult(title="Hot trend sources", output="\n".join(lines), metadata={"sources": meta})


async def _resolve(args: HotTrendsArgs, ctx: ToolContext) -> ToolResult:
    data = await trends_service.resolve_media(_caller(ctx), args.video or "")
    lines = [f"video_id={data['video_id']}", f"media_url={data['media_url']}", f"cached={'true' if data['cached'] else 'false'}",
             f"expires_at={data['expires_at']}", "Pass media_url as video_analyze source (direct link; expires)."]
    return ToolResult(title=f"Hot video media · {data['video_id']}", output="\n".join(lines), metadata=data)


async def execute(args: HotTrendsArgs, ctx: ToolContext) -> ToolResult:
    from session.browser_pref import get_browser_mode

    if not ctx.workspace_id:
        return ToolResult(title="Hot trends unavailable", output="No workspace in this context; hot lists are collected through the workspace's cloud desktop.")
    mode = await get_browser_mode(ctx.user_id) if ctx.user_id else "local"
    if mode == "remote" and args.action != "sources":
        return ToolResult(title="Hot trends unavailable in extension mode",
                          output="当前浏览器模式是用户本机 Chrome（extension），热点采集要走云电脑浏览器；切回云电脑模式后再用。",
                          metadata={"applicable": False, "mode": mode})
    try:
        if args.action == "sources":
            return await _sources(args, ctx)
        if args.action == "resolve":
            return await _resolve(args, ctx)
        return await _list(args, ctx)
    except trends_service.TrendsRefusal as exc:
        return ToolResult(title="Hot trends refused", output=str(exc), metadata={"refused": True})
    except KeyError as exc:
        return ToolResult(title="Hot trends refused", output=str(exc).strip("'\""), metadata={"refused": True})


HOT_TRENDS_DESCRIPTION = """Read what is hot on douyin (抖音): video boards, topic boards, search boards.

Actions:
- list (default): items for one board. `source=auto` picks 抖音热点宝 (douhot) when the \
workspace has bound it in the auth center, else the public 热榜 (douyin_public). \
Optional board / window_hours / category (热点宝 only; labels from `sources`) / limit / refresh.
- sources: which sources this workspace can use, their boards, windows and category labels.
- resolve: direct media URL for one hot video (id or page URL) so `video_analyze` can sample it. \
热点宝 items already carry one (has_media_url=true); public items need this step.

Collection runs in the workspace's cloud desktop browser and is cached per board per day for \
everyone (cached=true costs nothing; a live collection reports credits=). Only titles, metrics \
and links are stored — never the videos. Failures and rate limits are reported, never retried silently."""

hot_trends_tool = define_tool(
    "hot_trends",
    description=HOT_TRENDS_DESCRIPTION,
    parameters=HotTrendsArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
)
