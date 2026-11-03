"""What an uploaded video is, read by IMS instead of ffprobe (你拍我剪 F1).

The backend image has no ffmpeg and the free plan has no desktop, so the
facts the footage flow needs — duration, displayed size and orientation,
codec, whether there is sound — come from IMS ``RegisterMediaInfo`` +
``GetMediaInfo`` on the object's bucket URL (measured on light-test
2026-11-02: ~2 s per clip, HEVC/mov and rotated iPhone clips included). The
result is stored on ``file_assets.meta`` once; :func:`ensure_meta` probes on
demand when the upload hook has not finished yet.
"""
from __future__ import annotations

import asyncio
from typing import Any

from core.log import create_logger

log = create_logger("video.media_probe")

POLL_SECONDS = 1.5
MAX_POLLS = 40
_tasks: set[asyncio.Task] = set()


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN


def meta_from_media_info(info: dict, media_id: str | None) -> dict:
    """The facts we keep, from a GetMediaInfo body; width/height are as displayed (rotation applied)."""
    media = (info or {}).get("MediaInfo") or {}
    files = media.get("FileInfoList") or [{}]
    first = files[0] if files else {}
    basic = first.get("FileBasicInfo") or {}
    video = (first.get("VideoStreamInfoList") or [{}])[0] or {}
    audio = first.get("AudioStreamInfoList") or []
    width = int(_num(video.get("Width")) or _num(basic.get("Width")) or 0)
    height = int(_num(video.get("Height")) or _num(basic.get("Height")) or 0)
    rotation = int(_num(video.get("Rotate")) or 0) % 360
    if rotation in (90, 270):
        width, height = height, width
    duration = _num(basic.get("Duration")) or _num(video.get("Duration"))
    return {
        "duration_sec": round(duration, 3) if duration else None,
        "width": width or None, "height": height or None, "rotation": rotation,
        "orientation": ("portrait" if height > width else "landscape" if width > height else "square")
        if width and height else None,
        "codec": (video.get("CodecName") or None),
        "fps": _num(video.get("Fps")),
        "has_audio": bool(audio),
        "format": basic.get("FormatName") or None,
        "media_id": media_id,
        "source": "ims",
    }


async def probe_object(oss_key: str) -> dict:
    """Register the object with IMS and read its media info."""
    from core.oss import get_oss
    from video import ims_client as ims
    oss = get_oss()
    url = f"https://{oss.host}/{oss_key}"
    registered = await ims._rpc("RegisterMediaInfo", {"InputURL": url, "MediaType": "video", "Overwrite": "true"})
    media_id = registered.get("MediaId")
    if not media_id:
        raise ims.ImsError("NoMediaId", "RegisterMediaInfo returned no MediaId")
    info: dict = {}
    for _ in range(MAX_POLLS):
        info = await ims._rpc("GetMediaInfo", {"MediaId": media_id})
        media = info.get("MediaInfo") or {}
        status = (media.get("MediaBasicInfo") or {}).get("Status")
        files = media.get("FileInfoList") or []
        if status in ("Normal", "Failed") or (files and (files[0].get("FileBasicInfo") or {}).get("Duration")):
            break
        await asyncio.sleep(POLL_SECONDS)
    meta = meta_from_media_info(info, media_id)
    if not meta.get("duration_sec"):
        raise ims.ImsError("NoDuration", "IMS could not read this video")
    return meta


async def ensure_meta(asset_id: str, *, user_id: str) -> dict | None:
    """The asset's media facts, probing (and saving) them when missing; None when it is not a readable video."""
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
        if row is None or row.user_id != user_id or row.is_deleted or not str(row.mime or "").startswith("video/"):
            return None
        if isinstance(row.meta, dict) and row.meta.get("duration_sec"):
            return row.meta
        key = row.oss_key
    try:
        meta = await probe_object(key)
    except Exception as exc:
        log.warning("media probe failed asset=%s error=%s: %s", asset_id, type(exc).__name__, str(exc)[:200])
        return None
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
        if row is not None:
            row.meta = meta
    return meta


def probe_in_background(asset_id: str, user_id: str) -> None:
    """Upload hook: read the facts now so the agent finds them ready; never blocks the request."""
    task = asyncio.create_task(ensure_meta(asset_id, user_id=user_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
