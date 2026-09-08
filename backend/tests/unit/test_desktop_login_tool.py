"""desktop_login: the agent's view of cloud-desktop login state (status / open / probe)."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from platforms.desktop import service as desktop_service
from platforms.errors import PlatformError
from tool import desktop_login as mod
from tool.desktop_login import DesktopLoginArgs, desktop_login_tool, execute_desktop_login
from tool.tool import ToolContext

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _row(platform, status="bound", nickname="用户甲", error=None, display=None):
    return SimpleNamespace(
        id=f"pacc-{platform}", platform=platform, auth_kind="desktop_cookie", status=status, nickname=nickname,
        last_probe_at=NOW, last_ok_at=NOW, bound_at=NOW, last_error=error, desktop_id="ecd-1",
        probe_detail={"earliest_expiry": int(NOW.timestamp()) + 86400 * 30, "display": display or {}},
        external_id="x", union_id=None, avatar_url=None, scopes="", access_expires_at=None, refresh_expires_at=None,
        renew_count=0, last_refresh_at=None, bound_by_user_id="u", created_at=NOW, updated_at=NOW, deleted_at=None,
    )


@pytest.fixture
def world(monkeypatch):
    state = {"rows": [], "mode": "local", "desktop": {"desktop_id": "ecd-1"}, "opened": [], "probed": []}

    async def list_accounts(workspace_id):
        return state["rows"]

    async def get_mode(user_id):
        return state["mode"]

    async def workspace_desktop(workspace_id):
        return state["desktop"]

    async def open_login(ws, user, site):
        state["opened"].append(site)
        return SimpleNamespace(id="pacc-new", desktop_id="ecd-1", status="unknown")

    async def probe_workspace(ws, *, user_id, site_keys, level, force_level2, lease, session_id):
        state["probed"].append((site_keys, level))
        return [r for r in state["rows"] if r.platform in site_keys]

    import platforms.service as platform_service
    import session.browser_pref as pref

    monkeypatch.setattr(platform_service, "list_accounts", list_accounts)
    monkeypatch.setattr(pref, "get_browser_mode", get_mode)
    monkeypatch.setattr(desktop_service, "workspace_desktop", workspace_desktop)
    monkeypatch.setattr(desktop_service, "open_login", open_login)
    monkeypatch.setattr(desktop_service, "probe_workspace", probe_workspace)
    return state


def _ctx():
    return ToolContext(session_id="s1", user_id="user-1", workspace_id="ws-1", message_id="m1", part_id="p1")


def test_registered_build_only_and_skill_preface():
    assert desktop_login_tool.sandbox_required is False and desktop_login_tool.parallel_safe is False
    assert "user_id" not in DesktopLoginArgs.model_fields and "workspace_id" not in DesktopLoginArgs.model_fields
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    from tool.registry import get_tool, register_builtin_tools

    register_builtin_tools()
    assert get_tool("desktop_login") is not None
    assert "desktop_login" in AGENTS["build"].tools and "desktop_login" in BUILD_ONLY_WORKFLOW_TOOLS
    skill = (Path(__file__).resolve().parents[3] / "container" / "dev-browser" / "SKILL.md").read_text(encoding="utf-8")
    assert 'desktop_login(action="status"' in skill and "DESKTOP_LOGIN_REQUIRED" in skill


@pytest.mark.asyncio
async def test_status_reports_all_sites_and_flags_the_missing_one(world):
    world["rows"] = [_row("douyin_creator"), _row("douyin_laike", display={"account_name": "芊屿店"})]
    result = await execute_desktop_login(DesktopLoginArgs(action="status"), _ctx())
    assert result.metadata["sites"] == {"douyin_creator": "bound", "douyin_laike": "bound", "meituan_merchant": "none", "xiaohongshu_creator": "none"}
    assert "芊屿店" in result.output and "需要登录的站点" in result.output

    ok = await execute_desktop_login(DesktopLoginArgs(action="status", site="抖音来客"), _ctx())
    assert ok.metadata["status"] == "bound" and "可以直接" in ok.output

    need = await execute_desktop_login(DesktopLoginArgs(action="status", site="meituan_merchant"), _ctx())
    assert need.metadata["code"] == "DESKTOP_LOGIN_REQUIRED" and "action=open" in need.output


@pytest.mark.asyncio
async def test_status_is_not_applicable_in_extension_mode(world):
    world["mode"] = "remote"
    result = await execute_desktop_login(DesktopLoginArgs(action="status", site="douyin_creator"), _ctx())
    assert result.metadata["applicable"] is False and "extension" in result.output


@pytest.mark.asyncio
async def test_open_then_probe_round_trip(world):
    opened = await execute_desktop_login(DesktopLoginArgs(action="open", site="douyin_creator"), _ctx())
    assert world["opened"] == ["douyin_creator"] and "扫码" in opened.output and opened.metadata["account_id"] == "pacc-new"

    still = await execute_desktop_login(DesktopLoginArgs(action="probe", site="douyin_creator"), _ctx())
    assert still.metadata["code"] == "DESKTOP_LOGIN_REQUIRED" and world["probed"][-1] == (["douyin_creator"], 2)

    world["rows"] = [_row("douyin_creator", nickname="创作者乙")]
    done = await execute_desktop_login(DesktopLoginArgs(action="probe", site="douyin_creator"), _ctx())
    assert done.metadata["status"] == "bound" and "创作者乙" in done.output


@pytest.mark.asyncio
async def test_errors_are_structured(world, monkeypatch):
    bad = await execute_desktop_login(DesktopLoginArgs(action="status", site="weibo"), _ctx())
    assert bad.metadata["code"] == "SITE_UNKNOWN"
    missing = await execute_desktop_login(DesktopLoginArgs(action="open"), _ctx())
    assert missing.metadata["code"] == "SITE_REQUIRED"
    pending = await execute_desktop_login(DesktopLoginArgs(action="open", site="xiaohongshu_creator"), _ctx())
    assert pending.metadata["code"] == "SITE_NOT_SUPPORTED"

    async def offline(ws, user, site):
        raise desktop_service.DesktopUnavailable("workspace has no cloud desktop")

    monkeypatch.setattr(desktop_service, "open_login", offline)
    down = await execute_desktop_login(DesktopLoginArgs(action="open", site="douyin_creator"), _ctx())
    assert down.metadata["code"] == "DESKTOP_UNAVAILABLE"
    assert isinstance(PlatformError("x"), Exception) and mod.STATUS_LABEL["bound"] == "已登录"
