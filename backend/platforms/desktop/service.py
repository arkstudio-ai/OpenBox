"""Desktop login-state use cases: probe, open a login page, log out.

Rows live in `platform_accounts` with `auth_kind='desktop_cookie'`; one per
(workspace, desktop, site). Nothing here stores a cookie value.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.platform_account import PlatformAccount
from platforms.desktop import cdp
from platforms.desktop.sites import DesktopSite, get_site, list_sites, site_payload
from platforms.errors import PlatformError

log = create_logger("platforms.desktop")

AUTH_KIND = "desktop_cookie"
#: Level-2 (same-origin fetch) at most this often per site.
LEVEL2_MIN_INTERVAL = timedelta(hours=20)
#: Notify again about the same expiry no more than once a day.
NOTIFY_MIN_INTERVAL = timedelta(hours=23)
COMMAND_TIMEOUT = 45


class DesktopUnavailable(PlatformError):
    code = "DESKTOP_UNAVAILABLE"


class DesktopBusy(PlatformError):
    code = "DESKTOP_BUSY"


class BrowserNotRunning(PlatformError):
    code = "BROWSER_NOT_RUNNING"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# ── Desktop access ─────────────────────────────────────────────────────────
async def workspace_desktop(workspace_id: str) -> dict | None:
    """The workspace's live desktop record, or None when it has none."""
    from db.repository.cloud_desktop_repo import cloud_desktop_repo

    record = await cloud_desktop_repo.get_for_workspace(workspace_id)
    if not record or not record.get("desktop_id"):
        return None
    return record


def _client_for(record: dict):
    from sandbox.channel import ChannelNotReady, route_for_record
    from sandbox.client import SandboxClient

    try:
        host, port, api_key = route_for_record(record)
    except ChannelNotReady as exc:
        raise DesktopUnavailable(str(exc)) from exc
    return SandboxClient(host=host, port=port, api_key=api_key, desktop_id=record["desktop_id"])


async def run_on_desktop(
    record: dict,
    payload: dict,
    *,
    lease: bool,
    session_id: str = "auth-center",
    tool_call_id: str = "",
) -> dict:
    """Execute one cdp action on the desktop and return its parsed JSON."""
    return await run_command_on_desktop(
        record,
        cdp.build_command(payload),
        parse=cdp.parse_output,
        summary=f"{payload['action']} {','.join(s['key'] for s in payload['sites'])}",
        operation="login-state",
        lease=lease,
        session_id=session_id,
        tool_call_id=tool_call_id or f"login-{payload['action']}-{secrets.token_hex(4)}",
    )


