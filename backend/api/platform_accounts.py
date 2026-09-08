"""授权中心 API: platform accounts bound to the selected workspace.

Members can read, probe and publish; binding, refreshing and unbinding are
owner/admin actions (they change who the workspace can act as). The OAuth
callback is the one unauthenticated route: the browser arrives there from the
platform, and the `state` we issued is the only thing that identifies it.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from audit import record
from auth.middleware import get_current_user
from auth.workspace import get_workspace, require_workspace_role
from core.log import create_logger
from core.oss import OssNotConfigured, get_oss
from db.base import get_db_session
from platforms import service
from platforms.errors import PlatformError
from platforms.registry import get_provider, list_providers

log = create_logger("api.platform_accounts")

router = APIRouter(
    prefix="/api/platform-accounts",
    tags=["platform-accounts"],
    dependencies=[Depends(get_workspace)],
)
public_router = APIRouter(prefix="/api/platform-accounts", tags=["platform-accounts"])
platforms_router = APIRouter(
    prefix="/api/platforms", tags=["platform-accounts"], dependencies=[Depends(get_workspace)]
)
jobs_router = APIRouter(
    prefix="/api/publish-jobs", tags=["platform-accounts"], dependencies=[Depends(get_workspace)]
)

_MANAGER = require_workspace_role("owner", "admin")
AUTH_CENTER_PATH = "/app/auth-center"


def _http_error(exc: PlatformError, status: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": str(exc)},
        headers={"X-Error-Code": exc.code},
    )


def _status_for(exc: PlatformError) -> int:
    if exc.code in ("PLATFORM_ACCOUNT_NOT_FOUND", "PUBLISH_JOB_NOT_FOUND", "PUBLISH_FILE_NOT_FOUND", "PLATFORM_UNKNOWN"):
        return 404
    if exc.code == "PLATFORM_NOT_CONFIGURED":
        return 503
    if exc.code == "PLATFORM_AUTH_REQUIRED":
        return 409
    if exc.code == "PLATFORM_API_ERROR":
        return 502
    if exc.code in ("DESKTOP_UNAVAILABLE", "BROWSER_NOT_RUNNING"):
        return 503
    if exc.code == "DESKTOP_BUSY":
        return 423
    return 400


class PublishBody(BaseModel):
    file_asset_id: str
    title: str = ""
    hashtags: list[str] = Field(default_factory=list)
    #: 0 everyone, 1 only me, 2 friends
    private_status: int = 0
    #: 1 allow download, 2 forbid
    download_type: int = 1


# ── Catalogue ──────────────────────────────────────────────────────────────
@platforms_router.get("")
async def list_platforms(kinds: str = Query("oauth")):
    """Platform catalogue. `kinds=oauth,desktop` adds the cloud-desktop sites.

    Desktop sites are opt-in so a frontend built before they existed keeps
    working: it only ever asked for OAuth platforms and reads `capabilities`
    without a guard. Every entry carries the OAuth-shaped keys regardless.
    """
    from platforms.desktop.sites import list_sites, public_site

    wanted = {k.strip() for k in kinds.split(",") if k.strip()} or {"oauth"}
    out = []
    if "oauth" in wanted:
        for provider in list_providers():
            info = provider.info()
            out.append(
                {
                    "key": info.key,
                    "display": info.display,
                    "kind": "oauth",
                    "capabilities": info.capabilities,
                    "configured": info.configured,
                    "maxGrantDays": info.max_grant_days,
                }
            )
    if "desktop" in wanted:
        for site in list_sites():
            out.append({"capabilities": [], "configured": True, "maxGrantDays": None, **public_site(site)})
    return out


# ── Accounts ───────────────────────────────────────────────────────────────
@router.get("")
async def list_accounts(current_user: dict = Depends(get_current_user)):
    rows = await service.list_accounts(current_user["workspace_id"])
    return [service.to_public(r) for r in rows]


@router.post("/{platform}/authorize")
async def start_authorize(
    platform: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    _role: dict = Depends(_MANAGER),
):
    try:
        get_provider(platform)
        result = await service.start_authorize(
            user_id=current_user["user_id"],
            workspace_id=current_user["workspace_id"],
            platform=platform,
        )
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    await record(
        current_user["user_id"], current_user["workspace_id"],
        "platform_account.authorize_start", "platform", platform, None, request,
    )
    return result


@public_router.get("/{platform}/callback")
async def oauth_callback(
    platform: str,
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    scopes: str = Query(""),
    error: str = Query(""),
    error_description: str = Query(""),
):
    """Where the platform sends the browser back. Never trusts the session."""
    base = service.public_base_url(f"{request.url.scheme}://{request.url.netloc}")
    target = f"{base}{AUTH_CENTER_PATH}"
    if error or not code or not state:
        log.info("platform callback rejected platform=%s error=%s", platform, error or "missing_code")
        return RedirectResponse(f"{target}?platform={platform}&error=PLATFORM_DENIED", status_code=302)
    try:
        row = await service.complete_callback(platform=platform, code=code, state=state, granted_scopes=scopes)
    except PlatformError as exc:
        log.warning("platform callback failed platform=%s code=%s", platform, exc.code)
        return RedirectResponse(f"{target}?platform={platform}&error={exc.code}", status_code=302)
    except Exception:
        log.exception("platform callback crashed platform=%s", platform)
        return RedirectResponse(f"{target}?platform={platform}&error=PLATFORM_ERROR", status_code=302)
    await record(
        row.bound_by_user_id, row.workspace_id,
        "platform_account.bind", "platform_account", row.id,
        {"platform": platform, "external_id": row.external_id}, request,
    )
    return RedirectResponse(f"{target}?platform={platform}&bound={row.id}", status_code=302)


# ── Desktop login state (云电脑登录态) ───────────────────────────────────────
# Declared before the /{account_id}/... routes: "desktop" must not be read as an id.
@router.post("/desktop/{site}/open")
async def open_desktop_login(
    site: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Push the site's login page to the front of the workspace's cloud desktop."""
    from platforms.desktop import service as desktop_service
    from platforms.desktop.sites import get_site

    try:
        get_site(site)
    except KeyError:
        raise HTTPException(404, detail={"code": "PLATFORM_UNKNOWN", "message": f"unknown site {site}"})
    try:
        row = await desktop_service.open_login(current_user["workspace_id"], current_user["user_id"], site)
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    await record(
        current_user["user_id"], current_user["workspace_id"],
        "platform_account.desktop_login_open", "platform_account", row.id, {"site": site}, request,
    )
    return service.to_public(row)


