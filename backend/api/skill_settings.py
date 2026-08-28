"""Per-user skill enable/disable (§6.6, §12.1)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.user_skill_setting import UserSkillSetting
from skill_runtime import service
from skill_runtime.manifest import get_manifest, load_builtin_manifests

router = APIRouter(prefix="/api/skills", tags=["SkillSettings"])


@router.get("/settings")
async def list_skill_settings(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    manifests = load_builtin_manifests()
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(UserSkillSetting).where(UserSkillSetting.user_id == user_id)
            )
        ).scalars().all()
    overrides = {r.skill_key: r for r in rows}

    skills = []
    for skill_key, manifest in sorted(manifests.items()):
        setting = overrides.get(skill_key)
        skills.append(
            {
                "skillKey": skill_key,
                "name": manifest.name,
                "displayName": manifest.display_name or manifest.name,
                "version": manifest.version,
                "defaultEnabled": manifest.default_enabled,
                "enabled": setting.enabled if setting else manifest.default_enabled,
                "operations": sorted(manifest.operations),
                "phases": manifest.phases,
                "settings": (setting.settings_data if setting else {}) or {},
            }
        )
    return {"skills": skills}


class SkillSettingsRequest(BaseModel):
    enabled: bool
    settings: dict | None = None


@router.put("/{skill_key}/settings")
async def put_skill_settings(
    skill_key: str, body: SkillSettingsRequest, current_user: dict = Depends(get_current_user)
):
    if get_manifest(skill_key) is None:
        raise HTTPException(status_code=404, detail="unknown skill")
    await service.set_skill_enabled(
        current_user["user_id"], skill_key, body.enabled, settings_data=body.settings
    )
    return {"skillKey": skill_key, "enabled": body.enabled}
