"""云电脑登录态: site catalogue, cookie/probe judgement, and the probe use cases."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.notification import Notification
from db.models.platform_account import PlatformAccount
from platforms.desktop import cdp, service, sites

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())


# ── Catalogue & script ─────────────────────────────────────────────────────
def test_catalogue_matches_reconnaissance():
    keys = {s.key for s in sites.list_sites()}
    assert keys == {"douyin_creator", "douyin_laike", "meituan_merchant", "xiaohongshu_creator"}
    creator = sites.get_site("douyin_creator")
    assert "sessionid" in creator.session_cookies and creator.session_probe.expired_values == (8,)
    laike = sites.get_site("douyin_laike")
    assert laike.session_probe.nickname_path == "data.name" and 4000100 in laike.session_probe.expired_values
    meituan = sites.get_site("meituan_merchant")
    assert meituan.session_probe.code_path == "error.code" and None in meituan.session_probe.ok_values
    assert sites.get_site("xiaohongshu_creator").recon_pending
    payload = sites.site_payload(creator)
    assert payload["host"] == "creator.douyin.com" and payload["session_probe"]["url"].endswith("unread_count/")


def test_script_embeds_payload_and_never_prints_cookie_values():
    payload = cdp.build_payload("probe", [sites.site_payload(sites.get_site("douyin_laike"))], level=2, profile=True)
    script = cdp.build_script(payload)
    assert "__PAYLOAD__" not in script and "Storage.getCookies" in script
    # Only names and expiry leave the desktop.
    assert 'c.get("value")' not in script and 'c["value"]' not in script
    command = cdp.build_command(payload)
    assert command.startswith(": obx-login-probe;") and "| python3" in command
    assert len(script) < 16 * 1024


# ── Judgement ──────────────────────────────────────────────────────────────
def _rec(**over):
    base = {
        "cookie_ok": True, "missing": [], "expired": [], "cookie_count": 40,
        "session_cookies": {"sessionid": [TS + 86400 * 60], "sid_tt": [TS + 86400 * 60],
                            "uid_tt": [TS + 86400 * 60], "passport_auth_status": [TS + 86400 * 30]},
        "earliest_expiry": TS + 86400 * 30,
    }
    base.update(over)
    return base


def test_cookie_only_verdicts():
    creator = sites.get_site("douyin_creator")
    assert cdp.judge_site(creator, _rec()).status == "bound"
    v = cdp.judge_site(creator, _rec(cookie_ok=False, missing=["sessionid"]))
    assert v.status == "expired" and "sessionid" in v.reason
    xhs = sites.get_site("xiaohongshu_creator")
    assert cdp.judge_site(xhs, {"cookie_ok": False, "missing": [], "expired": []}).status == "unknown"


def test_level2_overrides_cookies_and_extracts_profile():
    laike = sites.get_site("douyin_laike")
    ok = cdp.judge_site(laike, _rec(via="tab", probe={"status": 200, "code": 0, "nickname": "用户A", "uid": "819", "display": {"role": "商家子账号"}},
                                    profile={"status": 200, "code": 0, "display": {"account_name": "芊屿店"}}))
    assert ok.status == "bound" and ok.nickname == "用户A" and ok.uid == "819"
    assert ok.display == {"role": "商家子账号", "account_name": "芊屿店"}
    gone = cdp.judge_site(laike, _rec(probe={"status": 200, "code": 4000100}))
    assert gone.status == "expired" and "4000100" in gone.reason
    # A failed probe is inconclusive, so the cookie verdict stands.
    flaky = cdp.judge_site(laike, _rec(probe={"error": "TypeError: Failed to fetch"}))
    assert flaky.status == "bound"
    meituan = sites.get_site("meituan_merchant")
    assert cdp.judge_site(meituan, _rec(session_cookies={"edper": [TS + 86400 * 400]}, probe={"status": 200, "code": "__MISSING__", "display": {"shop_id": "1"}})).status == "bound"
    assert cdp.judge_site(meituan, _rec(session_cookies={"edper": [TS + 86400 * 400]}, probe={"status": 200, "code": 10008})).status == "expired"


def test_parse_output_takes_last_json_line():
    assert cdp.parse_output("noise\n{\"action\":\"probe\",\"sites\":{}}\n")["action"] == "probe"
    with pytest.raises(ValueError):
        cdp.parse_output("nothing here")


# ── Service with a fake desktop ────────────────────────────────────────────
class FakeDesktop:
    """Stands in for the desktop + Chrome: returns a scripted probe result."""

    def __init__(self, result: dict):
        self.result = result
        self.calls: list[dict] = []

    async def run(self, record, payload, *, lease, session_id="auth-center", tool_call_id=""):
        self.calls.append(payload)
        return {"action": payload["action"], "chrome": "Chrome/151", "targets": 3, "error": None,
                "sites": {s["key"]: self.result.get(s["key"], {"cookie_ok": False, "missing": ["x"], "expired": []}) for s in payload["sites"]}}


@pytest.fixture
def fake_desktop(monkeypatch):
    holder = {"desktop": FakeDesktop({}), "record": {"desktop_id": "ecd-test-1", "user_id": "user-1", "tunnel_state": "up"}}

    async def fake_workspace_desktop(workspace_id):
        return holder["record"]

    async def fake_run(record, payload, **kw):
        return await holder["desktop"].run(record, payload, **kw)

    monkeypatch.setattr(service, "workspace_desktop", fake_workspace_desktop)
    monkeypatch.setattr(service, "run_on_desktop", fake_run)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    return holder


@pytest.mark.asyncio
async def test_probe_registers_logged_in_sites_and_notifies_on_expiry(fake_desktop):
    ws = f"ws-{uuid4().hex[:8]}"
    fake_desktop["desktop"] = FakeDesktop({
        "douyin_creator": _rec(via="tab", probe={"status": 200, "code": 0}, profile={"status": 200, "code": 0, "nickname": "创作者甲", "uid": "dy123"}),
        "douyin_laike": _rec(probe={"status": 200, "code": 0, "nickname": "用户乙", "uid": "819"}),
    })
    rows = await service.probe_workspace(ws, user_id="user-1", level=2)
    by = {r.platform: r for r in rows}
    assert set(by) == {"douyin_creator", "douyin_laike"}  # meituan/xhs not logged in → not registered
    assert by["douyin_creator"].status == "bound" and by["douyin_creator"].nickname == "创作者甲"
    assert by["douyin_creator"].external_id == "dy123" and by["douyin_creator"].desktop_id == "ecd-test-1"
    assert by["douyin_laike"].probe_detail["last_level2_at"] == NOW.isoformat()
    payload = fake_desktop["desktop"].calls[-1]
    assert payload["level"] == 2 and payload["profile"] is True
    # The recon-pending site never gets a level-2 probe.
    xhs = next(s for s in payload["sites"] if s["key"] == "xiaohongshu_creator")
    assert xhs["session_probe"] is None

    # The 6-hourly tick asks for level 1: cookie-only, no site endpoint touched.
    fake_desktop["desktop"] = FakeDesktop({"douyin_creator": _rec(), "douyin_laike": _rec()})
    await service.probe_workspace(ws, user_id="user-1", level=1)
    payload = fake_desktop["desktop"].calls[-1]
    assert payload["level"] == 1 and all(s["session_probe"] is None for s in payload["sites"])

    # A second level-2 run the same day: registered sites are not due again,
    # only the still-unregistered meituan site may be discovered.
    fake_desktop["desktop"] = FakeDesktop({"douyin_creator": _rec(), "douyin_laike": _rec()})
    await service.probe_workspace(ws, user_id="user-1", level=2)
    payload = fake_desktop["desktop"].calls[-1]
    probed = {s["key"] for s in payload["sites"] if s["session_probe"] is not None}
    assert probed == {"meituan_merchant"}

    # Creator session dies on the server: level-2 forced by the 检测 button.
    fake_desktop["desktop"] = FakeDesktop({"douyin_creator": _rec(probe={"status": 200, "code": 8})})
    row = await service.probe_account(by["douyin_creator"])
    assert row.status == "expired" and "8" in row.last_error
    async with get_db_session() as db:
        notes = list((await db.execute(select(Notification).where(Notification.workspace_id == ws))).scalars())
        assert [n.kind for n in notes] == ["desktop_login_expired"]
    # Probing again the same day does not spam.
    await service.probe_account(by["douyin_creator"])
    async with get_db_session() as db:
        notes = list((await db.execute(select(Notification).where(Notification.workspace_id == ws))).scalars())
        assert len(notes) == 1
    extras = service.public_extras(row)
    assert extras["desktopId"] == "ecd-test-1" and extras["probeDetail"]["cookieOk"] is True
    assert "value" not in json.dumps(extras)


@pytest.mark.asyncio
async def test_desktop_change_resets_rows(fake_desktop):
    ws = f"ws-{uuid4().hex[:8]}"
    fake_desktop["desktop"] = FakeDesktop({"douyin_laike": _rec(probe={"status": 200, "code": 0, "nickname": "乙"})})
    await service.probe_workspace(ws, user_id="user-1", level=2)
    fake_desktop["record"] = {"desktop_id": "ecd-test-2", "user_id": "user-1", "tunnel_state": "up"}
    fake_desktop["desktop"] = FakeDesktop({})  # nothing logged in on the new machine
    rows = await service.probe_workspace(ws, user_id="user-1", level=1)
    laike = next(r for r in rows if r.platform == "douyin_laike")
    assert laike.desktop_id == "ecd-test-2" and laike.status == "expired" and laike.nickname is None
    async with get_db_session() as db:
        kinds = [n.kind for n in (await db.execute(select(Notification).where(Notification.workspace_id == ws))).scalars()]
        assert "desktop_login_reset" in kinds


@pytest.mark.asyncio
async def test_open_login_and_logout_and_offline(fake_desktop):
    ws = f"ws-{uuid4().hex[:8]}"
    row = await service.open_login(ws, "user-1", "meituan_merchant")
    assert row.status == "unknown" and row.probe_detail["login_opened_at"] == NOW.isoformat()
    assert fake_desktop["desktop"].calls[-1]["action"] == "open"

    class LogoutDesktop(FakeDesktop):
        async def run(self, record, payload, **kw):
            self.calls.append(payload)
            return {"action": "logout", "sites": {"meituan_merchant": {"deleted": 7}}, "error": None}

    fake_desktop["desktop"] = LogoutDesktop({})
    gone = await service.logout(row)
    assert gone.status == "revoked" and gone.probe_detail["cookies_deleted"] == 7

    # No desktop at all → rows flip to desktop_offline without a probe.
    fake_desktop["record"] = None
    rows = await service.probe_workspace(ws, user_id="user-1")
    assert rows and all(r.status == "desktop_offline" for r in rows)


def test_predicted_expiry_takes_the_earlier_bound():
    row = SimpleNamespace(
        auth_kind="desktop_cookie", status="bound", platform="douyin_creator",
        probe_detail={"earliest_expiry": TS + 86400 * 60}, last_ok_at=NOW, bound_at=NOW,
    )
    assert service.predicted_expiry(row) == NOW + timedelta(days=30)  # inactivity ttl wins
    row.probe_detail = {"earliest_expiry": TS + 86400 * 3}
    assert service.predicted_expiry(row) == NOW + timedelta(days=3)


def test_desktop_routes_are_not_shadowed_by_account_id_routes():
    """POST /desktop/probe must reach the desktop handler, not /{account_id}/probe."""
    from api.platform_accounts import router

    paths = [r.path for r in router.routes]
    assert paths.index("/api/platform-accounts/desktop/probe") < paths.index("/api/platform-accounts/{account_id}/probe")
    assert paths.index("/api/platform-accounts/desktop/{site}/open") < paths.index("/api/platform-accounts/{account_id}/probe")


@pytest.mark.asyncio
async def test_platform_catalogue_is_backward_compatible():
    """Old frontends only asked for OAuth platforms and read `capabilities` unguarded."""
    from api.platform_accounts import list_platforms

    legacy = await list_platforms(kinds="oauth")
    assert all(entry["kind"] == "oauth" and isinstance(entry["capabilities"], list) for entry in legacy)
    both = await list_platforms(kinds="oauth,desktop")
    desktop = [e for e in both if e["kind"] == "desktop"]
    assert {e["key"] for e in desktop} == {"douyin_creator", "douyin_laike", "meituan_merchant", "xiaohongshu_creator"}
    assert all(isinstance(e["capabilities"], list) and "configured" in e for e in desktop)
