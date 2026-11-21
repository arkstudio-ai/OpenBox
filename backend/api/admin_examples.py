"""Platform-admin console for the Light reference example library (docs/LIGHT_MODE_IMPL_PLAN.md §11).

Collect one finished session's output (``prefill`` then ``create``), make an
example by hand from existing assets, edit, enable or disable, list with
filters, and see how often each one was shown to the model. Files are never
copied: an example points at the assets by id and bucket key.
"""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from audit import record
from core.identifier import ascending
from db.base import get_db_session
from db.models.light_example import ENTRY_IDS, STATUSES, LightExample
from light import examples
from light import taxonomy as taxonomy_mod
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
    scene: str | None = Field(default=None, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=12)
    rating: int = Field(default=3, ge=1, le=5)
    status: Literal["active", "disabled"] = "active"
    title: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=2000)
    recipe: str = Field(default="", max_length=20_000)
    #: The look in words; omit to keep what was read from the picture (light/example_look.py).
    look: str | None = Field(default=None, max_length=1200)
    #: The reference card; omit to keep what was read (light/example_card.py). A text that differs
    #: from the stored one counts as the team's own and is never read over.
    card: str | None = Field(default=None, max_length=2400)
    inputs: list[AssetRef] = Field(default_factory=list, max_length=12)
    outputs: list[AssetRef] = Field(default_factory=list, max_length=12)
    source: dict = Field(default_factory=dict)


class PrefillIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(min_length=1, max_length=64)
    asset_id: str | None = Field(default=None, max_length=64)


class SceneIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    #: Omitted for one the team just added; the server names it.
    id: str | None = Field(default=None, max_length=32)
    label: str = Field(min_length=1, max_length=20)


class ShopTypeIn(SceneIn):
    keywords: list[str] = Field(default_factory=list, max_length=16)


class IndustryIn(SceneIn):
    shop_types: list[ShopTypeIn] = Field(min_length=1, max_length=taxonomy_mod.MAX_SHOP_TYPES)


class TaxonomyIn(BaseModel):
    """The scenes of the entries being changed, and all the industries with their shop types."""
    model_config = ConfigDict(extra="forbid")
    scenes: dict[str, list[SceneIn]] = Field(default_factory=dict)
    industries: list[IndustryIn] = Field(min_length=1, max_length=taxonomy_mod.MAX_INDUSTRIES)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"]


#: Look readings started by a save; kept so the tasks are not collected mid-flight.
_READING: set[asyncio.Task] = set()


def _read_look_later(example_id: str, user: dict) -> None:
    from light import example_look
    task = asyncio.create_task(example_look.read_for_console(example_id, user["user_id"], user.get("workspace_id")))
    _READING.add(task)
    task.add_done_callback(_READING.discard)


def _read_card_later(example_id: str, user: dict, *, force: bool = False) -> None:
    from light import example_card
    task = asyncio.create_task(example_card.read_for_console(
        example_id, user["user_id"], user.get("workspace_id"), force=force))
    _READING.add(task)
    task.add_done_callback(_READING.discard)


def _apply_card(row: LightExample, body: ExampleIn, piece_changed: bool) -> None:
    """The team's own text stays; a card the model wrote goes when the piece or the recipe it read changed."""
    from light import example_card
    card = (row.card or "") if body.card is None else body.card
    if card != (row.card or ""):
        row.card = card
        # Emptied by hand means "read it again".
        row.card_meta = {"state": "edited", "at": example_card._now()} if card else {}
    elif piece_changed and (row.card_meta or {}).get("state") != "edited":
        row.card, row.card_meta = "", {}
    else:
        row.card = card


def _bad(message: str):
    return HTTPException(422, detail={"code": "EXAMPLE_INVALID", "message": message})


def _check_classification(entry_id: str, industry: str, shop_type: str, scene: str | None = None) -> None:
    tax = taxonomy()
    if entry_id not in ENTRY_IDS or entry_id not in tax.entry_ids():
        raise _bad("unknown entry_id")
    if industry not in tax.industry_ids():
        raise _bad("unknown industry")
    if shop_type not in tax.shop_type_ids(industry):
        raise _bad("unknown shop_type for this industry")
    if scene and scene not in tax.scene_ids(entry_id):
        raise _bad("unknown scene for this entry")


