"""The music under a 你拍我剪 cut: IMS's public library, referenced by MediaId.

The tracks are Aliyun's own (``icepublic-…``); a timeline names one with
``audio[].library_id`` and IMS fetches it while rendering, so nothing is
copied into our bucket. The list is read from IMS on first use and kept for a
day; when IMS cannot be asked, the snapshot next to this file (45 tracks,
2026-11-03) stands in, so a cut never fails for want of a catalogue.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from core.log import create_logger

log = create_logger("video.bgm")

SNAPSHOT = Path(__file__).with_name("bgm_library.json")
CACHE_SECONDS = 24 * 3600
#: The library's tags, in the owner's words. Order is the order the agent sees them.
TAG_LABELS = {
    "bgm-cuisine": "美食", "bgm-relaxing": "轻松", "bgm-upbeat": "欢快", "bgm-dynamic": "动感",
    "bgm-chinese-style": "国风", "bgm-quirky": "俏皮", "bgm-beauty": "时尚", "bgm-romantic": "浪漫",
}


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    tag: str
    sec: float

    @property
    def label(self) -> str:
        return TAG_LABELS.get(self.tag, self.tag)


_cache: tuple[float, list[Track]] | None = None


def snapshot() -> list[Track]:
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return [Track(id=t["id"], name=t["name"], tag=t["tag"], sec=float(t["sec"])) for t in data["tracks"]]


def _from_media_infos(items: list[dict]) -> list[Track]:
    tracks = []
    for item in items:
        basic = item.get("MediaBasicInfo") or {}
        files = item.get("FileInfoList") or [{}]
        info = (files[0] or {}).get("FileBasicInfo") or {}
        media_id = str(item.get("MediaId") or basic.get("MediaId") or "")
        try:
            sec = float(info.get("Duration") or 0)
        except (TypeError, ValueError):
            sec = 0.0
        if not media_id.startswith("icepublic-") or sec <= 0 or str(basic.get("Status") or "Normal") != "Normal":
            continue
        name = str(basic.get("Title") or info.get("FileName") or media_id).rsplit(".", 1)[0]
        tracks.append(Track(id=media_id, name=name, tag=str(basic.get("MediaTags") or ""), sec=round(sec, 2)))
    order = list(TAG_LABELS)
    return sorted(tracks, key=lambda t: (order.index(t.tag) if t.tag in order else len(order), t.name))


async def tracks() -> list[Track]:
    """The library, from IMS at most once a day, else the snapshot."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    from video.ims_client import list_public_bgm

    try:
        listed = _from_media_infos(await list_public_bgm())
    except Exception as exc:  # the snapshot is good enough to cut with
        log.info(f"music library not listed from IMS ({type(exc).__name__}); using the snapshot")
        listed = []
    found = listed or snapshot()
    _cache = (now, found)
    return found


async def track(media_id: str) -> Track | None:
    return next((t for t in await tracks() if t.id == media_id), None)


def catalogue(found: list[Track]) -> str:
    """One line per tag for the agent: ``美食: Delicious Notes 80s icepublic-…; …``."""
    lines = []
    for tag, label in TAG_LABELS.items():
        group = [t for t in found if t.tag == tag]
        if group:
            lines.append(f"{label}: " + "; ".join(f"{t.name} {t.sec:g}s {t.id}" for t in group))
    return "\n".join(lines)
