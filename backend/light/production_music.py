"""Music preferences for guided AI video renders."""
from __future__ import annotations

import asyncio
from subprocess import SubprocessError
from typing import Literal

from httpx import HTTPError

from core.log import create_logger
from core.oss import OssError
from light import shortcut_plan
from video import bgm_library, music_catalog, netease_music

MusicStyle = Literal["auto", "upbeat", "calm", "energetic", "off"]
log = create_logger("light.production_music")

_STYLES = {
    "upbeat": ("轻快 广告", "bgm-upbeat"),
    "calm": ("舒缓 吉他", "bgm-relaxing"),
    "energetic": ("动感 卡点 节奏 纯音乐", "bgm-dynamic"),
}
_AUTO = {
    "": ("轻松 美食 纯音乐", "bgm-cuisine"),
    "silk_road": ("轻柔 纯音乐", "bgm-relaxing"),
    "soda_story": ("俏皮 纯音乐", "bgm-quirky"),
    shortcut_plan.TEMPLATE: ("Carefree Kevin MacLeod", "bgm-quirky"),
}
CAREFREE = "netease:33190124"


def preference(form: dict) -> MusicStyle:
    return form.get("music_style") or ("off" if form.get("beat_music") is False else "auto")


async def for_composition(ctx, key: str | None, *, preserve_mixed_audio=None) -> tuple[MusicStyle, str] | None:
    if not ctx.user_id or not key or not key.startswith(f"light:{ctx.session_id}:"):
        return None
    from db.base import get_db_session
    from db.models.session import Session

    async with get_db_session() as db:
        row = await db.get(Session, ctx.session_id)
    if row is None or row.is_deleted or row.user_id != ctx.user_id or row.light_entry != "video":
        return None
    run = (row.light_production or {}).get("run") or {}
    prefix = run.get("job_prefix")
    if not prefix or run.get("action") != "render" or not run.get("music_style"):
        return None
    final = f"{prefix}:final"
    if key == final or key.startswith(final + ":"):
        if key != final and preserve_mixed_audio is not None:
            from sqlalchemy import or_, select
            from db.models.video_job import VideoJob
            from light.shot_editor import audible_assets

            sources = audible_assets(preserve_mixed_audio)
            if sources:
                async with get_db_session() as db:
                    master = await db.scalar(select(VideoJob.id).where(
                        VideoJob.session_id == ctx.session_id, VideoJob.user_id == ctx.user_id,
                        VideoJob.kind == "compose", VideoJob.status == "completed",
                        or_(VideoJob.idempotency_key == final,
                            VideoJob.idempotency_key.startswith(final + ":", autoescape=True)),
                        VideoJob.output_asset_id.in_(sources),
                    ).limit(1))
                if master:
                    return None
        return run["music_style"], run["music_template"]
    return None


async def _netease_track(ctx, query, duration, *, exact_id=None):
    if netease_music.api_base():
        try:
            async with asyncio.timeout(24):
                choices = await netease_music.search(query, duration, set(), ctx)
                candidates = [choice for choice in choices if not exact_id or choice["track_id"] == exact_id]
                for choice in candidates[:3]:
                    try:
                        return await netease_music.prepare(choice["track_id"], duration, ctx)
                    except (HTTPError, OSError, OssError, SubprocessError, ValueError) as exc:
                        log.info("NetEase recording unavailable (%s)", type(exc).__name__)
        except (HTTPError, OSError, ValueError) as exc:
            log.info("NetEase soundtrack unavailable (%s)", type(exc).__name__)
    return None


async def mix(ctx, timeline, style: MusicStyle, template: str, host: str, *, track_id: str | None = None):
    """Apply the chosen background track to a resolved edit, preserving its speech and cuts."""
    original_bed = [clip for clip in timeline.audio if clip.library_id]
    edit = timeline.model_copy(update={"audio": [clip for clip in timeline.audio if not clip.library_id]})
    data = {"music_style": style}
    if style == "off":
        return edit, data
    total = edit.duration_sec
    if not total or len(edit.audio) >= 6:
        return timeline, {**data, "music_source": "template" if original_bed else "unavailable"}

    query, tag = _STYLES.get(style, _AUTO.get(template, _STYLES["upbeat"]))
    spans = edit.shot_spans()
    shortcut = template == shortcut_plan.TEMPLATE and len(spans) >= shortcut_plan.REVEAL
    start = spans[shortcut_plan.REVEAL - 1][0] if shortcut and spans[shortcut_plan.REVEAL - 1] else 0
    track = netease_music.prepared(track_id, ctx) if track_id and track_id.startswith("netease:") else None
    if not track or track.sec < total:
        track = await _netease_track(ctx, query, total, exact_id=CAREFREE if shortcut and style == "auto" else None)
    offset = 0.0
    if track:
        from light.silk_road import MUSIC_REVEAL_SEC, TEMPLATE as SILK_ROAD

        # Line up the reveal (or first cut) by sliding the audio, keeping picture and dialogue timing fixed.
        anchor = MUSIC_REVEAL_SEC if template == SILK_ROAD else (spans[1][0] if len(spans) > 1 else 0)
        if not shortcut:
            offset = round(next((beat - anchor for beat in track.beats if anchor <= beat <= anchor + 1
                                 and beat - anchor + total <= track.sec), 0.0), 4)
        credit = track.credit()
        if track.id == CAREFREE:
            credit.update(license_url="https://creativecommons.org/licenses/by/4.0/",
                          attribution="Carefree — Kevin MacLeod (incompetech.com)")
        source, sec = "netease", track.sec
    else:
        if original_bed and style == "auto":
            return timeline, {**data, "music_source": "template"}
        backup = next((item for item in bgm_library.snapshot() if item.tag == tag and item.sec >= total), None)
        if not backup:
            return edit, {**data, "music_source": "unavailable"}
        credit = {"track_id": backup.id, "attribution": backup.name}
        source, sec = "ims", backup.sec

    volume = 0.12 if any(shot.volume > 0 for shot in edit.shots) or edit.audio else 0.36
    selection = music_catalog.MusicSelection(track_id=credit["track_id"], in_sec=offset, volume=volume)
    bed = music_catalog.music_audio(selection, edit, sec=sec, track=track, host=host)
    if start:
        bed = bed.model_copy(update={"from_sec": start, "volume": 0.32, "duck_volume": 0.13})
    return edit.model_copy(update={"audio": [*edit.audio, bed]}), {
        **data, "music_source": source, "edit_timeline": edit.model_dump(mode="json"),
        "soundtrack": {**credit, "in_sec": bed.in_sec, "beat_cuts": []},
    }
