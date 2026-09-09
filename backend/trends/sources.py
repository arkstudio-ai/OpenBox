"""Pluggable hot-list sources (plan §7 A4): 热点宝 first, the public 热榜 second.

A source declares which boards, windows and categories it supports and how
to collect one board inside the desktop browser: the page to load, the
resource to wait for, the JavaScript expression that returns a JSON string,
and how to normalise that JSON into `HotItem`s. Adding a source is adding a
module-level instance to `SOURCES`; the service and the tool never branch on
a source's name.

Everything here is data and pure functions, unit tested from captured
responses; only `service.collect` touches a desktop.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from trends.schema import HotItem, parse_cn_number, topics_from_title


@dataclass(frozen=True)
class Board:
    key: str
    label: str
    kind: str  # video | topic | search
    #: Source-specific selector (热点宝 sub_type).
    code: int = 0


@dataclass(frozen=True)
class CollectPlan:
    url: str
    expression: str
    wait_resource: str | None
    #: Extra seconds to let the SPA finish its own calls before we replay.
    settle_s: float = 1.0


@dataclass(frozen=True)
class HotSource:
    key: str
    label: str
    #: Desktop-site key that must be `bound` before collecting; None = public.
    requires_site: str | None
    boards: tuple[Board, ...]
    windows_hours: tuple[int, ...]
    #: Whether the source can filter by a category label.
    supports_category: bool
    plan: Callable[["HotSource", Board, int, str | None, int], CollectPlan]
    parse: Callable[["HotSource", Board, dict], tuple[list[HotItem], dict]]
    notes: str = ""

    def board(self, key: str) -> Board:
        for b in self.boards:
            if b.key == key:
                return b
        raise KeyError(key)

    def default_board(self) -> Board:
        return self.boards[0]

    def default_window(self) -> int:
        return self.windows_hours[0]


class SourceError(Exception):
    """The source answered, but not with a list we can use (logged out, API changed, empty)."""


# ── 热点宝 (douhot.douyin.com) ──────────────────────────────────────────────
DOUHOT_PAGE = "https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all"
DOUHOT_BOARDS = (
    Board("video_all", "视频总榜", "video", 1001),
    Board("video_lowfans", "低粉爆款视频榜", "video", 1002),
    Board("video_completion", "高完播率视频榜", "video", 1003),
    Board("video_follow", "高涨粉视频榜", "video", 1004),
    Board("video_like", "高点赞视频榜", "video", 1005),
    Board("topic", "话题榜", "topic", 2001),
    Board("topic_rising", "飙升话题榜", "topic", 2002),
    Board("search", "搜索榜", "search", 3001),
    Board("search_rising", "飙升搜索榜", "search", 3002),
)
DOUHOT_WINDOWS = (24, 1, 72, 168)

# Runs inside the loaded board page. Replays URLs the page itself signed
# (signature covers the URL, not the body — measured 2026-09-10), so the body
# can carry our own window / board / page size / category tags. Category
# tags are `{value: first-level, children: [{value: second-level}, …]}` from
# the page's own taxonomy call; a first level alone barely filters.
_DOUHOT_JS = r"""(async()=>{
const P=__P__;
const rs=performance.getEntriesByType('resource').map(e=>e.name);
const pick=(re)=>rs.filter(u=>re.test(u)).pop();
const out={taxonomy:null};
if(!/douhot\.douyin\.com/.test(location.host)){out.error='redirected: '+location.href.slice(0,120);return JSON.stringify(out);}
const ct=pick(/material\/content_tag/);
let taxonomy=null;
if(ct){try{const r=await fetch(ct,{credentials:'include'});const j=await r.json();
  taxonomy=(j.data||[]).map(t=>({label:t.label,value:t.value,children:(t.children||[]).map(c=>({label:c.label,value:c.value}))}));}
  catch(e){out.taxonomy_error=String(e).slice(0,120);}}
out.taxonomy=taxonomy?taxonomy.map(t=>({label:t.label,children:t.children.map(c=>c.label)})):null;
let tags=[];
if(P.category){
  if(!taxonomy){out.error='taxonomy unavailable, cannot filter by category';return JSON.stringify(out);}
  const t=taxonomy.find(x=>x.label===P.category||x.children.some(c=>c.label===P.category));
  if(!t){out.error='unknown category: '+P.category;return JSON.stringify(out);}
  const child=t.children.find(c=>c.label===P.category);
  tags=[{value:t.value,children:child?[{value:child.value}]:t.children.map(c=>({value:c.value}))}];
}
const url=P.kind==='video'?pick(/material\/video_billboard/):P.kind==='topic'?pick(/material\/challenge_billboard/):pick(/hot_search\/query_list/);
if(!url){out.error='page issued no signed '+P.kind+' request (not logged in, or page changed)';return JSON.stringify(out);}
const body=P.kind==='search'?{date_window:P.window,page_num:1,page_size:P.size,sub_type:P.sub_type}
  :{sub_type:P.sub_type,date_window:P.window,page:1,page_size:P.size,tag_version:'v2',...(tags.length?{tags}:{})};
const r=await fetch(url,{method:'POST',credentials:'include',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
const j=await r.json();
out.status=r.status;out.code=j.code;out.msg=j.msg||j.message||'';out.data=j.data||null;out.body=body;
return JSON.stringify(out);})()"""


def _douhot_plan(source: HotSource, board: Board, window: int, category: str | None, size: int) -> CollectPlan:
    params = {"kind": board.kind, "sub_type": board.code, "window": window, "size": size, "category": category}
    return CollectPlan(
        url=DOUHOT_PAGE,
        expression=_DOUHOT_JS.replace("__P__", json.dumps(params, ensure_ascii=False)),
        wait_resource=r"douhot/v1/material/video_billboard",
        settle_s=1.5,
    )


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _douhot_parse(source: HotSource, board: Board, value: dict) -> tuple[list[HotItem], dict]:
    if not isinstance(value, dict):
        raise SourceError("desktop returned no object")
    if value.get("error"):
        raise SourceError(str(value["error"]))
    extra = {"taxonomy": value.get("taxonomy"), "body": value.get("body"), "http_status": value.get("status")}
    if value.get("code") not in (0, "0"):
        raise SourceError(f"热点宝 code={value.get('code')} {value.get('msg') or ''}".strip())
    data = value.get("data") or {}
    items: list[HotItem] = []
    if board.kind == "video":
        for rank, o in enumerate(data.get("objs") or [], start=1):
            vid = str(o.get("item_id") or "")
            if not vid:
                continue
            title = str(o.get("item_title") or "")
            dur = o.get("item_duration")
            # media_type 4 = video; image posts (aweme_images) carry a static
            # bundle in item_url that video_analyze cannot sample.
            is_video = o.get("media_type") in (4, None)
            items.append(HotItem(
                kind="video", id=vid, title=title, url=f"https://www.douyin.com/video/{vid}",
                author=o.get("nick_name"), author_followers=_int(o.get("fans_cnt")),
                duration_sec=round(dur / 1000, 1) if isinstance(dur, (int, float)) and dur > 0 else None,
                published_at=_iso(o.get("publish_time")), play_count=_int(o.get("play_cnt")),
                like_count=_int(o.get("like_cnt")), heat=float(o["score"]) if isinstance(o.get("score"), (int, float)) else None,
                rank=rank, topics=topics_from_title(title), cover_url=o.get("item_cover_url"),
                media_url=(o.get("item_url") or None) if is_video else None,
                extra={**{k: o.get(k) for k in ("follow_cnt", "follow_rate", "like_rate", "media_type") if k in o},
                       **({} if is_video else {"post_type": "images"})},
            ))
    elif board.kind == "topic":
        for rank, o in enumerate(data.get("objs") or [], start=1):
            cid = str(o.get("challenge_id") or "")
            if not cid:
                continue
            name = str(o.get("challenge_name") or "")
            items.append(HotItem(
                kind="topic", id=cid, title=name,
                url=f"https://www.douyin.com/search/{name}" if name else None,
                play_count=_int(o.get("play_cnt")), heat=float(o["score"]) if isinstance(o.get("score"), (int, float)) else None,
                rank=rank, topics=[name] if name else [], cover_url=o.get("cover_url"),
                published_at=_iso(o.get("create_time")),
                extra={k: o.get(k) for k in ("publish_cnt", "avg_play_cnt") if k in o},
            ))
    else:
        for rank, o in enumerate(data.get("search_list") or [], start=1):
            word = str(o.get("key_word") or "")
            if not word:
                continue
            items.append(HotItem(
                kind="search", id=word, title=word, url=f"https://www.douyin.com/search/{word}",
                heat=float(o["search_score"]) if isinstance(o.get("search_score"), (int, float)) else None,
                rank=rank, topics=[word], extra={"trend_points": len(o.get("trends") or [])},
            ))
    page = data.get("page")
    extra["total"] = page.get("total") if isinstance(page, dict) and page else data.get("total_count")
    return items, extra


DOUHOT = HotSource(
    key="douhot", label="抖音热点宝", requires_site="douyin_hot", boards=DOUHOT_BOARDS,
    windows_hours=DOUHOT_WINDOWS, supports_category=True, plan=_douhot_plan, parse=_douhot_parse,
    notes="需要工作空间云电脑先在授权中心绑定「抖音热点宝」；视频条目自带直链（media_url），可直接交给 video_analyze。",
)


# ── Public 热榜 (www.douyin.com/hot) ───────────────────────────────────────
PUBLIC_PAGE = "https://www.douyin.com/hot"
_PUBLIC_JS = r"""(()=>{
const out=[];const seen=new Set();
for(const li of document.querySelectorAll('ul[data-e2e="scroll-list"] li')){
  const a=li.querySelector('a[href*="/video/"]');if(!a)continue;
  const m=(a.getAttribute('href')||'').match(/\/video\/(\d{15,})/);if(!m||seen.has(m[1]))continue;seen.add(m[1]);
  const img=li.querySelector('img');
  out.push({id:m[1],title:(img&&img.alt)||'',cover:(img&&(img.currentSrc||img.src))||'',lines:(li.innerText||'').split('\n').map(s=>s.trim()).filter(Boolean)});
}
return JSON.stringify({items:out,title:document.title,count:out.length});})()"""

_DURATION = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _public_plan(source: HotSource, board: Board, window: int, category: str | None, size: int) -> CollectPlan:
    return CollectPlan(url=PUBLIC_PAGE, expression=_PUBLIC_JS, wait_resource=None, settle_s=4.0)


def parse_public_lines(lines: list[str]) -> dict:
    """`['00:12', '2407', '<title>', '@author', '· 4天前']` → fields. Order-tolerant."""
    out: dict = {}
    for line in lines:
        m = _DURATION.match(line)
        if m and "duration_sec" not in out:
            h, mnt, sec = (0, int(m.group(1)), int(m.group(2))) if not m.group(3) else (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            out["duration_sec"] = float(h * 3600 + mnt * 60 + sec)
        elif line.startswith("@") and "author" not in out:
            out["author"] = line[1:].strip()
        elif line.startswith("·") and "published_at" not in out:
            out["published_at"] = line.lstrip("·").strip()
        elif "like_count" not in out and parse_cn_number(line) is not None and re.fullmatch(r"[\d.,]+[万亿wW]?", line):
            out["like_count"] = parse_cn_number(line)
    return out


def _public_parse(source: HotSource, board: Board, value: dict) -> tuple[list[HotItem], dict]:
    if not isinstance(value, dict):
        raise SourceError("desktop returned no object")
    if value.get("error"):
        raise SourceError(str(value["error"]))
    items: list[HotItem] = []
    for rank, o in enumerate(value.get("items") or [], start=1):
        vid = str(o.get("id") or "")
        if not vid:
            continue
        title = str(o.get("title") or "")
        fields = parse_public_lines(o.get("lines") or [])
        items.append(HotItem(
            kind="video", id=vid, title=title, url=f"https://www.douyin.com/video/{vid}",
            author=fields.get("author"), duration_sec=fields.get("duration_sec"),
            published_at=fields.get("published_at"), like_count=fields.get("like_count"),
            rank=rank, topics=topics_from_title(title), cover_url=o.get("cover") or None,
        ))
    if not items:
        raise SourceError(f"public hot page listed no videos (title={value.get('title')!r})")
    return items, {"page_title": value.get("title")}


DOUYIN_PUBLIC = HotSource(
    key="douyin_public", label="抖音公开热榜", requires_site=None,
    boards=(Board("video", "热点视频", "video"),), windows_hours=(24,), supports_category=False,
    plan=_public_plan, parse=_public_parse,
    notes="无需登录，字段较少（无播放量/粉丝数），直链要另行解析（hot_trends resolve）。",
)


SOURCES: dict[str, HotSource] = {DOUHOT.key: DOUHOT, DOUYIN_PUBLIC.key: DOUYIN_PUBLIC}
#: Order tried by `source=auto`: the richest source a workspace is entitled to.
AUTO_ORDER: tuple[str, ...] = (DOUHOT.key, DOUYIN_PUBLIC.key)


def get_source(key: str) -> HotSource:
    try:
        return SOURCES[key]
    except KeyError:
        raise KeyError(f"unknown hot source {key!r}; known: {', '.join(SOURCES)}") from None


# ── Direct media link resolution (public video page) ───────────────────────
VIDEO_PAGE = "https://www.douyin.com/video/{id}"
_RESOLVE_JS = r"""(()=>{
const direct=Array.from(document.querySelectorAll('video source, video')).map(e=>e.getAttribute('src')||e.src||'').filter(s=>s.startsWith('http'));
const html=document.documentElement.innerHTML.replace(/\\u002F/g,'/').replace(/\\\//g,'/');
const found=Array.from(new Set((html.match(/https?:\/\/[a-z0-9.-]*douyinvod\.com\/[^"'\\\s<>]+/g)||[])));
return JSON.stringify({direct:direct.slice(0,4),found:found.slice(0,6),title:document.title,url:location.href});})()"""


def resolve_plan(video_id: str) -> CollectPlan:
    return CollectPlan(url=VIDEO_PAGE.format(id=video_id), expression=_RESOLVE_JS, wait_resource=None, settle_s=5.0)


def pick_media_url(value: dict) -> str | None:
    if not isinstance(value, dict):
        return None
    for u in list(value.get("direct") or []) + list(value.get("found") or []):
        if isinstance(u, str) and u.startswith("http") and ("douyinvod.com" in u or "mime_type=video" in u):
            return u
    return None
