"""Administrative ECD fleet inspection and alert lifecycle API."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from audit import record
from auth.middleware import require_admin
from auth.workspace import get_workspace
from core.log import create_logger
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.fleet import FleetAlert, FleetSnapshot, PoolPurchase
from db.models.user import User
from db.models.workspace import Workspace
from sandbox import wuying_ecd


router = APIRouter(
    prefix="/api/admin/fleet",
    tags=["admin-fleet"],
    dependencies=[Depends(require_admin), Depends(get_workspace)],
)
log = create_logger("api.admin_fleet")


def _row(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class MuteRequest(BaseModel):
    until: datetime


class ApproveRequest(BaseModel):
    approve: bool = False


class AdoptRequest(BaseModel):
    pool_state: str
    rebuild: bool = False
    approve: bool = False
    gateway_release_verified: bool = False


def _pool_http_error(exc: Exception) -> HTTPException:
    from sandbox.pool import DestructiveApprovalRequired, PaidOperationApprovalRequired

    status = 409 if isinstance(
        exc, (DestructiveApprovalRequired, PaidOperationApprovalRequired)
    ) else 422
    return HTTPException(status, detail=str(exc))


@router.get("/desktops")
async def list_desktops(
    request: Request,
    pool_state: str | None = None,
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(require_admin),
):
    stmt = select(CloudDesktop)
    if pool_state:
        stmt = stmt.where(CloudDesktop.pool_state == pool_state)
    needle = q.strip()
    if needle:
        stmt = stmt.where(or_(
            CloudDesktop.desktop_id.ilike(f"%{needle}%"),
            CloudDesktop.workspace_id.ilike(f"%{needle}%"),
        ))
    async with get_db_session() as session:
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            await session.execute(
                stmt.order_by(CloudDesktop.updated_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        workspace_ids = {item.workspace_id for item in rows if item.workspace_id}
        user_ids = {item.user_id for item in rows if item.user_id}
        workspace_usernames = dict((
            await session.execute(
                select(Workspace.id, User.username)
                .join(User, User.id == Workspace.owner_user_id)
                .where(Workspace.id.in_(workspace_ids))
            )
        ).all()) if workspace_ids else {}
        user_usernames = dict((
            await session.execute(
                select(User.id, User.username).where(User.id.in_(user_ids))
            )
        ).all()) if user_ids else {}
    desktop_ids = [item.desktop_id for item in rows if item.desktop_id]
    live_entitlements: dict[str, list[str]] | None = None
    ecd_usernames: dict[str, str | None] = {}
    try:
        live_entitlements = await wuying_ecd.describe_desktop_entitlements(desktop_ids)
    except Exception as exc:
        log.warning("Failed to read live ECD desktop entitlements: %s", exc)
    if live_entitlements is not None:
        end_user_ids = sorted({
            end_user_id
            for bindings in live_entitlements.values()
            for end_user_id in bindings
        })
        if end_user_ids:
            try:
                ecd_usernames = await wuying_ecd.describe_end_users(end_user_ids)
            except Exception as exc:
                log.warning("Failed to read ECD EndUser display names: %s", exc)
    items = []
    for item in rows:
        payload = _row(item)
        bindings = (
            live_entitlements.get(item.desktop_id)
            if live_entitlements is not None and item.desktop_id
            else None
        )
        payload["ecd_end_user_ids"] = bindings
        local_username = (
            workspace_usernames.get(item.workspace_id)
            or user_usernames.get(item.user_id)
        )
        expected_end_user_id = (
            wuying_ecd.eu_id_for(item.workspace_id) if item.workspace_id else None
        )
        payload["ecd_end_users"] = (
            [
                {
                    "id": end_user_id,
                    "username": (
                        local_username
                        if end_user_id in {item.end_user_id, expected_end_user_id}
                        else ecd_usernames.get(end_user_id)
                    ),
                }
                for end_user_id in bindings
            ]
            if bindings is not None
            else None
        )
        items.append(payload)
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.view_desktops",
        "cloud_desktop", None, {"pool_state": pool_state, "q": q}, request,
    )
    return {"items": items, "total": total or 0}


@router.get("/pool")
async def get_pool(request: Request, admin: dict = Depends(require_admin)):
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_db_session() as session:
        states = (
            await session.execute(
                select(CloudDesktop.pool_state, func.count())
                .where(CloudDesktop.is_deleted.is_(False))
                .group_by(CloudDesktop.pool_state)
            )
        ).all()
        purchased_today = await session.scalar(
            select(func.coalesce(func.sum(PoolPurchase.quantity), 0)).where(
                PoolPurchase.created_at >= start,
                PoolPurchase.status.in_(("ordered", "created")),
            )
        )
    from core.config import get_config
    config = get_config()
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.view_pool",
        "pool", "prewarm", None, request,
    )
    return {
        "states": {state: count for state, count in states},
        "target_prewarm": config.pool_target_prewarm,
        "purchased_today": purchased_today or 0,
        "enabled": config.pool_enabled,
        "auto_purchase": config.pool_auto_purchase,
        "auto_renew": config.pool_auto_renew,
        "gates": {
            "max_unit_price_cny": config.pool_max_unit_price_cny,
            "max_per_tick": config.pool_max_purchases_per_tick,
            "max_per_day": config.pool_max_purchases_per_day,
            "min_balance_multiple": config.pool_min_account_balance_multiple,
        },
    }


@router.post("/pool/ensure")
async def ensure_pool(
    request: Request,
    dry_run: bool = Query(True),
    admin: dict = Depends(require_admin),
):
    from sandbox.pool import PoolStateError, pool_service

    try:
        result = await pool_service.ensure_prewarm(
            dry_run=dry_run,
            actor=admin["user_id"],
        )
    except PoolStateError as exc:
        raise _pool_http_error(exc) from exc
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.ensure_pool",
        "pool", "prewarm", {"dry_run": dry_run, "result": result}, request,
    )
    return result


@router.get("/alerts")
async def list_alerts(
    request: Request,
    state: str = Query("open", pattern="^(open|resolved)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(require_admin),
):
    stmt = select(FleetAlert).where(
        FleetAlert.resolved_at.is_(None)
        if state == "open"
        else FleetAlert.resolved_at.is_not(None)
    )
    async with get_db_session() as session:
        rows = (
            await session.execute(
                stmt.order_by(FleetAlert.last_seen_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.view_alerts",
        "fleet_alert", None, {"state": state}, request,
    )
    return {"items": [_row(item) for item in rows]}


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: str, request: Request, admin: dict = Depends(require_admin)
):
    async with get_db_session() as session:
        alert = await session.get(FleetAlert, alert_id)
        if alert is None:
            raise HTTPException(404, detail="Fleet alert not found")
        alert.acked_by = admin["user_id"]
        alert.acked_at = datetime.now(timezone.utc)
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.alert_ack",
        "fleet_alert", alert_id, None, request,
    )
    return {"ok": True}


@router.post("/alerts/{alert_id}/mute")
async def mute_alert(
    alert_id: str,
    body: MuteRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    until = body.until.replace(tzinfo=body.until.tzinfo or timezone.utc).astimezone(timezone.utc)
    if until <= datetime.now(timezone.utc):
        raise HTTPException(422, detail="Mute deadline must be in the future")
    async with get_db_session() as session:
        alert = await session.get(FleetAlert, alert_id)
        if alert is None:
            raise HTTPException(404, detail="Fleet alert not found")
        alert.muted_until = until
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.alert_mute",
        "fleet_alert", alert_id, {"until": until.isoformat()}, request,
    )
    return {"ok": True, "muted_until": until}


@router.get("/snapshots/latest")
async def latest_snapshot(request: Request, admin: dict = Depends(require_admin)):
    async with get_db_session() as session:
        latest = await session.scalar(select(func.max(FleetSnapshot.taken_at)))
        rows = [] if latest is None else (
            await session.execute(
                select(FleetSnapshot)
                .where(FleetSnapshot.taken_at == latest)
                .order_by(FleetSnapshot.source)
            )
        ).scalars().all()
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.view_snapshot",
        "fleet_snapshot", None, None, request,
    )
    return {"taken_at": latest, "sources": [_row(item) for item in rows]}


# --- browser diagnostics ---------------------------------------------------

class DiagRequest(BaseModel):
    session: str = ""
    lines: int = 60
    #: `auto` uses the application channel when it is up and falls back to
    #: Cloud Assistant; `cloud` forces Cloud Assistant (root, no tunnel).
    via: str = "auto"


@router.get("/diag/recent")
async def recent_diags(
    limit: int = Query(50, ge=1, le=200),
    desktop_id: str = "",
    admin: dict = Depends(require_admin),
):
    """Browser snapshots, newest first, without their report bodies."""
    from sandbox import diag

    return {"items": await diag.list_recent(limit, desktop_id=desktop_id)}


@router.get("/diag/{diag_id}")
async def get_diag(diag_id: str, admin: dict = Depends(require_admin)):
    from sandbox import diag

    record = await diag.get(diag_id)
    if record is None:
        raise HTTPException(404, detail="diagnostic not found")
    return record


@router.get("/events")
async def list_events(
    desktop_id: str = "",
    session: str = "",
    kind: str = "",
    status: str = "",
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(require_admin),
):
    """The desktop timeline: browser bring-ups, launches, repairs, verifies, leases, diags."""
    from sandbox import events

    return {"items": await events.list_events(
        desktop_id=desktop_id, session_id=session, kind=kind, status=status, limit=limit,
    )}


@router.get("/events/{event_id}")
async def get_event(event_id: str, admin: dict = Depends(require_admin)):
    from sandbox import events

    event = await events.get(event_id)
    if event is None:
        raise HTTPException(404, detail="event not found")
    return event


@router.get("/desktops/{desktop_id}/events")
async def desktop_events(
    desktop_id: str,
    session: str = "",
    kind: str = "",
    status: str = "",
    limit: int = Query(100, ge=1, le=500),
    admin: dict = Depends(require_admin),
):
    from sandbox import events

    return {"items": await events.list_events(
        desktop_id=desktop_id, session_id=session, kind=kind, status=status, limit=limit,
    )}


@router.post("/desktops/{desktop_id}/diag")
async def collect_desktop_diag(
    desktop_id: str,
    body: DiagRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Take a fresh browser snapshot of one desktop, without logging into it."""
    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    from sandbox import diag
    from sandbox.channel import ChannelNotReady, route_for_record
    from sandbox.client import SandboxClient

    if body.via not in ("auto", "channel", "cloud"):
        raise HTTPException(422, detail="via must be auto, channel or cloud")
    desktop = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
    if desktop is None:
        raise HTTPException(404, detail="unknown desktop")

    report = None
    errors: list[str] = []
    if body.via in ("auto", "channel"):
        try:
            host, port, api_key = route_for_record(desktop)
            client = SandboxClient(host=host, port=port, api_key=api_key, desktop_id=desktop_id)
            report = await diag.collect_browser_diag(client, session=body.session, lines=body.lines)
        except (ChannelNotReady, Exception) as exc:
            errors.append(f"channel: {type(exc).__name__}: {str(exc)[:300]}")
            if body.via == "channel":
                raise HTTPException(502, detail=errors[-1]) from exc
    if report is None:
        try:
            report = await diag.collect_desktop_diag(desktop_id, session=body.session, lines=body.lines)
        except Exception as exc:
            errors.append(f"cloud_assistant: {type(exc).__name__}: {str(exc)[:300]}")
            raise HTTPException(502, detail="; ".join(errors)) from exc

    diag_id = await diag.remember(
        report, desktop_id=desktop_id, session_id=body.session,
        reason=f"admin:{admin['user_id']}", note="; ".join(errors),
    )
    await record(
        admin["user_id"], desktop.get("workspace_id"), "desktop.diag",
        target_type="cloud_desktop", target_id=desktop_id,
        detail={"via": report.get("via"), "diag_id": diag_id, "lights": (report.get("summary") or {}).get("lights")},
        request=request,
    )
    return {"id": diag_id, "fallback_errors": errors, **report}


