"""Independent, owned still-image requests for the two ends of a video take."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assets.service import owned_ready_query
from core.log import create_logger
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob

log = create_logger("light.shot_frame_edits")
_tasks: set[asyncio.Task] = set()


class FrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=1)
    version_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=16, max_length=48, pattern=r"^[a-zA-Z0-9_-]+$")
    role: Literal["first_frame", "last_frame"]
    prompt: str = Field(min_length=1, max_length=2400)
    use_current: bool = True


def drafts(form):
    return form.get("shot_frame_drafts") or {}


async def read_drafts(db, row, *, now):
    """Look up completed images even if a worker stopped after saving the asset."""
    stored = drafts(row.light_production)
    filenames = [entry["filename"] for sides in stored.values() for entry in sides.values()]
    images = {image.name: image for image in (await db.scalars(
        owned_ready_query(row.user_id, row.workspace_id)
        .where(FileAsset.session_id == row.id, FileAsset.name.in_(filenames))
        .order_by(FileAsset.created_at)
    )).all()} if filenames else {}

    return {
        version: {
            role: (
                entry,
                images.get(entry["filename"]),
                "ready" if entry["filename"] in images else
                "generating" if entry["status"] == "generating" and entry["until"] > now else "failed",
            )
            for role, entry in sides.items()
        }
        for version, sides in stored.items()
    }


def references(form, index):
    # A modern guided remake has explicit cast and location references. Use
    # them instead of the historical motion footage for a clean still.
    from light import silk_road
    assets = form.get("asset_ids") or []
    plan = form.get("remake_adaptation") or {}
    world = (plan.get("world") or {}) if isinstance(plan, dict) else {}
    takes = world.get("takes") or []
    if form.get("template") == silk_road.TEMPLATE and 1 <= index <= len(takes):
        take = takes[index - 1]
        selected = [world.get("cast_material"), take.get("location_material")]
        if silk_road.case()["shots"][index - 1]["reference_product"]:
            selected.append(world.get("product_material"))
        return list(dict.fromkeys(assets[i - 1] for i in selected if i and 1 <= i <= len(assets)))
    return assets[:3]


def _dimensions(meta):
    width, height = meta.get("width") or 720, meta.get("height") or 1280
    scale = 1536 / max(width, height)
    return f"{max(512, round(width * scale / 16) * 16)}x{max(512, round(height * scale / 16) * 16)}"


async def submit(user_id, workspace_id, session_id, body: FrameRequest):
    from light.production import _check_revision, _lock, _now, _views, get
    from light.remake import _digest
    from light.shot_editor import _fail
    from tool.tool import ToolContext

    async with get_db_session() as db:
        row = await _lock(db, session_id, user_id, workspace_id)
        form = row.light_production
        stored = drafts(form)
        sides = stored.get(body.version_id, {})
        entry = sides.get(body.role, {})
        if entry.get("request_id") == body.request_id:
            if (entry["prompt"], entry["use_current"]) != (body.prompt, body.use_current):
                raise _fail("The image request was already used with different inputs.")
            return (await _views(db, [row], include_shots=True))[0]
        _check_revision(form, body.revision)
        view = (await _views(db, [row], include_shots=True))[0]
        editor = view.get("shot_editor") or {}
        shot = next((shot for shot in editor.get("shots", ()) if (shot["version"] or {}).get("id") == body.version_id), None)
        if (view["status"] != "completed" or editor.get("active") or not shot
                or not shot["editable"] or not shot["frame_editable"]):
            raise _fail("Open the latest finished fragment before generating a frame.")
        if ((shot.get("frame_drafts") or {}).get(body.role) or {}).get("status") == "generating":
            raise _fail("This frame is already generating.")
        source = await db.get(VideoJob, body.version_id)
        if source is None or source.user_id != user_id or source.session_id != session_id:
            raise _fail("The fragment is unavailable.")
        ids = references(form, shot["index"])
        ref_rows = list((await db.scalars(owned_ready_query(user_id, workspace_id).where(FileAsset.id.in_(ids)))).all())
        images = {asset.id for asset in ref_rows if asset.mime.startswith("image/")}
        ids = [asset_id for asset_id in ids if asset_id in images]
        key = f"light-frame:{session_id}:{body.version_id}:{body.role}:{body.request_id}"
        now = _now()
        entry = {"request_id": body.request_id, "prompt": body.prompt, "use_current": body.use_current,
                 "status": "generating", "filename": f"wan-story-{_digest(key)}-shot-frame.png",
                 "until": (now + timedelta(minutes=12)).isoformat()}
        row.light_production = {**form, "revision": form["revision"] + 1, "shot_frame_drafts": {
            **stored, body.version_id: {**sides, body.role: entry},
        }}
        row.updated_at = now
        ctx = ToolContext(session_id=session_id, user_id=user_id, workspace_id=workspace_id,
                          project_id=row.project_id or "")
    job = asyncio.create_task(_generate(ctx, source, body, ids, key))
    _tasks.add(job)
    job.add_done_callback(_tasks.discard)
    return await get(user_id, workspace_id, session_id)


async def _generate(ctx, source, body, refs, key):
    from light.production import _lock
    from light.shot_editor import _references, _snapshot
    from light.wan_remake import _still
    from video.media_probe import ensure_meta

    error = None
    try:
        meta = await ensure_meta(source.output_asset_id, user_id=ctx.user_id) or {}
        if body.use_current:
            frame = _references(source.request_data or {}).get(body.role)
            if not frame and meta.get("duration_sec"):
                frame = await _snapshot(ctx, source.output_asset_id, body.role, meta)
            if frame:
                refs = [frame, *refs]
        await _still(ctx, key, "shot-frame", body.prompt, list(dict.fromkeys(refs)),
                     size=_dimensions(meta), quality="medium")
    except Exception as exc:
        error = str(exc)[:240]
        log.warning("Frame generation stopped for %s/%s: %s", ctx.session_id, body.role, type(exc).__name__)
    try:
        async with get_db_session() as db:
            row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id)
            form = row.light_production
            stored = drafts(form)
            sides = stored.get(body.version_id, {})
            entry = sides.get(body.role, {})
            if entry.get("request_id") == body.request_id:
                row.light_production = {**form, "shot_frame_drafts": {
                    **stored, body.version_id: {**sides, body.role: {
                        **entry, "status": "failed" if error else "ready",
                        **({"error": error} if error else {}),
                    }},
                }}
    except Exception as exc:
        # The image asset's deterministic name is also used by the projection.
        log.warning("Could not settle the frame receipt for %s: %s", ctx.session_id, type(exc).__name__)
