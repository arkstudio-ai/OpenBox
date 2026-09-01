"""Compatibility auth surface for the no-JWT desktop deployment.

The desktop still uses the same v2 frontend as SaaS.  It has no credentials or
refresh tokens, but the UI needs a stable current user, preferences, and a
WebSocket handshake value.  The WebSocket endpoint ignores tickets when auth
is disabled; returning an inert marker here keeps the client protocol uniform.
"""
from fastapi import APIRouter
from pydantic import BaseModel

from auth.preview_token import revoke_preview_tokens
from db.repository.preference_repo import PgPreferenceRepo


router = APIRouter(prefix="/api/auth", tags=["Auth"])
_preferences = PgPreferenceRepo()
_USER = {"id": "default", "username": "default", "role": "admin"}


class PreferencesUpdate(BaseModel):
    theme: str | None = None
    default_model: str | None = None
    default_agent: str | None = None
    default_variant: str | None = None
    sidebar_open: bool | None = None
    right_panel_open: bool | None = None
    bottom_panel_height: int | None = None
    extra: dict | None = None


@router.get("/me")
async def get_me():
    return _USER


@router.get("/me/preferences")
async def get_preferences():
    return await _preferences.get("default") or {}


@router.put("/me/preferences")
async def update_preferences(body: PreferencesUpdate):
    fields = {key: value for key, value in body.model_dump().items() if value is not None}
    return await _preferences.upsert("default", **fields)


@router.post("/ticket")
async def get_ticket():
    return {"ticket": "single-user"}


@router.post("/logout")
async def logout():
    await revoke_preview_tokens("default")
    return {"ok": True}
