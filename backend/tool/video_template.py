"""Load a reusable storyboard and scope its reference frames to the conversation."""

import asyncio
import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert

GALLERY_WISHES = "gallery_wishes"
GALLERY_WISHES_VERSION = "gallery-wishes-v4"
GALLERY_JOB_VERSIONS = (GALLERY_WISHES_VERSION, "gallery-wishes-v3")
ROOT = Path(__file__).resolve().parents[1] / "assets/video_templates/gallery-wishes"
_THIRD_WISH_PREFIX = "第三个愿望的指定台词：「"


def recipe() -> dict:
    return json.loads((ROOT / "template.json").read_text(encoding="utf-8"))


def effective_h3_profile(form: dict | None, requested: str | None) -> str | None:
    if requested == "poster":
        return "poster"
    form = form or {}
    pinned_recipe = (form.get("template_recipe") or {}) if form.get("template_ref") else {}
    if form.get("template") == GALLERY_WISHES or pinned_recipe.get("reference_pack") == "gallery-stage":
        return "hybrid"
    return None


def promote_legacy_gallery_copy(form: dict) -> dict:
    """Fold old separately saved dialogue into the editable creative brief."""
    if "third_wish_line" not in form:
        return form
    migrated = dict(form)
    line = (migrated.pop("third_wish_line") or "").strip()
    copy = (migrated.get("reference_copy") or "").strip()
    if migrated.get("template") == GALLERY_WISHES and line and line not in copy:
        tagged = f"{_THIRD_WISH_PREFIX}{line}」"
        migrated["reference_copy"] = f"{copy}\n\n{tagged}" if copy else tagged
    return migrated


def verify_third_wish_line(form, shot, prompt):
    if not form or form.get("template") != GALLERY_WISHES or shot != 4:
        return
    explicit = next((line[len(_THIRD_WISH_PREFIX):-1] for line in
                     reversed((form.get("reference_copy") or "").splitlines())
                     if line.startswith(_THIRD_WISH_PREFIX) and line.endswith("」")), "")
    expected = form.get("third_wish_line") or explicit
    if not expected:
        return
    from tool.video_comfyui import dialogue
    from tool.video_providers import VideoRequestError

    def spoken_words(line):
        return unicodedata.normalize("NFKC", line).strip().rstrip("。.!?")

    if not any(spoken_words(line) == spoken_words(expected) for line in dialogue(prompt)):
        raise VideoRequestError("第 4 段须将第三个愿望的指定台词放在独立的 H3 对白标签中。")


async def _asset(name, ctx, project_id, *, oss, data=None) -> str:
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    is_video = name.endswith(".mp4")
    if data is None:
        data = (ROOT / ("references" if is_video else "frames") / name).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    identity = f"{ctx.user_id}:{ctx.workspace_id}:{ctx.session_id}:{name}:{digest}"
    asset_id = "vtpl-" + hashlib.sha256(identity.encode()).hexdigest()[:44]
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
        if row is not None:
            if row.is_deleted or row.status != "ready" or row.user_id != ctx.user_id:
                raise RuntimeError("模板素材在当前会话不可用。")
            return row.id
    mime = "video/mp4" if is_video else "image/jpeg"
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    await oss.put_object(key, data, content_type=mime, forbid_overwrite=True)
    async with get_db_session() as db:
        await db.execute(insert(FileAsset).values(
            id=asset_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            session_id=ctx.session_id, project_id=project_id, name=name, oss_key=key,
            mime=mime, size=len(data), status="ready", source="agent", transient=True,
            created_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=[FileAsset.id]))
    return asset_id


async def prepare_builtin(template_id, ctx):
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.session import Session
    from light import h3_recipes

    async with get_db_session() as db:
        session = await db.get(Session, ctx.session_id)
        form = session.light_production if session else {}
        if (not session or session.is_deleted or session.user_id != ctx.user_id
                or session.workspace_id != ctx.workspace_id or not ((form or {}).get("builtin_h3") or (form or {}).get("agent_recipe"))
                or form.get("template") != template_id):
            raise ValueError("请选择本次制作使用的模板。")
    files = h3_recipes.reference_files(template_id)
    oss = get_oss()
    ids = await asyncio.gather(*(_asset(f"{template_id}-{f.name}", ctx, ctx.project_id,
                                       oss=oss, data=f.read_bytes()) for f in files))
    return {"template": template_id, "name": (form.get("template_recipe") or {}).get("title") or template_id, "shots": [
        {"shot": index, "asset_id": asset_id,
         "role": "reference_video" if f.suffix == ".mp4" else "reference_image"}
        for index, (f, asset_id) in enumerate(zip(files, ids, strict=True), 1)]}


async def prepare(template_id, ctx) -> dict:
    from core.oss import get_oss

    from light import h3_recipes
    if template_id in h3_recipes.TEMPLATES:
        return await prepare_builtin(template_id, ctx)
    if template_id != GALLERY_WISHES or not ctx.user_id or not ctx.workspace_id or not ctx.session_id:
        raise ValueError("请选择「画廊里的三个愿望」模板并在已登录的工作区运行。")
    storyboard = recipe()
    names = sorted(
        {name for shot in storyboard["shots"] for name in shot.get("frames", {}).values()}
        | {name for shot in storyboard["shots"] for name in shot["still_options"]}
        | {shot["reference_video"] for shot in storyboard["shots"] if "reference_video" in shot}
    )
    oss = get_oss()
    ids = await asyncio.gather(*(_asset(name, ctx, ctx.project_id, oss=oss) for name in names))
    assets = dict(zip(names, ids, strict=True))
    for shot in storyboard["shots"]:
        shot["input_assets"] = [{"role": role, "asset_id": assets[name]}
                                for role, name in shot.pop("frames", {}).items()]
        shot["still_options"] = [{"name": name, "asset_id": assets[name]} for name in shot["still_options"]]
        if reference := shot.pop("reference_video", None):
            shot["input_assets"].append({"role": "reference_video", "asset_id": assets[reference]})
    return storyboard
