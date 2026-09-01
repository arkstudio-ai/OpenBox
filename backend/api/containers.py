import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from auth.middleware import get_current_user, require_admin
from auth.preview_origin import origin_is_allowed, preview_origin_host, request_host
from sandbox import provider
from models.container import (
    ContainerListResponse,
    CreateContainerRequest,
    ImageStatusResponse,
    PublicContainerInfo,
    SuccessResponse,
)

router = APIRouter(prefix="/api/containers", tags=["containers"], dependencies=[Depends(get_current_user)])

# Preview proxy: separate router with preview token auth (no JWT — browser accesses via URL)
preview_router = APIRouter(prefix="/api/containers", tags=["preview"])
preview_config_router = APIRouter(
    prefix="/api/preview",
    tags=["preview"],
    dependencies=[Depends(get_current_user)],
)

PREVIEW_COOKIE_NAME = "openbox_preview_token"


@preview_config_router.get("/config")
async def get_preview_config(response: Response):
    """Publish the non-secret navigation contract from the control plane."""
    from core.config import get_config

    origin = get_config().preview_public_origin
    response.headers["Cache-Control"] = "no-store"
    return {
        "mode": "isolated_origin" if origin else "sandboxed_same_origin",
        "origin": origin or None,
    }


def _preview_cookie_path(container_id: str, port: int) -> str:
    """Scope a preview credential to one container and one exposed port."""
    return f"/api/containers/{container_id}/preview/{port}"


def _without_preview_cookie(raw_cookie: str) -> str:
    """Remove the proxy credential while preserving application cookies."""
    kept: list[str] = []
    for item in raw_cookie.split(";"):
        name, separator, _value = item.strip().partition("=")
        if separator and name == PREVIEW_COOKIE_NAME:
            continue
        if item.strip():
            kept.append(item.strip())
    return "; ".join(kept)


def _without_preview_query(url: str) -> str:
    """Remove preview credentials embedded in a URL such as Referer."""
    parsed = urlsplit(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "_pt"
        ]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


@router.get("/sandbox-image/status", response_model=ImageStatusResponse)
async def check_sandbox_image(current_user: dict = Depends(require_admin)):
    """Compatibility projection for clients that still show an image gate.

    WUYING is pre-provisioned and has no user-buildable sandbox image. Report
    the configured desktop as available instead of reaching into the removed
    Docker provider and raising a 500.
    """
    from core.config import get_config

    config = get_config()
    image = f"wuying:{config.wuying_desktop_id or 'desktop'}"
    return ImageStatusResponse(
        exists=bool(config.wuying_desktop_id and config.wuying_endpoint),
        image=image,
    )


@router.post("/sandbox-image/build")
async def build_sandbox_image(current_user: dict = Depends(require_admin)):
    raise HTTPException(
        status_code=409,
        detail=(
            "WUYING system components are provisioned by the controlled "
            "deployment workflow; runtime image builds are not supported"
        ),
    )


@router.post("", response_model=PublicContainerInfo, status_code=201)
async def create_container(req: CreateContainerRequest, current_user: dict = Depends(get_current_user)):
    from auth.quota import check_container_quota
    from core.config import get_config
    user_id = current_user["user_id"]
    config = get_config()

    existing_list = provider.get_containers_for_user(user_id)
    if existing_list:
        raise HTTPException(status_code=409, detail="Each user can only have one container")

    await check_container_quota(user_id, config)
    try:
        return await provider.create_container(req.name, req.image, req.project_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=ContainerListResponse)
async def list_containers(current_user: dict = Depends(get_current_user)):
    containers = provider.get_containers_for_user(current_user["user_id"])
    return ContainerListResponse(containers=containers, total=len(containers))


