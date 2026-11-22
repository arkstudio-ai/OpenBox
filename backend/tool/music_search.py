"""Music search and source preparation for the editing Agent."""
from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

from tool.tool import ToolContext, ToolResult, define_tool
from video import bgm_library, music_catalog, netease_music


class MusicSearchArgs(BaseModel):
    query: str = Field(default="", max_length=100)
    prepare_track_id: str | None = Field(default=None, pattern=r"^netease:[0-9]+$")
    moods: list[str] = Field(default_factory=list, max_length=8)
    duration_sec: float = Field(default=15, gt=0, le=600)
    exclude_track_ids: list[str] = Field(default_factory=list, max_length=20)


async def search(args: MusicSearchArgs, _ctx: ToolContext) -> ToolResult:
    if netease_music.api_base():
        try:
            if args.prepare_track_id:
                track = await netease_music.prepare(args.prepare_track_id, args.duration_sec, _ctx)
                facts = {
                    "track_id": track.id, "title": track.title, "artist": track.artist,
                    "duration_sec": track.sec, "bpm": track.bpm, "beat_seconds": track.beats[:24],
                    "default_in_sec": track.start_sec, "ready_for_ims": True,
                }
                return ToolResult(title="NetEase audio ready", output=json.dumps(facts, ensure_ascii=False),
                                  metadata=facts)
            query = args.query.strip() or " ".join(args.moods).strip()
            if not query:
                return ToolResult(title="Enter search keywords", output="Pass query with your own search keywords.")
            choices = await netease_music.search(query, args.duration_sec, set(args.exclude_track_ids), _ctx)
            return ToolResult(title="Live NetEase search", output=json.dumps(choices, ensure_ascii=False),
                              metadata={"query": query, "tracks": choices})
        except ValueError as exc:
            return ToolResult(title="NetEase track unavailable", output=str(exc))
        except (httpx.HTTPError, TimeoutError, OSError):
            return ToolResult(title="NetEase request failed",
                              output="The live search or audio request failed. Refine the query or try another result.")
    if args.prepare_track_id:
        return ToolResult(title="Live NetEase preparation is unavailable", output="Use a track from the local catalogue.")
    moods = {mood.strip().casefold() for mood in args.moods}
    excluded = set(args.exclude_track_ids)
    curated = [
        item for item in music_catalog.catalogue()
        if item.id not in excluded and item.sec - item.start_sec >= args.duration_sec
    ]
    curated.sort(key=lambda item: -len(moods.intersection(m.casefold() for m in item.moods)))
    choices = [
        {"track_id": item.id, "title": item.title, "artist": item.artist, "moods": item.moods,
         "source": "NetEase", "duration_sec": item.sec, "bpm": item.bpm,
         "beat_seconds": item.beats[:16], "default_in_sec": item.start_sec}
        for item in curated[:5]
    ]
    if not choices:
        fallback = [t for t in await bgm_library.tracks() if t.sec >= args.duration_sec and t.id not in excluded]
        fallback.sort(key=lambda t: t.label.casefold() not in moods and t.tag not in moods)
        choices = [{"track_id": t.id, "title": t.name, "moods": [t.label],
                    "source": "IMS", "duration_sec": t.sec} for t in fallback[:5]]
    return ToolResult(title="Music choices", output=json.dumps(choices, ensure_ascii=False),
                      metadata={"tracks": choices})


music_search_tool = define_tool(
    "music_search",
    description=(
        "Find eligible instrumental tracks to score a real-footage edit, by moods such as 轻快, 美食, "
        "舒缓, 动感 or 俏皮. Returns curated NetEase tracks with measured beats, or IMS tracks "
        "when the curated library has no eligible track. Pass an actual returned track_id in "
        "video_compose's music argument. Use exclude_track_ids when changing the current music. "
        "For live NetEase, pass query with your own keywords. When you select a live result, call again with "
        "prepare_track_id to fetch the audio and detect beats, then compose with the prepared track ID."
    ),
    parameters=MusicSearchArgs,
    execute=search,
    sandbox_required=False,
    parallel_safe=True,
)