async def _apply(row: LightExample, body: ExampleIn) -> None:
    await taxonomy_mod.refresh(force=True)  # another worker may have saved the classification
    _check_classification(body.entry_id, body.industry, body.shop_type, body.scene)
    outputs = await examples.resolve_assets([ref.asset_id for ref in body.outputs])
    if body.outputs and not outputs:
        raise _bad("outputs name no known asset")
    kind = taxonomy().media(body.entry_id)
    if outputs and kind and not any(str(o.get("mime") or "").startswith(kind + "/") for o in outputs):
        # A menu made in a 做一条视频 chat is a menu example; filed under the video entry it would be
        # fed to every video as a model (gw2 2026-11-15: all four "video" examples were menu designs).
        raise _bad("this entry's examples need a video output" if kind == "video"
                   else "this entry's examples need an image output")
    row.entry_id, row.industry, row.shop_type = body.entry_id, body.industry, body.shop_type
    row.subtype = body.subtype or None
    row.scene = body.scene or None
    row.tags = examples._tags(body.tags)
    row.rating, row.status, row.title = body.rating, body.status, body.title
    recipe_before = row.recipe or ""
    row.notes, row.recipe = body.notes, body.recipe
    row.inputs = await examples.resolve_assets([ref.asset_id for ref in body.inputs])
    shown_before = (examples._shown_output(row) or {}).get("oss_key") if row.outputs else None
    row.outputs = outputs
    if body.look is not None:
        row.look = body.look
    elif (examples._shown_output(row) or {}).get("oss_key") != shown_before:
        row.look = ""  # another picture leads now; it is read again when the card next shows it
    _apply_card(row, body, (examples._shown_output(row) or {}).get("oss_key") != shown_before
                or body.recipe != recipe_before)
    row.source = {k: v for k, v in body.source.items()
                  if k in ("session_id", "job_id", "user_id", "workspace_id", "shop_name", "headline") and v is not None}
    row.updated_at = examples._now()


async def _example(db, example_id: str) -> LightExample:
    row = await db.get(LightExample, example_id)
    if row is None:
        raise HTTPException(404, detail={"code": "EXAMPLE_NOT_FOUND"})
    return row


def _taxonomy_view(tax) -> dict:
    return {**tax.public(), "builtin": taxonomy_mod.builtin()}


def _kept(given: str | None, known: set[str]) -> str:
    """The id of something that was already there; a new id for anything else."""
    return given if given and given in known else taxonomy_mod.new_id()


def _keywords(words: list[str]) -> list[str]:
    seen: list[str] = []
    for word in words:
        word = " ".join(str(word).split())[:20]
        if word and word not in seen:
            seen.append(word)
    return seen


async def _in_use(current, edited) -> list[str]:
    """What the edit removes that examples still carry, as ``label（n 条）``."""
    async with get_db_session() as db:
        scenes = {(e, s): int(n) for e, s, n in (await db.execute(
            select(LightExample.entry_id, LightExample.scene, func.count())
            .where(LightExample.scene.is_not(None)).group_by(LightExample.entry_id, LightExample.scene))).all()}
        shops = {(i, s): int(n) for i, s, n in (await db.execute(
            select(LightExample.industry, LightExample.shop_type, func.count())
            .group_by(LightExample.industry, LightExample.shop_type))).all()}
    held: list[str] = []
    for entry in current.entries:
        kept = set(edited.scene_ids(entry.id))
        held += [f"{entry.label} / {scene.label}（{scenes[(entry.id, scene.id)]} 条）" for scene in entry.scenes
                 if scene.id not in kept and scenes.get((entry.id, scene.id))]
    for industry in current.industries:
        kept = set(edited.shop_type_ids(industry.id))
        held += [f"{industry.label} / {shop.label}（{shops[(industry.id, shop.id)]} 条）" for shop in industry.shop_types
                 if shop.id not in kept and shops.get((industry.id, shop.id))]
    return held


@router.get("/taxonomy")
async def get_taxonomy(user=Depends(live_admin)):
    return _taxonomy_view(await taxonomy_mod.refresh(force=True))