@router.get("/admin/all")
async def list_all_containers(current_user: dict = Depends(require_admin)):
    """List all containers across all users (admin only)."""
    from db.base import get_db_session
    from db.models.container import Container as ContainerORM
    from sqlalchemy import select
    try:
        async with get_db_session() as db:
            result = await db.execute(
                select(ContainerORM)
                .where(ContainerORM.is_deleted == False)  # noqa: E712
                .order_by(ContainerORM.created_at.desc())
            )
            rows = result.scalars().all()
            return [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "project_id": r.project_id,
                    "docker_id": r.docker_id,
                    "host": r.host,
                    "name": r.name,
                    "status": r.status,
                    "image": r.image,
                    "port": r.port,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query containers: {e}")


@router.get("/{container_id}", response_model=PublicContainerInfo)
async def get_container(container_id: str, current_user: dict = Depends(get_current_user)):
    try:
        return await provider.get_container(container_id, user_id=current_user["user_id"])
    except ValueError:
        raise HTTPException(status_code=404, detail="Container not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.delete("/{container_id}", response_model=SuccessResponse)
async def delete_container(container_id: str, current_user: dict = Depends(get_current_user)):
    try:
        await provider.delete_container(container_id, user_id=current_user["user_id"])
        return SuccessResponse(message="Container deleted")
    except ValueError:
        raise HTTPException(status_code=404, detail="Container not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/{container_id}/stop", response_model=SuccessResponse)
async def stop_container(container_id: str, current_user: dict = Depends(get_current_user)):
    try:
        await provider.stop_container(container_id, user_id=current_user["user_id"])
        return SuccessResponse(message="Container stopped")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/{container_id}/start", response_model=SuccessResponse)
async def start_container(container_id: str, current_user: dict = Depends(get_current_user)):
    try:
        await provider.start_container(container_id, user_id=current_user["user_id"])
        return SuccessResponse(message="Container started")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/{container_id}/ports")
async def get_listening_ports(container_id: str, current_user: dict = Depends(get_current_user)):
    """Detect TCP ports with services listening inside the container."""
    try:
        resp = await provider.forward_to_container(
            container_id, "GET", "/listening_ports", user_id=current_user["user_id"], timeout=5.0,
        )
        return resp.json()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to query ports: {e}")


@router.post("/{container_id}/preview-token")
async def create_preview_access_token(
    request: Request,
    container_id: str,
    port: int = Query(..., ge=1, le=65535),
    current_user: dict = Depends(get_current_user),
):
    """Set a short-lived preview cookie bound to user + container + port."""
    from core.config import get_config

    config = get_config()
    preview_origin = config.preview_public_origin
    if preview_origin:
        if request_host(request.scope.get("headers", ())) != preview_origin_host(preview_origin):
            raise HTTPException(status_code=404, detail="Not found")
        if not origin_is_allowed(request.headers.get("origin", ""), config.cors_origins):
            raise HTTPException(status_code=403, detail="Invalid preview issuer origin")

    user_id = current_user["user_id"]
    try:
        await provider.get_container(container_id, user_id=user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Container not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")

    from auth.preview_token import PREVIEW_TOKEN_TTL, create_preview_token

    token = await create_preview_token(user_id, container_id, port)
    preview_path = f"/api/containers/{container_id}/preview/{port}/"
    response = JSONResponse(
        {
            "url": f"{preview_origin}{preview_path}" if preview_origin else preview_path,
            "mode": "isolated_origin" if preview_origin else "sandboxed_same_origin",
        },
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        key=PREVIEW_COOKIE_NAME,
        value=token,
        max_age=PREVIEW_TOKEN_TTL,
        path=_preview_cookie_path(container_id, port),
        httponly=True,
        secure=bool(preview_origin) or request.url.scheme == "https",
        samesite="none" if preview_origin else "lax",
    )
    return response


# ── Preview Proxy (short-lived preview-token authentication) ──

@preview_router.api_route(
    "/{container_id}/preview/{port:int}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
@preview_router.api_route(
    "/{container_id}/preview/{port:int}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def preview_proxy(request: Request, container_id: str, port: int, path: str = ""):
    """Proxy requests to a user application running inside the container on the given port.

    The authenticated management request that creates the preview sets an
    HttpOnly cookie scoped to this exact container/port. Every request
    revalidates the opaque token so expiry/revocation takes effect immediately.
    """
    from auth.preview_token import (
        get_preview_token_claims,
    )

    token = request.cookies.get(PREVIEW_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Preview token required")

    claims = await get_preview_token_claims(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Invalid or expired preview token")
    if claims["container_id"] != container_id or claims["port"] != port:
        raise HTTPException(
            status_code=403,
            detail="Preview token is not valid for this container or port",
        )

    try:
        info = await provider.get_container(container_id, user_id=claims["user_id"])
    except ValueError:
        raise HTTPException(status_code=404, detail="Container not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")

    # A legacy or user-supplied `_pt` parameter is never a credential and must
    # not reach sandbox code. Preserve all other query parameters, including
    # duplicates.
    query_items = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "_pt"
    ]
    proxy_url = f"http://{info.host}:{info.port}/proxy/{port}/{path}"
    if query_items:
        proxy_url += f"?{urlencode(query_items)}"

    body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in (
            "host",
            "connection",
            "cookie",
            "authorization",
            "proxy-authorization",
            "referer",
            "x-api-key",
        )
    }
    application_cookie = _without_preview_cookie(request.headers.get("cookie", ""))
    if application_cookie:
        headers["cookie"] = application_cookie
    referer = _without_preview_query(request.headers.get("referer", ""))
    if referer:
        headers["referer"] = referer
    headers["X-API-Key"] = info.api_key or ""

    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=proxy_url,
                headers=headers,
                content=body,
            )
        # Sandbox applications do not get to mutate browser state owned by the
        # platform origin. Dedicated origins limit the blast radius, while the
        # same filtering also protects the safe fallback mode.
        excluded = {
            "transfer-encoding",
            "connection",
            "content-encoding",
            "content-length",
            "set-cookie",
            "clear-site-data",
            "service-worker-allowed",
            "cache-control",
            "expires",
        }
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        resp_headers["Cache-Control"] = "private, no-store"

        content = resp.content
        content_type = resp.headers.get("content-type", "")

        if "text/html" in content_type and resp.status_code == 200:
            base_href = f"/api/containers/{container_id}/preview/{port}/"
            base_tag = f'<base href="{base_href}">'
            html = content.decode("utf-8", errors="replace")

            html = re.sub(
                r'''((?:src|href|action)\s*=\s*["'])/(?!/|https?:)''',
                r'\1./',
                html,
                flags=re.IGNORECASE,
            )

            html = re.sub(
                r'''url\(\s*(["']?)/(?!/|https?:)''',
                r'url(\1./',
                html,
                flags=re.IGNORECASE,
            )

            ws_stub = (
                '<script>'
                '(function(){'
                'var _WS=window.WebSocket;'
                'window.WebSocket=function(u,p){'
                'try{var o=new URL(u,location.href);'
                'if(o.hostname===location.hostname){'
                'var f={readyState:3,CONNECTING:0,OPEN:1,CLOSING:2,CLOSED:3,'
                'send:function(){},close:function(){},'
                'addEventListener:function(){},removeEventListener:function(){}};'
                'return f;'
                '}}catch(e){}'
                'return p!==void 0?new _WS(u,p):new _WS(u);'
                '};'
                'window.WebSocket.CONNECTING=0;window.WebSocket.OPEN=1;'
                'window.WebSocket.CLOSING=2;window.WebSocket.CLOSED=3;'
                '})();'
                '</script>'
            )
            inject = ws_stub + base_tag
            if "<head>" in html:
                html = html.replace("<head>", f"<head>{inject}", 1)
            elif "<HEAD>" in html:
                html = html.replace("<HEAD>", f"<HEAD>{inject}", 1)
            elif "<html" in html.lower():
                html = re.sub(r"(<html[^>]*>)", rf"\1<head>{inject}</head>", html, count=1, flags=re.IGNORECASE)
            else:
                html = inject + html

            content = html.encode("utf-8")

        elif "text/css" in content_type and resp.status_code == 200:
            css = content.decode("utf-8", errors="replace")
            css = re.sub(
                r'''url\(\s*(["']?)/(?!/|https?:)''',
                r'url(\1./',
                css,
                flags=re.IGNORECASE,
            )
            content = css.encode("utf-8")

        elif ("javascript" in content_type or "text/javascript" in content_type) and resp.status_code == 200:
            base_href = f"/api/containers/{container_id}/preview/{port}"
            js = content.decode("utf-8", errors="replace")
            js = re.sub(
                r'''((?:from|import\()\s*)(["'])/(?!/|https?:)''',
                rf'\1\2{base_href}/',
                js,
                flags=re.IGNORECASE,
            )
            js = re.sub(
                r'''(\bimport\s+)(["'])/(?!/|https?:)''',
                rf'\1\2{base_href}/',
                js,
            )
            js = re.sub(
                r'''((?:fetch|new\s+URL)\s*\(\s*)(["'])/(?!/|https?:)''',
                rf'\1\2{base_href}/',
                js,
                flags=re.IGNORECASE,
            )
            content = js.encode("utf-8")

        return Response(
            content=content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=content_type or None,
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"No service on port {port} in container")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Timeout connecting to port {port}")
