"""The desktop_login tool: is site X logged in on this workspace's cloud desktop?

Three actions, identity only from ToolContext:

* ``status`` — what the 授权中心 already knows (no desktop round trip): per
              site, bound / expired / unknown, nickname, when it was last checked.
* ``open``   — push the site's login page to the front of the cloud desktop so
              the person can scan there; the agent never logs in for them.
* ``probe``  — after the person says they scanned: one real check (cookies +
              the site's own JSON endpoint) and the updated verdict.

A site that is not ``bound`` comes back as the structured error
``DESKTOP_LOGIN_REQUIRED`` so the agent stops and guides instead of guessing
from screenshots.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from core.log import create_logger
from platforms.desktop import service as desktop_service
from platforms.desktop.sites import DesktopSite, get_site, list_sites
from platforms.errors import PlatformError
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.desktop_login")

STATUS_LABEL = {
    "bound": "已登录",
    "expired": "已失效",
    "unknown": "待确认",
    "desktop_offline": "云电脑离线",
    "revoked": "已退出",
    "none": "未登录",
}


class DesktopLoginArgs(BaseModel):
    action: Literal["status", "open", "probe"] = Field(
        description=(
            "status: what is known about the cloud desktop's logins (cheap, no desktop access); "
            "open: push a site's login page to the cloud desktop for the person to scan; "
            "probe: re-check one site right now after the person says they logged in."
        )
    )
    site: str | None = Field(
        default=None,
        description=(
            "Site key: douyin_creator (抖音创作者中心), douyin_laike (抖音来客), "
            "meituan_merchant (美团经营宝/点评商户平台), xiaohongshu_creator (小红书创作平台). "
            "Optional for status (all sites); required for open and probe."
        ),
    )


def _error(code: str, message: str, **extra) -> ToolResult:
    return ToolResult(
        title=f"desktop_login: {code}",
        output=message,
        metadata={"error": True, "code": code, **extra},
    )


def _resolve_site(raw: str | None) -> DesktopSite | None:
    if not raw:
        return None
    key = raw.strip().lower()
    try:
        return get_site(key)
    except KeyError:
        pass
    for site in list_sites():
        if raw.strip() in site.display or site.host in key:
            return site
    return None


def _fmt(when: datetime | None) -> str:
    if not when:
        return "从未"
    aware = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")


def _describe(site: DesktopSite, row, public: dict | None) -> str:
    if row is None:
        return f"- {site.display}：未登录" + ("（待侦察，暂不支持检测）" if site.recon_pending else "")
    label = STATUS_LABEL.get(row.status, row.status)
    parts = [f"- {site.display}：{label}"]
    if row.nickname:
        parts.append(f"账号 {row.nickname}")
    display = ((row.probe_detail or {}).get("display") or {})
    if display.get("account_name"):
        parts.append(f"店铺 {display['account_name']}")
    if public and public.get("predictedExpiresAt") and row.status == "bound":
        parts.append(f"预计 {public['predictedExpiresAt'][:10]} 需重新登录")
    parts.append(f"上次检测 {_fmt(row.last_probe_at)}")
    if row.status != "bound" and row.last_error:
        parts.append(f"原因：{row.last_error}")
    return "，".join(parts)


async def _status(ctx: ToolContext, site: DesktopSite | None) -> ToolResult:
    from platforms import service as platform_service
    from session.browser_pref import get_browser_mode

    mode = await get_browser_mode(ctx.user_id) if ctx.user_id else "local"
    if mode == "remote":
        return ToolResult(
            title="桌面登录态不适用",
            output=(
                "当前浏览器模式是用户本机的 Chrome（extension），登录态在用户自己的电脑上，"
                "云电脑的登录记录不适用。直接在用户浏览器里操作即可；如果站点未登录，让用户自己登录。"
            ),
            metadata={"applicable": False, "mode": mode},
        )
    rows = {
        r.platform: r
        for r in await platform_service.list_accounts(ctx.workspace_id)
        if r.auth_kind == desktop_service.AUTH_KIND
    }
    desktop = await desktop_service.workspace_desktop(ctx.workspace_id)
    sites = [site] if site else list_sites()
    lines = [_describe(s, rows.get(s.key), platform_service.to_public(rows[s.key]) if s.key in rows else None) for s in sites]
    header = (
        f"云电脑 {desktop['desktop_id']} 上的登录态：" if desktop else "当前工作空间没有云电脑，登录态无从谈起："
    )
    summary = {s.key: (rows[s.key].status if s.key in rows else "none") for s in sites}
    if site:
        row = rows.get(site.key)
        if row is not None and row.status == "bound":
            return ToolResult(
                title=f"{site.display} 已登录",
                output=header + "\n" + "\n".join(lines) + "\n\n可以直接在云电脑浏览器里操作这个站点。",
                metadata={"applicable": True, "site": site.key, "status": "bound", "nickname": row.nickname, "sites": summary},
            )
        reason = "云电脑离线" if (row and row.status == "desktop_offline") else (STATUS_LABEL.get(row.status, row.status) if row else "未登录")
        return ToolResult(
            title=f"{site.display} 需要登录",
            output=(
                header + "\n" + "\n".join(lines)
                + f"\n\n{site.display} 当前{reason}。不要自己去打开登录页或截屏猜：先用 action=open 把登录页推到云电脑，"
                "请用户在云电脑里扫码登录，用户说完成后再用 action=probe 确认。"
            ),
            metadata={"error": True, "code": "DESKTOP_LOGIN_REQUIRED", "site": site.key, "status": summary[site.key], "sites": summary},
        )
    verdict = "需要登录的站点：" + "、".join(s.display for s in sites if summary[s.key] != "bound") if any(v != "bound" for v in summary.values()) else "所有站点都已登录。"
    return ToolResult(
        title="云电脑登录态",
        output=header + "\n" + "\n".join(lines) + "\n\n" + verdict,
        metadata={"applicable": True, "sites": summary},
    )


def _platform_error(exc: PlatformError, site: DesktopSite | None) -> ToolResult:
    hints = {
        "DESKTOP_UNAVAILABLE": "云电脑不在线或还没开通，现在无法操作登录态；让用户先在工作台打开云电脑。",
        "DESKTOP_BUSY": "云电脑正在被使用，稍等几秒再试。",
        "BROWSER_NOT_RUNNING": "云电脑上的浏览器没有运行；先用 computer(action=open_browser) 打开浏览器再试。",
    }
    return _error(exc.code, hints.get(exc.code, str(exc)), site=site.key if site else None, detail=str(exc))


async def _open(ctx: ToolContext, site: DesktopSite) -> ToolResult:
    if site.recon_pending:
        return _error("SITE_NOT_SUPPORTED", f"{site.display} 还没有接入登录态检测，只能请用户自行在云电脑上登录。", site=site.key)
    try:
        row = await desktop_service.open_login(ctx.workspace_id, ctx.user_id, site.key)
    except PlatformError as exc:
        return _platform_error(exc, site)
    return ToolResult(
        title=f"已在云电脑打开 {site.display} 登录页",
        output=(
            f"{site.display} 的登录页已推到云电脑前台。请告诉用户：在工作台右侧的云电脑面板里用手机 App 扫码登录，"
            "登录完成后回复一声。不要替用户输入验证码或密码。用户说完成后，用 action=probe 确认登录态。"
        ),
        metadata={"site": site.key, "account_id": row.id, "desktop_id": row.desktop_id, "status": row.status},
    )


async def _probe(ctx: ToolContext, site: DesktopSite) -> ToolResult:
    if site.recon_pending:
        return _error("SITE_NOT_SUPPORTED", f"{site.display} 还没有接入登录态检测。", site=site.key)
    try:
        rows = await desktop_service.probe_workspace(
            ctx.workspace_id, user_id=ctx.user_id, site_keys=[site.key], level=2, force_level2=True,
            lease=True, session_id=ctx.session_id or "desktop-login-tool",
        )
    except PlatformError as exc:
        return _platform_error(exc, site)
    row = next((r for r in rows if r.platform == site.key), None)
    if row is None or row.status != "bound":
        label = STATUS_LABEL.get(row.status, row.status) if row else "未登录"
        reason = f"（{row.last_error}）" if row and row.last_error else ""
        return ToolResult(
            title=f"{site.display} 仍未登录",
            output=f"检测结果：{site.display} {label}{reason}。请用户确认是否在云电脑里完成了扫码；需要时再 action=open 重新推登录页。",
            metadata={"error": True, "code": "DESKTOP_LOGIN_REQUIRED", "site": site.key, "status": row.status if row else "none"},
        )
    from platforms import service as platform_service

    public = platform_service.to_public(row)
    return ToolResult(
        title=f"{site.display} 已登录",
        output=_describe(site, row, public) + "\n\n登录态有效，可以在云电脑浏览器里操作这个站点了。",
        metadata={"site": site.key, "status": "bound", "nickname": row.nickname, "predictedExpiresAt": public.get("predictedExpiresAt")},
    )


async def execute_desktop_login(args: DesktopLoginArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.workspace_id:
        return _error("WORKSPACE_REQUIRED", "这个会话没有工作空间上下文，无法读取云电脑登录态。")
    site = _resolve_site(args.site)
    if args.site and site is None:
        return _error(
            "SITE_UNKNOWN",
            "不认识这个站点。可用：" + "、".join(f"{s.key}（{s.display}）" for s in list_sites()),
        )
    if args.action == "status":
        return await _status(ctx, site)
    if site is None:
        return _error("SITE_REQUIRED", f"{args.action} 需要指定 site。")
    if args.action == "open":
        return await _open(ctx, site)
    return await _probe(ctx, site)


DESKTOP_LOGIN_DESCRIPTION = """\
Login state of sites on the workspace's cloud desktop browser (抖音创作者中心, \
抖音来客, 美团经营宝/点评商户平台, 小红书创作平台). Before automating any site \
that needs the person's account in the cloud browser, call action=status with \
the site: if it is not bound you get DESKTOP_LOGIN_REQUIRED — then action=open \
pushes the login page to the cloud desktop for the person to scan, and \
action=probe confirms after they say they are done. Never open login pages or \
enter codes yourself; never guess login state from screenshots."""

desktop_login_tool = define_tool(
    "desktop_login",
    description=DESKTOP_LOGIN_DESCRIPTION,
    parameters=DesktopLoginArgs,
    execute=execute_desktop_login,
    sandbox_required=False,
    parallel_safe=False,
)
