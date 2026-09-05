"""Administrative ECD fleet inspection and alert lifecycle API."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from audit import record
from auth.middleware import require_admin
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.fleet import FleetAlert, FleetSnapshot, PoolPurchase


router = APIRouter(
    prefix="/api/admin/fleet",
    tags=["admin-fleet"],
    dependencies=[Depends(require_admin), Depends(get_workspace)],
)


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
    await record(
        admin["user_id"], admin.get("workspace_id"), "admin.fleet.view_desktops",
        "cloud_desktop", None, {"pool_state": pool_state, "q": q}, request,
    )
    return {"items": [_row(item) for item in rows], "total": total or 0}


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
        "gates": {
            "max_unit_price_cny": config.pool_max_unit_price_cny,
            "max_per_tick": config.pool_max_purchases_per_tick,
            "max_per_day": config.pool_max_purchases_per_day,
            "min_balance_multiple": config.pool_min_account_balance_multiple,
        },
    }


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
