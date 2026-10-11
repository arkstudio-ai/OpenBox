"""Operator view of partner (API-key) workspaces: tariff, monthly ladder usage, rebate settlement."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select

from audit import record
from auth.middleware import require_admin
from billing import partner
from db.base import get_db_session
from db.models.api_key import ApiKey
from db.models.partner import PartnerRebate, PartnerTariff
from db.models.workspace import Workspace

router = APIRouter(prefix="/api/admin/partners", tags=["admin-partners"], dependencies=[Depends(require_admin)])


class TariffWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tariff: dict
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    expected_revision: int | None = Field(default=None, ge=0)


class RebateSettle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


def _tariff_view(workspace_id: str, row: PartnerTariff | None) -> dict:
    if row is None:
        return {"workspace_id": workspace_id, "source": "default", "revision": 0, "tariff": partner.default_tariff()}
    return {"workspace_id": workspace_id, "source": "workspace", "revision": row.revision, "tariff": row.tariff,
            "reason": row.reason, "actor_user_id": row.actor_user_id, "updated_at": row.updated_at.isoformat()}


@router.get("")
async def list_partners():
    """Workspaces that own API keys, with key counts and whether a tariff override exists."""
    async with get_db_session() as db:
        rows = await db.execute(
            select(ApiKey.workspace_id, func.count(ApiKey.id), func.max(ApiKey.last_used_at))
            .where(ApiKey.revoked_at.is_(None)).group_by(ApiKey.workspace_id))
        items = []
        for workspace_id, keys, last_used in rows:
            workspace = await db.get(Workspace, workspace_id)
            tariff = await db.get(PartnerTariff, workspace_id)
            items.append({"workspace_id": workspace_id, "name": workspace.name if workspace else "",
                          "api_keys": int(keys), "last_used_at": last_used.isoformat() if last_used else None,
                          "tariff_revision": tariff.revision if tariff else 0})
    return {"items": items}


@router.get("/{workspace_id}/tariff")
async def get_tariff(workspace_id: str):
    async with get_db_session() as db:
        if await db.get(Workspace, workspace_id) is None:
            raise HTTPException(404, detail={"code": "WORKSPACE_NOT_FOUND"})
        row = await db.get(PartnerTariff, workspace_id)
    return _tariff_view(workspace_id, row)


@router.put("/{workspace_id}/tariff")
async def put_tariff(workspace_id: str, body: TariffWrite, request: Request, admin: dict = Depends(require_admin)):
    try:
        tariff = partner.validate_tariff(body.tariff)
    except partner.TariffError as exc:
        raise HTTPException(422, detail={"code": "TARIFF_INVALID", "message": str(exc)}) from exc
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_db_session() as db:
        if await db.get(Workspace, workspace_id) is None:
            raise HTTPException(404, detail={"code": "WORKSPACE_NOT_FOUND"})
        row = await db.get(PartnerTariff, workspace_id)
        current = row.revision if row else 0
        if body.expected_revision is not None and body.expected_revision != current:
            raise HTTPException(409, detail={"code": "REVISION_CONFLICT", "current": current})
        before = row.tariff if row else None
        if row is None:
            row = PartnerTariff(workspace_id=workspace_id, tariff=tariff, revision=1, reason=body.reason,
                                actor_user_id=admin["user_id"], updated_at=now)
            db.add(row)
        else:
            row.tariff, row.revision, row.reason, row.actor_user_id, row.updated_at = tariff, current + 1, body.reason, admin["user_id"], now
        revision = row.revision
    partner.clear_caches()
    await record(admin["user_id"], workspace_id, "admin.partner_tariff.update", "partner_tariff", workspace_id,
                 {"revision": revision, "reason": body.reason, "before_version": (before or {}).get("version"),
                  "after_version": tariff.get("version"), "digest": hashlib.sha256(str(tariff).encode()).hexdigest()[:16]}, request)
    async with get_db_session() as db:
        row = await db.get(PartnerTariff, workspace_id)
    return _tariff_view(workspace_id, row)


@router.delete("/{workspace_id}/tariff")
async def reset_tariff(workspace_id: str, request: Request, admin: dict = Depends(require_admin)):
    async with get_db_session() as db:
        row = await db.get(PartnerTariff, workspace_id)
        if row is not None:
            await db.delete(row)
    partner.clear_caches()
    await record(admin["user_id"], workspace_id, "admin.partner_tariff.reset", "partner_tariff", workspace_id, {}, request)
    return _tariff_view(workspace_id, None)


@router.get("/{workspace_id}/usage")
async def partner_usage(workspace_id: str, month: str | None = None):
    month = month or partner.current_month()
    try:
        summary = await partner.month_summary(workspace_id, month)
    except partner.TariffError as exc:
        raise HTTPException(400, detail={"code": "INVALID_MONTH", "message": str(exc)}) from exc
    async with get_db_session() as db:
        settled = await db.scalar(select(PartnerRebate).where(PartnerRebate.workspace_id == workspace_id, PartnerRebate.month == month))
    return {**partner._jsonable(summary), "settlement": partner.rebate_view(settled) if settled else None}


@router.post("/{workspace_id}/rebates/{month}")
async def settle_rebate(workspace_id: str, month: str, body: RebateSettle, request: Request, admin: dict = Depends(require_admin)):
    try:
        result = await partner.settle_rebate(workspace_id, month, admin["user_id"])
    except partner.TariffError as exc:
        raise HTTPException(409, detail={"code": "REBATE_NOT_SETTLEABLE", "message": str(exc)}) from exc
    await record(admin["user_id"], workspace_id, "admin.partner_rebate.settle", "partner_rebate", result["id"],
                 {"month": month, "rebate": result["rebate"], "reason": body.reason, "replayed": result["replayed"]}, request)
    return result
