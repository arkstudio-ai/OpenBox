"""hot_trends: source parsing, shared daily cache, rate limits, billing, media resolution."""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

import trends.service as ts
from tool.hot_trends import HotTrendsArgs, execute
from tool.tool import ToolContext
from trends import desktop_page
from trends.sources import DOUHOT, DOUYIN_PUBLIC, SourceError, get_source, pick_media_url

DOUHOT_VIDEO = {
    "taxonomy": [{"label": "美食", "children": ["美食教程", "吃播"]}, {"label": "旅行", "children": ["旅行风景"]}],
    "status": 200, "code": 0, "msg": "",
    "body": {"sub_type": 1001, "date_window": 72, "page": 1, "page_size": 50, "tag_version": "v2"},
    "data": {"page": {"page": 1, "page_size": 50, "total": 1000}, "objs": [
        {"item_id": "7682944569603926010", "item_title": "我的腿怎么在漏水 #自由潜湿衣的重要性 #自由潜教练",
         "item_cover_url": "https://p3-sign.douyinpic.com/c.jpeg", "item_duration": 35800, "nick_name": "涛涛很会玩",
         "fans_cnt": 5939, "play_cnt": 23482854, "publish_time": 1788824930, "score": 3372350,
         "item_url": "https://v26-cold.douyinvod.com/abc/video/?mime_type=video_mp4", "like_cnt": 659302,
         "follow_cnt": 2552, "follow_rate": 0.000107, "like_rate": 0.028076, "media_type": 4},
        {"item_id": "7682000000000000001", "item_title": "第二条", "item_duration": 12000, "nick_name": "b", "fans_cnt": 10,
         "play_cnt": 100, "publish_time": 1788824930, "score": 1, "item_url": "", "like_cnt": 5},
        {"item_id": "7681922343069819109", "item_title": "混成这样你们满意了吧", "item_duration": 0, "nick_name": "闹什么^",
         "fans_cnt": 2177, "play_cnt": 30209787, "publish_time": 1788586924, "score": 1217621, "media_type": 2,
         "item_url": "https://sf6-cdn-tos.douyinstatic.com/obj/tos-cn-ve-2774/images-bundle"},
    ]},
}
DOUHOT_TOPIC = {"taxonomy": None, "status": 200, "code": 0, "msg": "", "body": {}, "data": {"page": {"total": 100}, "objs": [
    {"challenge_id": "1606465689061379", "challenge_name": "续火花", "play_cnt": 14525489264, "publish_cnt": 420257,
     "cover_url": "https://p11-sign.douyinpic.com/x.jpeg", "score": 15278516, "avg_play_cnt": 34563, "create_time": 1532045068},
]}}
DOUHOT_SEARCH = {"taxonomy": None, "code": 0, "data": {"page_num": 1, "page_size": 30, "total_count": 10000, "search_list": [
    {"key_word": "早春晴朗", "search_score": 183467758, "trends": [{"date": "20260826", "value": 1}]},
]}}
PUBLIC = {"title": "抖音热点榜", "count": 2, "items": [
    {"id": "7679771371484867429", "title": "让屁桃帮忙选盲盒 #小猫vlog#带猫出门",
     "cover": "https://p3-pc-sign.douyinpic.com/a.jpeg",
     "lines": ["03:43", "7353", "让屁桃帮忙选盲盒 #小猫vlog#带猫出门", "@一颗小桃桃（大屁桃）", "· 8月30日"]},
    {"id": "7677881294382819770", "title": "翻唱", "cover": "", "lines": ["00:25", "3.9万", "翻唱", "@大都都音乐会", "· 8月25日"]},
]}
RESOLVE = {"direct": ["https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4",
                      "https://v26-web.douyinvod.com/75fe/video/tos/cn/x/?a=6383&mime_type=video_mp4&__vid=7681818700668891647"],
           "found": [], "title": "", "url": "https://www.douyin.com/video/7681818700668891647"}


