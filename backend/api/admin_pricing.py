"""Operator pricing: every billable item's cost, sale price and margin, editable without a release.

Reads are not audited (the table is refreshed constantly and a trail of
"views" would say nothing); every write lands in ``audit_logs`` under an
idempotent receipt, like the billing writes.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from auth.middleware import require_admin
from billing import pricing_admin
from billing.pricing_admin import PricingPreview, PricingRevert, PricingWrite
from core.config import get_config

router = APIRouter(
    prefix="/api/admin/pricing",
    tags=["admin-pricing"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
async def table():
    return await pricing_admin.pricing_table(get_config())


@router.post("/preview")
async def preview(body: PricingPreview):
    try:
        return pricing_admin.preview(body)
    except pricing_admin.RuleError as exc:
        return JSONResponse(422, {"detail": {"code": exc.code, "message": str(exc)}})


@router.get("/export")
async def export():
    data = pricing_admin.export_catalogue()
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="rates-{data.get("version", "export")}.json"'})


@router.get("/{key}/history")
async def history(key: str):
    return await pricing_admin.history(key)


@router.put("/{key}")
async def write(key: str, body: PricingWrite, request: Request, admin: dict = Depends(require_admin)):
    return await pricing_admin.write_rule(key, admin["user_id"], body, request, get_config())


@router.post("/{key}/revert")
async def revert(key: str, body: PricingRevert, request: Request, admin: dict = Depends(require_admin)):
    return await pricing_admin.revert_rule(key, admin["user_id"], body, request)