@router.post("/desktop/probe")
async def probe_desktop_logins(
    current_user: dict = Depends(get_current_user),
):
    """Probe every catalogued site on the workspace's desktop (cookie level)."""
    from platforms.desktop import service as desktop_service

    try:
        rows = await desktop_service.probe_workspace(
            current_user["workspace_id"], user_id=current_user["user_id"], level=1,
            session_id=f"auth-center:{current_user['user_id']}",
        )
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    return [service.to_public(r) for r in rows]


@router.post("/{account_id}/logout")
async def logout_desktop_login(
    account_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    _role: dict = Depends(_MANAGER),
):
    """Delete the site's cookies on the desktop (退出登录)."""
    from platforms.desktop import service as desktop_service

    try:
        async with get_db_session() as db:
            row = await service._owned(db, account_id, current_user["workspace_id"])
            if row.auth_kind != "desktop_cookie":
                raise PlatformError("only desktop logins can be logged out here", code="PLATFORM_KIND_MISMATCH")
        row = await desktop_service.logout(row)
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    await record(
        current_user["user_id"], current_user["workspace_id"],
        "platform_account.desktop_logout", "platform_account", row.id, {"site": row.platform}, request,
    )
    return service.to_public(row)


@router.post("/{account_id}/probe")
async def probe_account(account_id: str, current_user: dict = Depends(get_current_user)):
    try:
        row = await service.probe(account_id, current_user["workspace_id"])
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    return service.to_public(row)


@router.post("/{account_id}/refresh")
async def refresh_account(
    account_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    _role: dict = Depends(_MANAGER),
):
    try:
        row = await service.refresh_account(account_id, current_user["workspace_id"])
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    await record(
        current_user["user_id"], current_user["workspace_id"],
        "platform_account.refresh", "platform_account", row.id, {"status": row.status}, request,
    )
    return service.to_public(row)


@router.delete("/{account_id}")
async def unbind_account(
    account_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    _role: dict = Depends(_MANAGER),
):
    try:
        row = await service.unbind(account_id, current_user["workspace_id"])
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    await record(
        current_user["user_id"], current_user["workspace_id"],
        "platform_account.unbind", "platform_account", row.id, {"platform": row.platform}, request,
    )
    return {"ok": True, "id": row.id, "status": row.status}


# ── Publish ────────────────────────────────────────────────────────────────
@router.post("/douyin/publish")
async def publish_to_douyin(
    body: PublishBody,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    try:
        oss = get_oss()
    except OssNotConfigured as exc:
        raise HTTPException(503, detail=str(exc))
    if body.private_status not in (0, 1, 2) or body.download_type not in (1, 2):
        raise HTTPException(422, detail="invalid private_status or download_type")
    try:
        job, schema = await service.create_publish_job(
            oss=oss,
            user_id=current_user["user_id"],
            workspace_id=current_user["workspace_id"],
            file_asset_id=body.file_asset_id,
            title=body.title.strip(),
            hashtags=[h.strip() for h in body.hashtags if h and h.strip()],
            private_status=body.private_status,
            download_type=body.download_type,
        )
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    await record(
        current_user["user_id"], current_user["workspace_id"],
        "publish.create", "publish_job", job.id,
        {"platform": "douyin", "file_asset_id": job.file_asset_id, "share_id": job.share_id}, request,
    )
    return {
        "job": service.job_to_public(job),
        "schema": schema,
        "schemaSource": "local" if (job.error or "").startswith("schema_source=local") else "get_share",
    }


@jobs_router.get("")
async def list_publish_jobs(current_user: dict = Depends(get_current_user)):
    rows = await service.list_jobs(current_user["workspace_id"])
    return [service.job_to_public(r) for r in rows]


@jobs_router.get("/{job_id}")
async def get_publish_job(job_id: str, current_user: dict = Depends(get_current_user)):
    try:
        row = await service.get_job(job_id, current_user["workspace_id"])
    except PlatformError as exc:
        raise _http_error(exc, _status_for(exc))
    return service.job_to_public(row)