# ── pure source parsing ─────────────────────────────────────────────────────
def test_douhot_video_topic_search_parse():
    items, extra = DOUHOT.parse(DOUHOT, DOUHOT.board("video_all"), DOUHOT_VIDEO)
    assert [i.id for i in items] == ["7682944569603926010", "7682000000000000001", "7681922343069819109"]
    # an image post on a video board keeps its metrics but never a media_url
    assert items[2].media_url is None and items[2].extra["post_type"] == "images" and items[2].duration_sec is None
    first = items[0]
    assert first.kind == "video" and first.rank == 1 and first.duration_sec == 35.8
    assert first.author == "涛涛很会玩" and first.author_followers == 5939 and first.play_count == 23482854
    assert first.like_count == 659302 and first.heat == 3372350.0
    assert first.topics == ["自由潜湿衣的重要性", "自由潜教练"]
    assert first.media_url.startswith("https://v26-cold.douyinvod.com/") and items[1].media_url is None
    assert first.url == "https://www.douyin.com/video/7682944569603926010"
    assert first.published_at.startswith("2026-09-07")
    assert extra["taxonomy"][0]["label"] == "美食" and extra["total"] == 1000

    topics, _ = DOUHOT.parse(DOUHOT, DOUHOT.board("topic"), DOUHOT_TOPIC)
    assert topics[0].kind == "topic" and topics[0].title == "续火花" and topics[0].extra["publish_cnt"] == 420257
    words, extra = DOUHOT.parse(DOUHOT, DOUHOT.board("search"), DOUHOT_SEARCH)
    assert words[0].kind == "search" and words[0].id == "早春晴朗" and words[0].heat == 183467758.0 and extra["total"] == 10000


def test_douhot_errors_are_source_errors():
    with pytest.raises(SourceError, match="redirected"):
        DOUHOT.parse(DOUHOT, DOUHOT.board("video_all"), {"error": "redirected: https://open.douyin.com/..."})
    with pytest.raises(SourceError, match="code=5"):
        DOUHOT.parse(DOUHOT, DOUHOT.board("video_all"), {"code": 5, "msg": "参数不合法", "data": None})


def test_douhot_plan_carries_category_and_waits_for_the_signed_request():
    plan = DOUHOT.plan(DOUHOT, DOUHOT.board("video_like"), 168, "美食教程", 50)
    assert plan.url.startswith("https://douhot.douyin.com/square/hotspot")
    assert plan.wait_resource == "douhot/v1/material/video_billboard"
    assert '"sub_type": 1005' in plan.expression and '"window": 168' in plan.expression and "美食教程" in plan.expression
    assert "children" in plan.expression  # second-level tags, a first level alone barely filters


def test_public_parse_and_media_pick():
    items, extra = DOUYIN_PUBLIC.parse(DOUYIN_PUBLIC, DOUYIN_PUBLIC.board("video"), PUBLIC)
    assert items[0].duration_sec == 223.0 and items[0].like_count == 7353 and items[0].author == "一颗小桃桃（大屁桃）"
    assert items[0].published_at == "8月30日" and items[0].topics == ["小猫vlog", "带猫出门"] and items[0].media_url is None
    assert items[1].like_count == 39000 and items[1].cover_url is None
    with pytest.raises(SourceError, match="no videos"):
        DOUYIN_PUBLIC.parse(DOUYIN_PUBLIC, DOUYIN_PUBLIC.board("video"), {"items": [], "title": "x"})
    # the static h265 probe clip is not the video; the douyinvod link is
    assert pick_media_url(RESOLVE).startswith("https://v26-web.douyinvod.com/")
    assert pick_media_url({"direct": [], "found": []}) is None


def test_desktop_page_command_and_output():
    payload = desktop_page.build_payload(url="https://douhot.douyin.com/x", expression="1+1", wait_resource="a/b")
    cmd = desktop_page.build_command(payload)
    assert cmd.startswith(": obx-trends-page-eval; printf %s ") and "| base64 -d | python3" in cmd
    assert desktop_page.parse_output("noise\n{\"ok\": true, \"value\": {\"a\": 1}}\n")["value"] == {"a": 1}
    with pytest.raises(ValueError):
        desktop_page.parse_output("nothing")


# ── service with a fake desktop ─────────────────────────────────────────────
@pytest.fixture
def world(monkeypatch):
    state = {"runs": [], "value": DOUHOT_VIDEO, "sites": {"douyin_hot": "bound"}, "fail": None, "mode": "local"}

    async def run_page(caller, *, url, expression, wait_resource, settle_s, summary):
        state["runs"].append({"url": url, "summary": summary, "workspace": caller.workspace_id})
        if state["fail"]:
            raise state["fail"]
        return {"ok": True, "value": state["value"], "title": "t", "final_url": url, "load_s": 1.2}

    async def site_status(workspace_id, site_key):
        return state["sites"].get(site_key, "none")

    async def get_mode(user_id):
        return state["mode"]

    monkeypatch.setattr(ts, "run_page", run_page)
    monkeypatch.setattr(ts, "site_status", site_status)
    monkeypatch.setattr("session.browser_pref.get_browser_mode", get_mode)
    monkeypatch.setenv("BILLING_MODE", "shadow")
    ts._locks.clear()
    return state


def _ctx(workspace=None, user=None):
    return ToolContext(user_id=user or "u_" + uuid.uuid4().hex[:8], workspace_id=workspace or "w_" + uuid.uuid4().hex[:8],
                       session_id="", message_id="m1", part_id="p1")


