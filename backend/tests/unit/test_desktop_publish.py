"""desktop_publish: mode switch, budget, breaker, degrade path, job records, script builder."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import publish.desktop_policy as policy
import publish.desktop_service as svc
from publish import desktop_script as script
from tool.desktop_publish import DesktopPublishArgs, execute
from tool.tool import ToolContext

SH = policy.SHANGHAI


def _now_sh(hour: int) -> datetime:
    return datetime(2026, 9, 10, hour, 30, tzinfo=SH).astimezone(timezone.utc)


# ── pure policy ─────────────────────────────────────────────────────────────
def test_mode_resolution_breaker_wins_then_request_then_default():
    from types import SimpleNamespace as NS
    assert policy.resolve_mode(None, None, "auto")[0] == "auto"
    assert policy.resolve_mode(None, None, "package")[0] == "package"
    assert policy.resolve_mode("package", NS(auto_publish_disabled_at=None), "auto")[0] == "package"
    assert policy.resolve_mode("auto", NS(auto_publish_disabled_at=None), "package")[0] == "auto"
    mode, reason = policy.resolve_mode("auto", NS(auto_publish_disabled_at=datetime.now(timezone.utc), auto_publish_disabled_reason="风控信号「验证码」"), "auto")
    assert mode == "package" and "验证码" in reason


def test_window_and_risk_patterns():
    assert policy.in_window(_now_sh(9), 8, 23) and not policy.in_window(_now_sh(23), 8, 23) and not policy.in_window(_now_sh(7), 8, 23)
    nxt = policy.next_window_start(_now_sh(23), 8, 23).astimezone(SH)
    assert (nxt.day, nxt.hour) == (11, 8)
    assert policy.risk_signal("系统检测到操作过于频繁，请完成滑动验证", ["验证码", "滑动验证"]) == "滑动验证"
    assert policy.risk_signal("作品未见异常", ["验证码"]) is None


def test_script_params_command_and_parse():
    p = script.build_params(file_path="/workspace/uploads/a.mp4", title="t", intro="i", topics=["装修"], declaration="ai",
                            hot_word=None, visibility="private", schedule_at=None, dry_run=True, simulate_risk=False,
                            risk_patterns=["验证码"], upload_timeout_seconds=300, evidence_path="tmp/e.png")
    assert p["declaration_label"] == "内容由AI生成" and p["visibility_label"] == "仅自己可见" and p["upload_timeout_ms"] == 300000
    cmd = script.build_command(p)
    assert cmd.startswith(": obx-desktop-publish; cd /opt/openbox/skills/dev-browser") and "npx tsx tmp/obx-publish.ts" in cmd
    assert len(cmd) < 8000  # gzip keeps it well under the action-server command limits
    assert script.parse_output("noise\nOBX_RESULT {\"ok\": true, \"item_id\": \"1\"}")["item_id"] == "1"
    with pytest.raises(ValueError):
        script.parse_output("nothing")


def test_args_validation():
    with pytest.raises(ValidationError):
        DesktopPublishArgs(action="publish", asset_id="a")
    with pytest.raises(ValidationError):
        DesktopPublishArgs(action="publish", asset_id="a", title="t", schedule_at="tomorrow 10:00")
    with pytest.raises(ValidationError):
        DesktopPublishArgs(action="publish", asset_id="a", title="t", topics=["#装修"])
    DesktopPublishArgs(action="publish", asset_id="a", title="t", schedule_at="2026-09-11 10:00", topics=["装修"])


# ── service with a fake desktop ─────────────────────────────────────────────
@pytest.fixture
async def world(monkeypatch):
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.platform_account import PlatformAccount

    state = {"result": None, "runs": [], "prepare_fail": None}
    ws = "w_" + uuid.uuid4().hex[:8]
    uid = "u_" + uuid.uuid4().hex[:8]
    aid = "asset_" + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(FileAsset(id=aid, user_id=uid, workspace_id=ws, session_id="s1", project_id=None, name="final.mp4",
                         oss_key=f"assets/{uid}/{aid}/final.mp4", mime="video/mp4", size=10, status="ready", source="agent",
                         transient=False, created_at=now))
        acct = PlatformAccount(id="pa_" + uuid.uuid4().hex[:8], workspace_id=ws, bound_by_user_id=uid, platform="douyin_creator",
                               auth_kind="desktop_cookie", external_id="uid-1", nickname="测试号", status="bound", desktop_id="ecd-test",
                               bound_at=now, created_at=now, updated_at=now)
        db.add(acct)
    state.update({"ws": ws, "uid": uid, "aid": aid, "acct": acct.id})

    async def workspace_desktop(workspace_id):
        return {"desktop_id": "ecd-test", "workspace_id": workspace_id}

    async def prepare_desktop(caller, record, asset):
        if state["prepare_fail"]:
            raise state["prepare_fail"]
        return object(), f"/workspace/uploads/{asset.name}"

    async def run_script(caller, record, params, *, timeout_s):
        state["runs"].append(params)
        return state["result"]

    monkeypatch.setattr("platforms.desktop.service.workspace_desktop", workspace_desktop)
    monkeypatch.setattr(svc, "prepare_desktop", prepare_desktop)
    monkeypatch.setattr(svc, "run_script_on_desktop", run_script)
    # deterministic clock inside the posting window
    monkeypatch.setattr(svc, "_now", lambda: _now_sh(10))
    monkeypatch.setattr(policy, "_aware", policy._aware)
    return state


def _ctx(w):
    return ToolContext(user_id=w["uid"], workspace_id=w["ws"], session_id="s1", message_id="m1", part_id="p1")


OK_RESULT = {"ok": True, "step": "readback", "steps": ["goto", "upload", "publish", "readback"], "item_id": "7681000000000000001",
             "item_url": "https://www.douyin.com/video/7681000000000000001", "declaration_row": "内容由AI生成",
             "visibility": {"found": True, "checked": True}, "upload": {"ms": 2000}, "publish_ms": 1200,
             "summary": {"title": "标题", "counters": ["2/30"]}, "evidence": "tmp/e.png", "final_url": "https://creator.douyin.com/creator-micro/content/manage"}


async def test_precheck_then_publish_records_job_and_counts_budget(world, monkeypatch):
    from db.base import get_db_session
    from db.models.notification import Notification
    from sqlalchemy import select

    ctx = _ctx(world)
    pre = await execute(DesktopPublishArgs(action="precheck"), ctx)
    assert pre.metadata["mode"] == "auto" and pre.metadata["login_ok"] and pre.metadata["can_auto_publish"] is True
    assert pre.metadata["budget"]["today"] == 0
    world["result"] = OK_RESULT
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="标题", intro="简介", topics=["装修"],
                                         visibility="private"), ctx)
    assert r.metadata["status"] == "published" and r.metadata["item_id"] == "7681000000000000001"
    params = world["runs"][-1]
    assert params["file_path"] == "/workspace/uploads/final.mp4" and params["visibility_label"] == "仅自己可见" and params["topics"] == ["装修"]
    st = await execute(DesktopPublishArgs(action="status"), ctx)
    assert st.metadata["jobs"][0]["status"] == "published" and st.metadata["jobs"][0]["item_id"] == "7681000000000000001"
    # the account's budget now shows one post today, and the interval blocks an immediate second post
    pre2 = await execute(DesktopPublishArgs(action="precheck"), ctx)
    assert pre2.metadata["budget"]["today"] == 1 and pre2.metadata["budget"]["allowed"] is False and "分钟" in pre2.metadata["budget"]["reason"]
    blocked = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="第二条"), ctx)
    assert blocked.metadata["refused"] and blocked.metadata["next_allowed_at"] and len(world["runs"]) == 1
    async with get_db_session() as db:
        kinds = (await db.execute(select(Notification.kind).where(Notification.workspace_id == world["ws"]))).scalars().all()
    assert "publish_done" in kinds


async def test_dry_run_is_recorded_as_draft_and_not_counted(world):
    ctx = _ctx(world)
    world["result"] = {**OK_RESULT, "step": "save_draft", "item_id": None, "final_url": "https://creator.douyin.com/creator-micro/content/upload"}
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="演练", dry_run=True), ctx)
    assert r.metadata["status"] == "draft" and world["runs"][-1]["dry_run"] is True
    pre = await execute(DesktopPublishArgs(action="precheck"), ctx)
    assert pre.metadata["budget"]["today"] == 0 and pre.metadata["can_auto_publish"] is True


async def test_risk_signal_trips_breaker_degrades_and_notifies(world):
    from db.base import get_db_session
    from db.models.notification import Notification
    from db.models.platform_account import PlatformAccount
    from sqlalchemy import select

    ctx = _ctx(world)
    world["result"] = {"ok": False, "step": "publish", "steps": ["goto", "upload", "publish"], "risk": "滑动验证",
                       "error": "risk signal after publish click: 滑动验证", "evidence": "tmp/e.png", "risk_evidence": "tmp/e-risk.png"}
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="标题"), ctx)
    assert r.metadata["refused"] and r.metadata["degrade"] is True and "douyin_publish" in r.output and "滑动验证" in r.output
    async with get_db_session() as db:
        acct = await db.get(PlatformAccount, world["acct"])
        assert acct.auto_publish_disabled_at is not None and "滑动验证" in acct.auto_publish_disabled_reason
        kinds = (await db.execute(select(Notification.kind).where(Notification.workspace_id == world["ws"]))).scalars().all()
    assert "desktop_publish_degraded" in kinds
    # breaker: the next attempt is package mode without touching the desktop
    pre = await execute(DesktopPublishArgs(action="precheck"), ctx)
    assert pre.metadata["mode"] == "package" and pre.metadata["can_auto_publish"] is False
    again = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="标题"), ctx)
    assert again.metadata["degrade"] is True and len(world["runs"]) == 1
    st = await execute(DesktopPublishArgs(action="status"), ctx)
    assert st.metadata["jobs"][0]["status"] == "failed" and "risk" in st.metadata["jobs"][0]["error"]
    # a person re-enables
    en = await execute(DesktopPublishArgs(action="enable_auto"), ctx)
    assert en.metadata["enabled"] is True
    assert (await execute(DesktopPublishArgs(action="precheck"), ctx)).metadata["mode"] == "auto"


async def test_login_expired_and_package_default_and_foreign_asset(world, monkeypatch):
    from core.config import get_config
    from db.base import get_db_session
    from db.models.platform_account import PlatformAccount

    ctx = _ctx(world)
    world["result"] = {"ok": False, "step": "goto", "steps": ["goto"], "login_expired": True, "error": "not logged in: https://creator.douyin.com/login"}
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="标题"), ctx)
    assert r.metadata["login_expired"] is True and r.metadata["refused"]
    # account marked expired by the probe → refused before any desktop work
    async with get_db_session() as db:
        acct = await db.get(PlatformAccount, world["acct"]); acct.status = "expired"
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="标题"), ctx)
    assert r.metadata["login_expired"] is True and len(world["runs"]) == 1 and "desktop_login" in r.output
    async with get_db_session() as db:
        acct = await db.get(PlatformAccount, world["acct"]); acct.status = "bound"
    # deployment default = package → degrade without desktop
    monkeypatch.setattr(get_config().desktop_publish, "default_mode", "package")
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="标题"), ctx)
    assert r.metadata["degrade"] is True and len(world["runs"]) == 1
    # someone else's asset / too long title are refused before any desktop work
    r = await execute(DesktopPublishArgs(action="publish", asset_id="asset_nope", title="标题", mode="auto"), _ctx({**world, "uid": "u_other"}))
    assert r.metadata["refused"] and "asset_id" in r.output
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="一" * 31, mode="auto"), ctx)
    assert "上限 30" in r.output and len(world["runs"]) == 1
    # explicit auto from a template overrides the package default
    world["result"] = OK_RESULT
    r = await execute(DesktopPublishArgs(action="publish", asset_id=world["aid"], title="标题", mode="auto"), ctx)
    assert r.metadata["status"] == "published"
    monkeypatch.setattr(get_config().desktop_publish, "default_mode", "auto")


async def test_registration_and_skill():
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    from agent.tool_exposure import INTENT_PACKS
    from skill import skill as sk
    from tool.desktop_publish import desktop_publish_tool

    assert "desktop_publish" in BUILD_ONLY_WORKFLOW_TOOLS and "desktop_publish" in AGENTS["build"].tools
    assert "desktop_publish" in INTENT_PACKS["video"]
    assert desktop_publish_tool.sandbox_required is False and desktop_publish_tool.parallel_safe is False
    names = {s.name: s for s in await sk.list_skills()}
    assert "douyin-desktop-publish" in names
    body = names["douyin-desktop-publish"].content
    assert "desktop_publish" in body and "douyin_publish" in body and "≤ 30" in body