async def run_command_on_desktop(
    record: dict,
    command: str,
    *,
    parse,
    summary: str,
    operation: str,
    lease: bool,
    timeout: int = COMMAND_TIMEOUT,
    lease_ttl: float = 60.0,
    span_kind: str = "platform.probe",
    allow_script_error: bool = False,
    session_id: str = "auth-center",
    tool_call_id: str = "",
) -> dict:
    """Run one self-contained desktop script and return `parse(stdout)`.

    Shared by the login-state probe and hot-list collection: one lease and
    timeline protocol, one mapping of transport failures to platform errors.
    Publishers opt into structured script errors so their caller can classify
    risk, login expiry and uncertain posting results instead of retrying them.
    Process/transport failures still raise regardless of this option.
    """
    from sandbox.events import span

    client = _client_for(record)
    tool_call_id = tool_call_id or f"{operation}-{secrets.token_hex(4)}"

    async def _execute():
        result = await client.execute(command, timeout=timeout)
        if result.exit_code != 0:
            text = (result.stderr or result.stdout or "").strip().splitlines()
            raise BrowserNotRunning((text[-1] if text else f"exit {result.exit_code}")[:200])
        return parse(result.stdout)

    async with span(
        span_kind, client=client, desktop_id=record["desktop_id"],
        session_id=session_id, tool_call_id=tool_call_id,
    ) as event:
        event.summary = summary[:200]
        try:
            if lease:
                async with client.desktop_lease(
                    session_id=session_id, tool_call_id=tool_call_id,
                    operation=operation, wait_timeout=5.0, ttl_seconds=lease_ttl,
                ):
                    data = await _execute()
            else:
                data = await _execute()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 423:
                event.status = "skip"
                event.summary = "desktop busy"
                raise DesktopBusy("desktop is in use, probe skipped") from exc
            raise DesktopUnavailable(f"HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise DesktopUnavailable(f"{type(exc).__name__}: {exc}"[:200]) from exc
        if data.get("error"):
            event.status = "fail"
            event.summary = str(data["error"])[:200]
            if not allow_script_error:
                raise BrowserNotRunning(str(data["error"])[:200])
        event.detail = {"chrome": data.get("chrome"), "targets": data.get("targets")}
        return data


# ── Rows ───────────────────────────────────────────────────────────────────
async def _rows_for(db, workspace_id: str) -> dict[str, PlatformAccount]:
    rows = (
        await db.execute(
            select(PlatformAccount).where(
                PlatformAccount.workspace_id == workspace_id,
                PlatformAccount.auth_kind == AUTH_KIND,
                PlatformAccount.deleted_at.is_(None),
            )
        )
    ).scalars()
    return {r.platform: r for r in rows}


def _new_row(workspace_id: str, user_id: str, desktop_id: str, site: DesktopSite, now: datetime) -> PlatformAccount:
    return PlatformAccount(
        id=ascending("pacc"),
        workspace_id=workspace_id,
        bound_by_user_id=user_id,
        platform=site.key,
        auth_kind=AUTH_KIND,
        external_id=f"desk:{desktop_id}",
        desktop_id=desktop_id,
        scopes="",
        status="unknown",
        renew_count=0,
        bound_at=now,
        created_at=now,
        updated_at=now,
    )


async def _retire_rows_for_old_desktop(db, rows: dict[str, PlatformAccount], desktop_id: str, now: datetime) -> bool:
    """The workspace got a new desktop: every old row's session is gone."""
    from platforms.service import add_notification

    changed = False
    for row in rows.values():
        if row.desktop_id and row.desktop_id != desktop_id:
            row.status = "unknown"
            row.last_error = f"desktop changed from {row.desktop_id} to {desktop_id}"
            row.desktop_id = desktop_id
            row.external_id = f"desk:{desktop_id}"
            row.nickname = None
            row.updated_at = now
            changed = True
    if changed:
        await add_notification(
            db, workspace_id=next(iter(rows.values())).workspace_id, user_id=None,
            kind="desktop_login_reset", title="云电脑已更换，需要重新登录",
            body="工作空间的云电脑已重建，之前登录过的站点需要在授权中心重新登录。",
        )
    return changed


def _apply_verdict(row: PlatformAccount, verdict: cdp.SiteVerdict, now: datetime, *, probed_level2: bool) -> str:
    """Write one verdict into a row; returns the transition ('' if none)."""
    previous = row.status
    row.last_probe_at = now
    detail = dict(row.probe_detail or {})
    detail.update({
        "cookie_ok": verdict.cookie_ok,
        "earliest_expiry": verdict.earliest_expiry,
        "reason": verdict.reason,
        **verdict.detail,
    })
    if verdict.display:
        detail["display"] = verdict.display
    if probed_level2:
        detail["last_level2_at"] = now.isoformat()
    row.probe_detail = detail
    if verdict.nickname:
        row.nickname = verdict.nickname[:255]
    if verdict.uid:
        row.external_id = verdict.uid[:128]
        row.union_id = None
    if verdict.status == "bound":
        row.status = "bound"
        row.last_ok_at = now
        row.last_error = None
        if previous != "bound":
            row.bound_at = now
    elif verdict.status == "expired":
        row.status = "expired"
        row.last_error = verdict.reason
    else:
        # unknown: keep the previous verdict, note why this one was inconclusive
        row.last_error = verdict.reason or row.last_error
    row.updated_at = now
    return f"{previous}->{row.status}" if previous != row.status else ""


def _level2_due(row: PlatformAccount | None, site: DesktopSite, now: datetime, force: bool) -> bool:
    if site.recon_pending or site.session_probe is None:
        return False
    if force:
        return True
    last = (row.probe_detail or {}).get("last_level2_at") if row else None
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= LEVEL2_MIN_INTERVAL
    except ValueError:
        return True


async def _notify_expired(db, row: PlatformAccount, site: DesktopSite, now: datetime) -> None:
    from platforms.service import add_notification

    last = (row.probe_detail or {}).get("last_notified_at")
    if last:
        try:
            if now - datetime.fromisoformat(last) < NOTIFY_MIN_INTERVAL:
                return
        except ValueError:
            pass
    await add_notification(
        db, workspace_id=row.workspace_id, user_id=None, kind="desktop_login_expired",
        title=f"{site.display} 登录已失效",
        body=f"云电脑上 {site.display} 的登录状态已失效（{row.last_error or '未知原因'}），请到授权中心重新登录。",
    )
    detail = dict(row.probe_detail or {})
    detail["last_notified_at"] = now.isoformat()
    row.probe_detail = detail


# ── Use cases ──────────────────────────────────────────────────────────────
async def probe_workspace(
    workspace_id: str,
    *,
    user_id: str = "",
    site_keys: list[str] | None = None,
    level: int = 1,
    force_level2: bool = False,
    lease: bool = True,
    session_id: str = "auth-center",
) -> list[PlatformAccount]:
    """Probe every catalogued site (or the given ones) on the workspace's desktop.

    Rows are created for sites that turn out to be logged in even if nobody
    registered them — a login done by hand on the desktop is a login.
    """
    now = _now()
    sites = [get_site(k) for k in site_keys] if site_keys else list_sites()
    record = await workspace_desktop(workspace_id)
    async with get_db_session() as db:
        rows = await _rows_for(db, workspace_id)
        if record is None:
            for row in rows.values():
                if row.status != "desktop_offline":
                    row.status = "desktop_offline"
                    row.last_error = "workspace has no cloud desktop"
                    row.updated_at = now
            await db.commit()
            return [rows[k] for k in sorted(rows)]
        desktop_id = record["desktop_id"]
        await _retire_rows_for_old_desktop(db, rows, desktop_id, now)

        level2_sites = {
            s.key for s in sites if level >= 2 and _level2_due(rows.get(s.key), s, now, force_level2)
        }
        payload = cdp.build_payload(
            "probe", [site_payload(s) for s in sites],
            level=2 if level2_sites else 1, profile=bool(level2_sites),
        )
        # Sites not due for level 2 are probed at level 1 only: strip their probes.
        for entry in payload["sites"]:
            if entry["key"] not in level2_sites:
                entry["session_probe"] = None
                entry["profile_probe"] = None
        try:
            data = await run_on_desktop(record, payload, lease=lease, session_id=session_id)
        except DesktopBusy:
            raise
        except PlatformError as exc:
            for row in rows.values():
                row.status = "desktop_offline"
                row.last_error = str(exc)[:200]
                row.last_probe_at = now
                row.updated_at = now
            await db.commit()
            raise

        out: list[PlatformAccount] = []
        for site in sites:
            rec = data.get("sites", {}).get(site.key)
            if rec is None:
                continue
            verdict = cdp.judge_site(site, rec)
            row = rows.get(site.key)
            if row is None:
                if verdict.status != "bound":
                    continue  # nothing to register: not logged in there
                row = _new_row(workspace_id, user_id or record.get("user_id") or "", desktop_id, site, now)
                db.add(row)
                rows[site.key] = row
            elif row.desktop_id != desktop_id:
                row.desktop_id = desktop_id
            transition = _apply_verdict(row, verdict, now, probed_level2=site.key in level2_sites)
            if row.status == "expired" and (transition or verdict.reason):
                await _notify_expired(db, row, site, now)
                from notifications.events import auth_blocked
                await auth_blocked(db, row, session_id=session_id, user_id=user_id)
            out.append(row)
        await db.commit()
        for row in out:
            await db.refresh(row)
        return out


async def probe_account(row: PlatformAccount, *, force_level2: bool = True) -> PlatformAccount:
    """The 授权中心 "检测" button: one site, level 2, right now."""
    rows = await probe_workspace(
        row.workspace_id, user_id=row.bound_by_user_id, site_keys=[row.platform],
        level=2, force_level2=force_level2, lease=True,
    )
    for updated in rows:
        if updated.id == row.id:
            return updated
    async with get_db_session() as db:
        fresh = (await db.execute(select(PlatformAccount).where(PlatformAccount.id == row.id))).scalar_one()
        return fresh


async def open_login(workspace_id: str, user_id: str, site_key: str) -> PlatformAccount:
    """Push the site's login page to the desktop's foreground; register the row."""
    site = get_site(site_key)
    now = _now()
    record = await workspace_desktop(workspace_id)
    if record is None:
        raise DesktopUnavailable("workspace has no cloud desktop")
    payload = cdp.build_payload("open", [site_payload(site)])
    await run_on_desktop(record, payload, lease=True, session_id=f"auth-center:{user_id}")
    async with get_db_session() as db:
        rows = await _rows_for(db, workspace_id)
        row = rows.get(site.key)
        if row is None:
            row = _new_row(workspace_id, user_id, record["desktop_id"], site, now)
            db.add(row)
        row.desktop_id = record["desktop_id"]
        row.last_error = None
        detail = dict(row.probe_detail or {})
        detail["login_opened_at"] = now.isoformat()
        row.probe_detail = detail
        row.updated_at = now
        await db.commit()
        await db.refresh(row)
        return row


async def logout(row: PlatformAccount) -> PlatformAccount:
    """Delete the site's cookies on the desktop; the row becomes revoked."""
    site = get_site(row.platform)
    now = _now()
    record = await workspace_desktop(row.workspace_id)
    if record is None:
        raise DesktopUnavailable("workspace has no cloud desktop")
    data = await run_on_desktop(record, cdp.build_payload("logout", [site_payload(site)]), lease=True)
    deleted = (data.get("sites", {}).get(site.key) or {}).get("deleted", 0)
    async with get_db_session() as db:
        fresh = (await db.execute(select(PlatformAccount).where(PlatformAccount.id == row.id))).scalar_one()
        fresh.status = "revoked"
        fresh.last_error = None
        detail = dict(fresh.probe_detail or {})
        detail.update({"logout_at": now.isoformat(), "cookies_deleted": deleted})
        fresh.probe_detail = detail
        fresh.updated_at = now
        await db.commit()
        await db.refresh(fresh)
        return fresh


def predicted_expiry(row: PlatformAccount) -> datetime | None:
    """min(earliest session cookie expiry, last activity + inactivity ttl)."""
    if row.auth_kind != AUTH_KIND or row.status != "bound":
        return None
    try:
        site = get_site(row.platform)
    except KeyError:
        return None
    candidates: list[datetime] = []
    earliest = (row.probe_detail or {}).get("earliest_expiry")
    if earliest:
        candidates.append(datetime.fromtimestamp(float(earliest), tz=timezone.utc))
    base = _aware(row.last_ok_at) or _aware(row.bound_at)
    if base:
        candidates.append(base + timedelta(days=site.inactivity_ttl_days))
    return min(candidates) if candidates else None


def public_extras(row: PlatformAccount) -> dict:
    """Fields the 授权中心 shows for a desktop_cookie row (all redacted)."""
    if row.auth_kind != AUTH_KIND:
        return {}
    detail = dict(row.probe_detail or {})
    predicted = predicted_expiry(row)
    try:
        site = get_site(row.platform)
        display = site.display
    except KeyError:
        display = row.platform
    return {
        "desktopId": row.desktop_id,
        "siteDisplay": display,
        "probeDetail": {
            "cookieOk": detail.get("cookie_ok"),
            "sessionCookies": detail.get("session_cookies"),
            "earliestExpiry": detail.get("earliest_expiry"),
            "reason": detail.get("reason"),
            "via": detail.get("via"),
            "probe": detail.get("probe"),
            "display": detail.get("display"),
            "lastLevel2At": detail.get("last_level2_at"),
        },
        "predictedExpiresAt": predicted.isoformat() if predicted else None,
    }