def _kv(result):
    return dict(line.split("=", 1) for line in result.output.splitlines()[:4] if "=" in line and " " not in line.split("=", 1)[0])


async def test_same_board_same_day_is_collected_once_for_everyone(world, monkeypatch):
    monkeypatch.setattr(ts, "day_key", lambda when=None: "2026-09-10-" + uuid.uuid4().hex[:6])  # isolate this test's keys
    fixed = ts.day_key()
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    a = await execute(HotTrendsArgs(source="douhot", board="video_all", window_hours=72, category="美食", limit=1), _ctx())
    assert a.title.startswith("Hot trends · 抖音热点宝 · 视频总榜")
    assert a.metadata["cached"] is False and len(a.metadata["items"]) == 1 and a.metadata["credits"] == "0.2"
    assert "credits=0.2" in a.output and world["runs"][-1]["summary"] == "hot_trends douhot video_all 72h 美食"
    # another workspace, same key: served from the shared snapshot, no desktop run, nothing billed
    b = await execute(HotTrendsArgs(source="douhot", board="video_all", window_hours=72, category="美食", limit=5), _ctx())
    assert b.metadata["cached"] is True and b.metadata["snapshot_id"] == a.metadata["snapshot_id"]
    assert len(b.metadata["items"]) == 3 and len(world["runs"]) == 1 and "credits=" not in b.output
    # refresh inside the minimum interval keeps the fresh copy instead of driving the desktop again
    c = await execute(HotTrendsArgs(source="douhot", board="video_all", window_hours=72, category="美食", refresh=True), _ctx())
    assert c.metadata["cached"] is True and len(world["runs"]) == 1
    # a different board is a different key
    world["value"] = DOUHOT_TOPIC
    d = await execute(HotTrendsArgs(source="douhot", board="topic"), _ctx())
    assert d.metadata["cached"] is False and d.metadata["items"][0]["kind"] == "topic" and len(world["runs"]) == 2


async def test_auto_falls_back_to_public_when_douhot_is_not_bound(world, monkeypatch):
    fixed = "2026-09-10-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    world["sites"] = {"douyin_hot": "none"}
    world["value"] = PUBLIC
    r = await execute(HotTrendsArgs(), _ctx())
    assert r.metadata["source"] == "douyin_public" and world["runs"][-1]["url"] == "https://www.douyin.com/hot"
    assert "action=resolve" in r.output
    refused = await execute(HotTrendsArgs(source="douhot"), _ctx())
    assert refused.metadata.get("refused") and "授权中心" in refused.output and len(world["runs"]) == 1
    refused = await execute(HotTrendsArgs(source="douyin_public", category="美食"), _ctx())
    assert "不支持按类目" in refused.output


async def test_workspace_without_douhot_still_reads_todays_shared_douhot_snapshot(world, monkeypatch):
    fixed = "2026-09-10-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    bound = _ctx()
    a = await execute(HotTrendsArgs(), bound)  # bound workspace collects 热点宝 total board
    assert a.metadata["source"] == "douhot" and a.metadata["cached"] is False
    world["sites"] = {"douyin_hot": "none"}
    other = _ctx()
    # auto from an unbound workspace: today's 热点宝 snapshot is shared, no desktop driven, nothing billed
    b = await execute(HotTrendsArgs(), other)
    assert b.metadata["source"] == "douhot" and b.metadata["cached"] is True and b.metadata["snapshot_id"] == a.metadata["snapshot_id"]
    assert len(world["runs"]) == 1
    # explicit douhot read also works from cache; a board nobody collected is refused with the authorisation hint
    c = await execute(HotTrendsArgs(source="douhot"), other)
    assert c.metadata["cached"] is True
    d = await execute(HotTrendsArgs(source="douhot", board="video_like"), other)
    assert d.metadata.get("refused") and "授权中心" in d.output and len(world["runs"]) == 1
    # refresh from the unbound workspace cannot drive 热点宝: auto falls through to its own public collection
    world["value"] = PUBLIC
    e = await execute(HotTrendsArgs(refresh=True), other)
    assert e.metadata["source"] == "douyin_public" and e.metadata["cached"] is False and len(world["runs"]) == 2