@router.post("/desktops/{desktop_id}/release")
async def release_desktop(
    desktop_id: str, admin: dict = Depends(require_admin)
):
    from sandbox.pool import PoolStateError, pool_service

    try:
        result = await pool_service.release(desktop_id, admin["user_id"])
    except PoolStateError as exc:
        raise _pool_http_error(exc) from exc
    return result


@router.post("/desktops/{desktop_id}/recycle")
async def recycle_desktop(
    desktop_id: str,
    body: ApproveRequest,
    admin: dict = Depends(require_admin),
):
    from sandbox.pool import PoolStateError, pool_service

    try:
        result = await pool_service.recycle(
            desktop_id, admin["user_id"], approve=body.approve
        )
    except PoolStateError as exc:
        raise _pool_http_error(exc) from exc
    return result


@router.post("/desktops/{desktop_id}/retire")
async def retire_desktop(
    desktop_id: str, admin: dict = Depends(require_admin)
):
    from sandbox.pool import PoolStateError, pool_service

    try:
        result = await pool_service.retire(desktop_id, admin["user_id"])
    except PoolStateError as exc:
        raise _pool_http_error(exc) from exc
    return result


@router.post("/desktops/{desktop_id}/renew")
async def renew_desktop(
    desktop_id: str,
    body: ApproveRequest,
    admin: dict = Depends(require_admin),
):
    from sandbox.pool import PoolStateError, pool_service

    try:
        result = await pool_service.renew(
            desktop_id, admin["user_id"], approve=body.approve
        )
    except PoolStateError as exc:
        raise _pool_http_error(exc) from exc
    return result


@router.post("/desktops/{desktop_id}/adopt")
async def adopt_desktop(
    desktop_id: str,
    body: AdoptRequest,
    admin: dict = Depends(require_admin),
):
    from sandbox.pool import PoolStateError, pool_service

    try:
        result = await pool_service.adopt(
            desktop_id, body.pool_state, admin["user_id"],
            rebuild=body.rebuild, approve=body.approve,
            gateway_release_verified=body.gateway_release_verified,
        )
    except PoolStateError as exc:
        raise _pool_http_error(exc) from exc
    return result
