"""Platform-admin console for the Light reference example library (docs/LIGHT_MODE_IMPL_PLAN.md §11).

Collect one finished session's output (``prefill`` then ``create``), make an
example by hand from existing assets, edit, enable or disable, list with
filters, and see how often each one was shown to the model. Files are never
copied: an example points at the assets by id and bucket key.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from audit import record
from core.identifier import ascending
from db.base import get_db_session
from db.models.light_example import ENTRY_IDS, STATUSES, LightExample
from light import examples
from light.taxonomy import taxonomy
from notifications.testing import live_admin

router = APIRouter(prefix="/api/admin/examples", tags=["admin-examples"])

LINK_SECONDS = 3600


class AssetRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    asset_id: str = Field(min_length=1, max_length=64)


class ExampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    entry_id: str = Field(max_length=32)
    industry: str = Field(max_length=32)
    shop_type: str = Field(default="other", max_length=32)
    subtype: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=12)
    rating: int = Field(default=3, ge=1, le=5)
    status: Literal["active", "disabled"] = "active"
    title: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=2000)
    recipe: str = Field(default="", max_length=20_000)
    inputs: list[AssetRef] = Field(default_factory=list, max_length=12)
    outputs: list[AssetRef] = Field(default_factory=list, max_length=12)
    source: dict = Field(default_factory=dict)


class PrefillIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(min_length=1, max_length=64)
    asset_id: str | None = Field(default=None, max_length=64)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"]


def _bad(message: str):
    return HTTPException(422, detail={"code": "EXAMPLE_INVALID", "message": message})


def _check_classification(entry_id: str, industry: str, shop_type: str) -> None:
    tax = taxonomy()
    if entry_id not in ENTRY_IDS or entry_id not in tax.entry_ids():
        raise _bad("unknown entry_id")
    if industry not in tax.industry_ids():
        raise _bad("unknown industry")
    if shop_type not in tax.shop_type_ids(industry):
        raise _bad("unknown shop_type for this industry")


async def _apply(row: LightExample, body: ExampleIn) -> None:
    _check_classification(body.entry_id, body.industry, body.shop_type)
    outputs = await examples.resolve_assets([ref.asset_id for ref in body.outputs])
    if body.outputs and not outputs:
        raise _bad("outputs name no known asset")
    row.entry_id, row.industry, row.shop_type = body.entry_id, body.industry, body.shop_type
    row.subtype = body.subtype or None
    row.tags = examples._tags(body.tags)
    row.rating, row.status, row.title = body.rating, body.status, body.title
    row.notes, row.recipe = body.notes, body.recipe
    row.inputs = await examples.resolve_assets([ref.asset_id for ref in body.inputs])
    row.outputs = outputs
    row.source = {k: v for k, v in body.source.items()
                  if k in ("session_id", "job_id", "user_id", "workspace_id", "shop_name", "headline") and v is not None}
    row.updated_at = examples._now()


async def _example(db, example_id: str) -> LightExample:
    row = await db.get(LightExample, example_id)
    if row is None:
        raise HTTPException(404, detail={"code": "EXAMPLE_NOT_FOUND"})
    return row


@router.get("/taxonomy")
async def get_taxonomy(user=Depends(live_admin)):
    return taxonomy().public()


@router.get("")
async def list_examples(entry_id: str | None = Query(None), industry: str | None = Query(None),
                        shop_type: str | None = Query(None), status: str | None = Query(None),
                        q: str | None = Query(None, max_length=80),
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                        user=Depends(live_admin)):
    conditions = []
    if entry_id:
        conditions.append(LightExample.entry_id == entry_id)
    if industry:
        conditions.append(LightExample.industry == industry)
    if shop_type:
        conditions.append(LightExample.shop_type == shop_type)
    if status:
        if status not in STATUSES:
            raise _bad("unknown status")
        conditions.append(LightExample.status == status)
    if q:
        conditions.append(LightExample.title.ilike(f"%{q}%"))
    async with get_db_session() as db:
        total = await db.scalar(select(func.count()).select_from(LightExample).where(*conditions))
        rows = (await db.scalars(select(LightExample).where(*conditions)
                                 .order_by(LightExample.created_at.desc(), LightExample.id.desc())
                                 .offset(offset).limit(limit))).all()
    return {"items": [examples.public(r) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}


@router.post("/prefill")
async def prefill_example(body: PrefillIn, user=Depends(live_admin)):
    """What one session gives a new example; the team fills shop type, tags, rating and notes."""
    draft = await examples.prefill(body.session_id, body.asset_id)
    if draft is None:
        raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND"})
    return draft


@router.post("", status_code=201)
async def create_example(body: ExampleIn, request: Request, user=Depends(live_admin)):
    row = LightExample(id=ascending("lex"), created_by=user["user_id"], created_at=examples._now(), use_count=0)
    await _apply(row, body)
    async with get_db_session() as db:
        db.add(row)
        await db.commit()
        view = examples.public(row)
    await record(user["user_id"], None, "admin.light_example_create", "light_example", row.id,
                 {"entry_id": row.entry_id, "industry": row.industry, "shop_type": row.shop_type}, request)
    return view


@router.get("/{example_id}")
async def get_example(example_id: str, user=Depends(live_admin)):
    async with get_db_session() as db:
        return examples.public(await _example(db, example_id))


@router.put("/{example_id}")
async def update_example(example_id: str, body: ExampleIn, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _example(db, example_id)
        await _apply(row, body)
        await db.commit()
        view = examples.public(row)
    await record(user["user_id"], None, "admin.light_example_update", "light_example", example_id, None, request)
    return view


@router.post("/{example_id}/status")
async def set_status(example_id: str, body: StatusIn, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _example(db, example_id)
        row.status, row.updated_at = body.status, examples._now()
        await db.commit()
        view = examples.public(row)
    await record(user["user_id"], None, "admin.light_example_status", "light_example", example_id,
                 {"status": body.status}, request)
    return view


@router.get("/assets/{asset_id}/url")
async def asset_url(asset_id: str, user=Depends(live_admin)):
    """A short-lived preview link for an input or output of the library (the console is not the owner)."""
    rows = await examples.resolve_assets([asset_id])
    if not rows:
        raise HTTPException(404, detail={"code": "ASSET_NOT_FOUND"})
    try:
        from core.oss import get_oss
        url = get_oss().presign_get(rows[0]["oss_key"], LINK_SECONDS)
    except Exception:
        raise HTTPException(503, detail={"code": "OSS_UNAVAILABLE"})
    return {"url": url, "mime": rows[0]["mime"], "name": rows[0]["name"], "expires_in": LINK_SECONDS}