async def test_failure_is_visible_stored_and_not_retried_inside_the_interval(world, monkeypatch):
    fixed = "2026-09-10-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    world["value"] = {"error": "page issued no signed video request (not logged in, or page changed)"}
    r = await execute(HotTrendsArgs(source="douhot"), _ctx())
    assert r.metadata.get("refused") and "采集 抖音热点宝·视频总榜 失败" in r.output and "not logged in" in r.output
    again = await execute(HotTrendsArgs(source="douhot"), _ctx())
    assert "刚采集失败" in again.output and len(world["runs"]) == 1
    # after the interval the failed row is retried and overwritten by a good snapshot
    from core.config import get_config
    monkeypatch.setattr(get_config().hot_trends, "min_interval_seconds", 10)
    row = await ts._get_snapshot(ts.cache_key("douhot", "video_all", 24, None, fixed))
    from db.base import get_db_session
    from db.models.hot_trend import HotTrendSnapshot
    async with get_db_session() as db:
        db_row = await db.get(HotTrendSnapshot, row.id)
        db_row.fetched_at = db_row.fetched_at - timedelta(seconds=60)
    world["value"] = DOUHOT_VIDEO
    ok = await execute(HotTrendsArgs(source="douhot"), _ctx())
    assert ok.metadata["cached"] is False and ok.metadata["snapshot_id"] == row.id and len(world["runs"]) == 2


async def test_daily_fetch_budget_and_desktop_errors(world, monkeypatch):
    fixed = "2026-09-10-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    from core.config import get_config
    monkeypatch.setattr(get_config().hot_trends, "max_fetches_per_day", 1)
    await execute(HotTrendsArgs(source="douhot", board="video_all"), _ctx())
    r = await execute(HotTrendsArgs(source="douhot", board="video_like"), _ctx())
    assert "已达上限 1" in r.output and len(world["runs"]) == 1
    monkeypatch.setattr(get_config().hot_trends, "max_fetches_per_day", 48)
    from platforms.desktop.service import DesktopBusy
    world["fail"] = DesktopBusy("desktop is in use")
    r = await execute(HotTrendsArgs(source="douhot", board="video_like"), _ctx())
    assert r.metadata.get("refused") and "DesktopBusy" in r.output


async def test_resolve_uses_douhot_media_link_then_public_page(world, monkeypatch):
    fixed = "2026-09-10-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    ctx = _ctx()
    await execute(HotTrendsArgs(source="douhot"), ctx)  # mirrors item_url into hot_media_links
    r = await execute(HotTrendsArgs(action="resolve", video="https://www.douyin.com/video/7682944569603926010"), ctx)
    assert r.metadata["cached"] is True and r.metadata["media_url"].startswith("https://v26-cold.douyinvod.com/")
    n = len(world["runs"])
    world["value"] = RESOLVE
    r = await execute(HotTrendsArgs(action="resolve", video="7681818700668891647"), ctx)
    assert r.metadata["cached"] is False and r.metadata["media_url"].startswith("https://v26-web.douyinvod.com/")
    assert world["runs"][-1]["url"] == "https://www.douyin.com/video/7681818700668891647" and len(world["runs"]) == n + 1
    r2 = await execute(HotTrendsArgs(action="resolve", video="7681818700668891647"), ctx)
    assert r2.metadata["cached"] is True and len(world["runs"]) == n + 1
    world["value"] = {"direct": [], "found": []}
    r3 = await execute(HotTrendsArgs(action="resolve", video="7000000000000000000"), ctx)
    assert r3.metadata.get("refused") and "没有可用直链" in r3.output
    bad = await execute(HotTrendsArgs(action="resolve", video="not a video"), ctx)
    assert "不是抖音视频 id" in bad.output


async def test_sources_lists_entitlement_and_cached_taxonomy(world, monkeypatch):
    fixed = "2026-09-10-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    r = await execute(HotTrendsArgs(action="sources"), _ctx())
    assert r.metadata["sources"][0]["source"] == "douhot" and r.metadata["sources"][0]["usable"] is True
    assert r.metadata["sources"][1]["source"] == "douyin_public" and r.metadata["sources"][1]["usable"] is True
    await execute(HotTrendsArgs(source="douhot"), _ctx())
    r = await execute(HotTrendsArgs(action="sources"), _ctx())
    assert r.metadata["sources"][0]["categories"][0]["label"] == "美食" and "美食[美食教程,吃播]" in r.output
    world["mode"] = "remote"
    r = await execute(HotTrendsArgs(), _ctx())
    assert r.metadata.get("applicable") is False


def test_args_validation_and_registration():
    with pytest.raises(ValidationError):
        HotTrendsArgs(action="resolve")
    with pytest.raises(ValidationError, match="unknown source"):
        HotTrendsArgs(source="weibo")
    with pytest.raises(ValidationError, match="unknown board"):
        HotTrendsArgs(source="douhot", board="nope")
    assert get_source("douhot").board("search_rising").code == 3002
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    from agent.tool_exposure import INTENT_PACKS
    from tool.hot_trends import hot_trends_tool
    assert "hot_trends" in BUILD_ONLY_WORKFLOW_TOOLS and "hot_trends" in AGENTS["build"].tools
    assert "hot_trends" in INTENT_PACKS["video"]
    assert hot_trends_tool.sandbox_required is False and hot_trends_tool.parallel_safe is False
