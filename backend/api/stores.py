"""Store profile API (门店档案).

One store per workspace in this phase. Rows of another workspace read as 404.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.log import create_logger
from db.models.store import CATEGORIES, OPEN_CATEGORIES, PLATFORMS
from store import service as store_service

log = create_logger("api.stores")

router = APIRouter(prefix="/api/stores", tags=["stores"], dependencies=[Depends(get_workspace)])


class StoreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category: str = "food"
    main_platforms: list[str] = Field(default_factory=list, max_length=4)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=64)

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        return value

    @field_validator("main_platforms")
    @classmethod
    def _platforms(cls, values: list[str]) -> list[str]:
        bad = [v for v in values if v not in PLATFORMS]
        if bad:
            raise ValueError(f"unknown platforms: {bad}")
        return values


class StorePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = None
    main_platforms: list[str] | None = Field(default=None, max_length=4)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=64)

    @field_validator("category")
    @classmethod
    def _category(cls, value: str | None) -> str | None:
        if value is not None and value not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        return value

    @field_validator("main_platforms")
    @classmethod
    def _platforms(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        bad = [v for v in values if v not in PLATFORMS]
        if bad:
            raise ValueError(f"unknown platforms: {bad}")
        return values


@router.get("")
async def list_stores(current_user: dict = Depends(get_current_user)):
    store = await store_service.get_store(current_user["workspace_id"])
    return {"items": [store] if store else [], "categories": list(CATEGORIES),
            "openCategories": list(OPEN_CATEGORIES), "platforms": list(PLATFORMS)}


@router.post("", status_code=201)
async def create_store(body: StoreCreate, current_user: dict = Depends(get_current_user)):
    try:
        return await store_service.create_store(
            workspace_id=current_user["workspace_id"], user_id=current_user["user_id"],
            name=body.name, category=body.category, main_platforms=body.main_platforms,
            address=body.address, city=body.city,
        )
    except store_service.StoreExists:
        raise HTTPException(status_code=409, detail={"code": "STORE_EXISTS", "message": "This workspace already has a store"},
                            headers={"X-Error-Code": "STORE_EXISTS"})


@router.patch("/{store_id}")
async def patch_store(store_id: str, body: StorePatch, current_user: dict = Depends(get_current_user)):
    updated = await store_service.update_store(
        workspace_id=current_user["workspace_id"], store_id=store_id, user_id=current_user["user_id"],
        patch=body.model_dump(exclude_unset=True),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return updated


@router.get("/{store_id}/starter-cards")
async def starter_cards(store_id: str, locale: str = Query("zh-CN", max_length=16),
                        current_user: dict = Depends(get_current_user)):
    store = await store_service.get_store(current_user["workspace_id"])
    if store is None or store["id"] != store_id:
        raise HTTPException(status_code=404, detail="Store not found")
    cards = await store_service.starter_cards_for(current_user["workspace_id"], current_user["user_id"], locale=locale)
    return {"items": cards or [], "personaStatus": store["personaStatus"]}


@router.post("/{store_id}/persona/regenerate")
async def regenerate_persona(store_id: str, current_user: dict = Depends(get_current_user)):
    store = await store_service.get_store(current_user["workspace_id"])
    if store is None or store["id"] != store_id:
        raise HTTPException(status_code=404, detail="Store not found")
    from store.persona_init import start_persona_init

    session_id = await start_persona_init(current_user["workspace_id"], current_user["user_id"],
                                          sites=store["mainPlatforms"], force=True)
    return {"ok": session_id is not None, "sessionId": session_id}
