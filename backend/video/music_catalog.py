"""Locally curated, IMS-readable tracks. Ingested audio and measurements are immutable."""
from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from core.log import create_logger
from video.timeline import AudioClip, Timeline

log = create_logger("video.music_catalog")


class MusicTrack(BaseModel):
    id: str = Field(pattern=r"^netease:[0-9]+$")
    title: str
    artist: str
    moods: list[str]
    sec: float = Field(gt=0)
    bpm: float | None = Field(gt=0)
    beats: list[float]
    oss_key: str = Field(pattern=r"^music/catalog/[0-9]+/[a-f0-9]{64}\.mp3$")
    source_url: str
    license_url: str
    attribution: str

    @field_validator("beats")
    @classmethod
    def _in_order(cls, beats: list[float]) -> list[float]:
        if any(beat < 0 for beat in beats) or any(a >= b for a, b in zip(beats, beats[1:])):
            raise ValueError("beat times must be nonnegative and in order")
        return beats

    @property
    def start_sec(self) -> float:
        return self.beats[0] if self.beats else 0

    def credit(self) -> dict[str, str]:
        return {"track_id": self.id, "attribution": self.attribution,
                "source_url": self.source_url, "license_url": self.license_url}


def catalogue() -> list[MusicTrack]:
    path = os.environ.get("OPENBOX_MUSIC_CATALOG", "")
    if not path:
        return []
    try:
        return [MusicTrack.model_validate(row) for row in json.loads(Path(path).read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError, ValidationError):
        log.warning("Unable to read the configured music catalogue")
        return []


class MusicSelection(BaseModel):
    track_id: str = Field(pattern=r"^(?:netease:[0-9]+|icepublic-[0-9A-Za-z]+)$")
    cut_after: list[int] = Field(default_factory=list, max_length=16)
    in_sec: float | None = Field(default=None, ge=0)
    volume: float = Field(default=0.28, gt=0, le=1)


def snap_cuts(timeline: Timeline, track: MusicTrack, selection: MusicSelection,
              lengths: dict[str, float]) -> tuple[Timeline, list[dict]]:
    """Move specified hard cuts to nearby beats by exchanging time across the boundary."""
    if not track.beats:
        return timeline, []
    shots = list(timeline.shots)
    spans = timeline.shot_spans()
    music_in = track.start_sec if selection.in_sec is None else selection.in_sec
    snapped = []
    for index in sorted(set(selection.cut_after)):
        if not 1 <= index < len(shots):
            raise ValueError("cut_after uses one-based shot numbers before a cut")
        left, right = shots[index - 1:index + 1]
        if (spans[index] is None or left.duration_sec is None or right.duration_sec is None
                or left.volume or right.volume or left.transition_out or right.transition_out):
            continue  # Protect original speech and transitions.
        boundary = spans[index][0]
        target = min(track.beats, key=lambda beat: abs(beat - (boundary + music_in))) - music_in
        delta = round(target - boundary, 4)
        if abs(delta) > 0.22 or any(c.from_sec < max(boundary, target) and c.to_sec > min(boundary, target)
                                    for c in timeline.captions):
            continue
        left_duration = round(left.duration_sec + delta * left.speed, 4)
        right_duration = round(right.duration_sec - delta * right.speed, 4)
        right_in = round(right.in_sec + delta * right.speed, 4)
        if (left_duration / left.speed < 0.24 or right_duration / right.speed < 0.24
                or right_in < 0
                or (delta > 0 and left.in_sec + left_duration > lengths.get(left.asset, 0))):
            continue
        shots[index - 1] = left.model_copy(update={"duration_sec": left_duration})
        shots[index] = right.model_copy(update={"in_sec": right_in, "duration_sec": right_duration})
        snapped.append({"after_shot": index, "before_sec": boundary, "at_sec": round(target, 4)})
    return timeline.model_copy(update={"shots": shots}), snapped


def music_audio(selection: MusicSelection, timeline: Timeline, *, sec: float,
                track: MusicTrack | None = None, host: str = "") -> AudioClip:
    total = timeline.duration_sec
    if total is None:
        raise ValueError("Music selection requires a duration for each shot")
    start = selection.in_sec if selection.in_sec is not None else (track.start_sec if track else 0)
    if start >= sec or sec - start < total:
        raise ValueError("Music must cover the finished timeline. Select a longer track or an earlier music start.")
    media = {"asset": f"https://{host}/{track.oss_key}"} if track else {"library_id": selection.track_id}
    return AudioClip(
        **media, to_sec=total, in_sec=start, source_sec=sec, volume=selection.volume,
        duck_under_captions=True, duck_volume=round(selection.volume * 0.38, 3),
        fade_in_sec=min(0.10, total / 4), fade_out_sec=min(0.60, total / 4),
    )