@router.put("/taxonomy")
async def put_taxonomy(body: TaxonomyIn, request: Request, user=Depends(live_admin)):
    """Save the team's scenes, industries and shop types over the file that ships.

    Refused: removing something the code depends on (a built-in scene or industry, an industry's
    generic shop type) and removing anything an example still carries — move those examples first."""
    from pydantic import ValidationError
    from db.models.light_taxonomy import LightTaxonomy
    shipped = taxonomy_mod.base()
    current = await taxonomy_mod.refresh(force=True)
    try:
        scenes = {}
        for entry_id, items in body.scenes.items():
            known = set(current.scene_ids(entry_id))
            scenes[entry_id] = [{"id": _kept(item.id, known), "label": item.label} for item in items]
        industries = []
        for item in body.industries:
            industry_id = _kept(item.id, set(current.industry_ids()))
            known = set(current.shop_type_ids(industry_id)) | {taxonomy_mod.GENERIC}
            industries.append({"id": industry_id, "label": item.label, "shop_types": [
                {"id": _kept(shop.id, known), "label": shop.label, "keywords": _keywords(shop.keywords)}
                for shop in item.shop_types]})
        # Entries the edit does not name keep the scenes they have now.
        scenes = {**{e.id: [s.model_dump() for s in e.scenes] for e in current.entries}, **scenes}
        custom = taxonomy_mod.Custom.model_validate({"scenes": scenes, "industries": industries})
        taxonomy_mod.check(shipped, custom)
    except (ValidationError, taxonomy_mod.TaxonomyInvalid) as exc:
        message = str(exc) if isinstance(exc, taxonomy_mod.TaxonomyInvalid) else "a name or keyword is not valid"
        raise HTTPException(422, detail={"code": "TAXONOMY_INVALID", "message": message})
    held = await _in_use(current, taxonomy_mod.merge(shipped, custom))
    if held:
        raise HTTPException(409, detail={"code": "TAXONOMY_IN_USE", "message": "；".join(held[:8])})
    async with get_db_session() as db:
        row = await db.get(LightTaxonomy, taxonomy_mod.ROW_ID)
        if row is None:
            row = LightTaxonomy(id=taxonomy_mod.ROW_ID)
            db.add(row)
        row.data, row.updated_by, row.updated_at = custom.model_dump(), user["user_id"], examples._now()
        await db.commit()
    saved = await taxonomy_mod.refresh(force=True)
    await record(user["user_id"], None, "admin.light_taxonomy_update", "light_taxonomy", taxonomy_mod.ROW_ID,
                 {"version": saved.version, "industries": len(saved.industries),
                  "scenes": {entry.id: len(entry.scenes) for entry in saved.entries}}, request)
    return _taxonomy_view(saved)


@router.get("")
async def list_examples(entry_id: str | None = Query(None), industry: str | None = Query(None),
                        shop_type: str | None = Query(None), scene: str | None = Query(None, max_length=32),
                        status: str | None = Query(None),
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
    if scene:  # ``none`` lists the examples nobody gave a scene yet
        conditions.append(LightExample.scene.is_(None) if scene == "none" else LightExample.scene == scene)
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


@router.get("/coverage")
async def coverage(user=Depends(live_admin)):
    """Active examples per entry and scene, so the team sees which cell the example card cannot fill.

    A cell needs ``target`` examples for the card to show a full row of the owner's own scene."""
    await taxonomy_mod.refresh()
    async with get_db_session() as db:
        counts = {(entry, scene): int(n) for entry, scene, n in (await db.execute(
            select(LightExample.entry_id, LightExample.scene, func.count())
            .where(LightExample.status == "active")
            .group_by(LightExample.entry_id, LightExample.scene))).all()}
    rows = []
    for entry in taxonomy().entries:
        cells = [{"scene": scene.id, "label": scene.label, "count": counts.get((entry.id, scene.id), 0)}
                 for scene in entry.scenes]
        known = {scene.id for scene in entry.scenes}
        rows.append({"entry_id": entry.id, "label": entry.label, "scenes": cells,
                     "unclassified": sum(n for (e, s), n in counts.items() if e == entry.id and s not in known),
                     "total": sum(n for (e, _), n in counts.items() if e == entry.id)})
    return {"target": 3, "entries": rows}


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
    _read_look_later(row.id, user)
    _read_card_later(row.id, user)
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
    _read_look_later(example_id, user)
    _read_card_later(example_id, user)
    return view


@router.post("/{example_id}/card", status_code=202)
async def read_card(example_id: str, request: Request, user=Depends(live_admin)):
    """Write the reference card again from the piece and the recipe; the console polls the example for it."""
    from light import example_card
    async with get_db_session() as db:
        row = await _example(db, example_id)
        if example_card.kind(row) is None:
            raise _bad("this example has no picture or clip to read")
    await record(user["user_id"], None, "admin.light_example_card_read", "light_example", example_id, None, request)
    _read_card_later(example_id, user, force=True)
    return {"example_id": example_id, "state": "reading"}


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
