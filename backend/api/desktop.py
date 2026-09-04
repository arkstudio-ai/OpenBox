"""Cloud-desktop view: connection tickets for the Wuying Web SDK, plus
per-user desktop provisioning (wuying_mode="per_user").

The frontend's 云桌面 tab streams the Wuying desktop through Alibaba's Web SDK,
which needs a one-time connection ticket from ECD ``GetConnectionTicket``.
That call is an async task server-side: the first request may only return a
``taskId`` while Wuying logs the end user onto the desktop, and the ticket
appears once the task reaches FINISHED. We poll within a small budget and
return 202 (with the task id for the next attempt) when it isn't ready yet —
the frontend retries on 202, mirroring the reference integration in bossip.

In shared mode the desktop/end-user come from config (one desktop for
everyone). In per_user mode they resolve from the caller's own provisioned
desktop, with tag-based ownership verification, and a desktop that is still
being created or started also flows through the same 202 retry channel.

Credentials come from ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET, falling back to the
aliyun CLI profile (~/.aliyun/config.json) on dev machines.
"""
import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from auth.middleware import get_current_user
from auth.workspace import get_workspace, require_workspace_role
from core.aliyun import AliyunCredentialsError, load_credentials
from core.config import get_config
from core.log import create_logger

log = create_logger("api.desktop")

router = APIRouter(prefix="/api/desktop", tags=["desktop"])

_POLL_INTERVAL = 2.0
_POLL_BUDGET = 14.0  # keep well under typical proxy/request timeouts


def _ecd_client(region_id: str):
    from alibabacloud_ecd20200930.client import Client
    from alibabacloud_tea_openapi import models as open_api_models

    creds = load_credentials()
    config = open_api_models.Config(
        access_key_id=creds["access_key_id"],
        access_key_secret=creds["access_key_secret"],
        security_token=creds.get("security_token"),
        endpoint=f"ecd.{region_id}.aliyuncs.com",
        region_id=region_id,
    )
    return Client(config)


_warned_split = False


def _per_user() -> bool:
    """Whether to resolve the caller's own desktop for the cloud-desktop view.

    Requires two things, not one: the deployment asked for per-user desktops,
    *and* the sandbox provider actually routes per user. With only the first,
    the view would stream a desktop nobody works on — the agent would keep
    running on the single shared box, so a person watching the tab sees an idle
    machine while their command runs somewhere they cannot see. Falling back to
    shared keeps the one property that matters: what you watch is where it runs.
    """
    global _warned_split

    config = get_config()
    if not (config.sandbox_provider == "wuying" and config.wuying_mode == "per_user"):
        return False

    from sandbox import get_provider

    try:
        routes_per_user = get_provider().routes_per_user
    except Exception as e:  # provider not constructible — assume the safe answer
        log.warning(f"Cannot read sandbox provider routing, treating as shared: {e}")
        routes_per_user = False
    if routes_per_user:
        return True

    if not _warned_split:
        _warned_split = True
        log.error(
            "WUYING_MODE=per_user but the sandbox provider serves one shared "
            f"desktop ({config.wuying_desktop_id}); the cloud-desktop view is "
            "falling back to that desktop so it shows where the agent actually "
            "runs. Per-user desktops stay unused until the provider routes per "
            "user."
        )
    return False


def _pending(payload: dict) -> JSONResponse:
    return JSONResponse(payload, status_code=202, headers={"Retry-After": "3"})


@router.get("/status")
async def desktop_status(
    user=Depends(get_current_user), _workspace=Depends(get_workspace)
):
    """Provisioning state of the caller's desktop (per_user mode).

    Shared mode reports "running" whenever the ticket API is usable, so the
    frontend needs no mode switch.
    """
    config = get_config()
    if config.sandbox_provider != "wuying":
        return JSONResponse({"available": False, "reason": "provider"}, status_code=503)
    if not _per_user():
        state = "running" if (config.wuying_desktop_id and config.wuying_end_user_id) else "not_provisioned"
        return {"state": state, "mode": "shared"}

    from sandbox.wuying_desktop_service import wuying_desktop_service

    from sandbox.ownership import owner_for_request

    state = await wuying_desktop_service.status(await owner_for_request(user))
    return {**state, "mode": "per_user"}


@router.post("/provision")
async def desktop_provision(
    user=Depends(get_current_user),
    _workspace=Depends(require_workspace_role("owner", "admin")),
):
    """Create (or wake) the caller's own desktop. Idempotent; poll /status."""
    if not _per_user():
        return JSONResponse({"available": False, "reason": "mode"}, status_code=503)

    from sandbox.wuying_desktop_service import wuying_desktop_service

    from sandbox.ownership import owner_for_request

    state = await wuying_desktop_service.provision(
        await owner_for_request(user), triggered_by_user_id=user["user_id"]
    )
    return {**state, "mode": "per_user"}


@router.get("/ticket")
async def desktop_ticket(
    task_id: str | None = None,
    user=Depends(get_current_user),
    _workspace=Depends(get_workspace),
):
    """One-time Wuying connection ticket for the caller's desktop.

    202 + {taskId} while the desktop session is still being prepared; the
    client retries with that task id. In per_user mode a desktop that is still
    creating/starting rides the same 202 channel (with its state instead of a
    task id). The response carries the desktop and region only because the Web
    SDK's connect payload needs them — the UI never renders them.
    """
    config = get_config()
    if config.sandbox_provider != "wuying":
        return JSONResponse({"available": False, "reason": "provider"}, status_code=503)

    region = config.wuying_region_id

    if _per_user():
        from sandbox.wuying_desktop_service import (
            DesktopNotReady,
            wuying_desktop_service,
        )
        from sandbox.wuying_ecd import DesktopOwnershipError

        try:
            from sandbox.ownership import owner_for_request

            workspace_id = await owner_for_request(user)
            desktop_id, end_user_id = await wuying_desktop_service.resolve_ticket_target(
                workspace_id
            )
        except DesktopNotReady as e:
            state = e.payload.get("state")
            if state in ("creating", "starting"):
                return _pending({"pending": True, "state": state})
            return JSONResponse(
                {
                    "available": False,
                    "reason": state or "not_ready",
                    "code": "DESKTOP_NOT_READY",
                    "channel": e.payload.get("channel"),
                },
                status_code=503,
            )
        except DesktopOwnershipError as e:
            log.warning(f"Desktop ownership check failed: {e}")
            return JSONResponse({"available": False, "reason": "ownership"}, status_code=403)
        except Exception as e:
            log.warning(f"Desktop resolution failed: {e}")
            return JSONResponse({"available": False, "reason": "api_error"}, status_code=502)
    else:
        if not config.wuying_desktop_id:
            return JSONResponse({"available": False, "reason": "provider"}, status_code=503)
        if not config.wuying_end_user_id:
            return JSONResponse({"available": False, "reason": "no_end_user"}, status_code=503)
        desktop_id, end_user_id = config.wuying_desktop_id, config.wuying_end_user_id

    from alibabacloud_ecd20200930 import models as ecd_models

    try:
        client = _ecd_client(region)
    except AliyunCredentialsError as e:
        log.warning(f"Desktop ticket unavailable: {e}")
        return JSONResponse({"available": False, "reason": "credentials"}, status_code=503)

    deadline = asyncio.get_event_loop().time() + _POLL_BUDGET
    current_task = (task_id or "").strip() or None
    while True:
        try:
            resp = await client.get_connection_ticket_async(
                ecd_models.GetConnectionTicketRequest(
                    desktop_id=desktop_id,
                    end_user_id=end_user_id,
                    region_id=region,
                    task_id=current_task,
                )
            )
        except Exception as e:
            # Transient network wobble inside the budget keeps polling; a
            # hard API error is the caller's 502.
            message = str(e)
            if any(x in message for x in ("ConnectTimeout", "timed out", "ECONNRESET", "Connection reset")):
                if asyncio.get_event_loop().time() + _POLL_INTERVAL > deadline:
                    return _pending({"pending": True, "taskId": current_task})
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            # Ghost desktop: the ticket backend says NotFound while
            # DescribeDesktops still lists it. Deleting is the only recovery;
            # the next provision() builds a clean one.
            if _per_user() and any(x in message for x in ("NotFound", "InvalidDesktopId")):
                from sandbox.wuying_desktop_service import wuying_desktop_service

                log.warning(f"Ticket NotFound for {desktop_id}; releasing ghost desktop")
                try:
                    from sandbox.ownership import owner_for_request

                    await wuying_desktop_service.release_ghost(
                        await owner_for_request(user), actor_user_id=user["user_id"]
                    )
                except Exception as release_error:
                    log.warning(f"Ghost release failed: {release_error}")
                return JSONResponse({"available": False, "reason": "ghost_released"}, status_code=503)
            log.warning(f"GetConnectionTicket failed: {message}")
            return JSONResponse({"available": False, "reason": "api_error"}, status_code=502)

        body = resp.body
        ticket = (body.ticket or "").strip() if body else ""
        current_task = (body.task_id or "").strip() or current_task if body else current_task
        status = (body.task_status or "").strip() if body else ""

        if ticket:
            return {
                "ticket": ticket,
                "desktopId": desktop_id,
                "regionId": region,
            }
        if status == "FAILED":
            log.warning(f"GetConnectionTicket task failed: {body.task_message if body else ''}")
            return JSONResponse({"available": False, "reason": "task_failed"}, status_code=502)
        if asyncio.get_event_loop().time() + _POLL_INTERVAL > deadline:
            return _pending({"pending": True, "taskId": current_task})
        await asyncio.sleep(_POLL_INTERVAL)
