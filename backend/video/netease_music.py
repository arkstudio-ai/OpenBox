"""Live NetEase search and source preparation for a configured internal API."""
from __future__ import annotations

import asyncio
from array import array
from collections import OrderedDict
import hashlib
import math
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

import httpx

from video.music_catalog import MusicTrack

# The live QA backend uses a single serving process. Bind the offered and
# prepared records to the current owner and conversation.
_sessions: OrderedDict[tuple[str, str], tuple[dict, dict]] = OrderedDict()


def api_base() -> str:
    return os.environ.get("OPENBOX_NETEASE_API", "").rstrip("/")


def _params(**values):
    if protocol := os.environ.get("OPENBOX_NETEASE_API_CRYPTO"):
        values["crypto"] = protocol
    return values


def _session(ctx):
    key = (ctx.user_id, ctx.session_id)
    if key not in _sessions:
        _sessions[key] = ({}, {})
        if len(_sessions) > 64:
            _sessions.popitem(last=False)
    _sessions.move_to_end(key)
    return _sessions[key]


def prepared(track_id: str, ctx) -> MusicTrack | None:
    return _sessions.get((ctx.user_id, ctx.session_id), ({}, {}))[1].get(track_id)


async def search(query: str, duration: float, excluded: set[str], ctx) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as http:
        response = await http.get(f"{api_base()}/cloudsearch",
                                  params=_params(keywords=query, type=1, limit=18))
        response.raise_for_status()
    offered, _ = _session(ctx)
    choices = []
    for song in (response.json().get("result") or {}).get("songs") or []:
        number = song.get("id")
        sec = (song.get("dt") or 0) / 1000
        track_id = f"netease:{number}"
        if not isinstance(number, int) or track_id in excluded or sec < duration:
            continue
        artist = " / ".join(a["name"] for a in song.get("ar") or [] if a.get("name"))
        choice = {
            "track_id": track_id, "title": str(song.get("name") or "")[:140],
            "artist": artist[:140], "album": str((song.get("al") or {}).get("name") or "")[:120],
            "duration_sec": round(sec, 2), "source": "NetEase live",
        }
        offered[track_id] = choice
        choices.append(choice)
        if len(choices) == 10:
            break
    return choices


def _beat_grid(path: Path) -> tuple[float, float | None, list[float]]:
    duration = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
    ], timeout=12))
    samples = array("f")
    samples.frombytes(subprocess.check_output([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", "54", "-ac", "1",
        "-ar", "4000", "-af", "highpass=f=55,lowpass=f=850", "-f", "f32le", "-",
    ], timeout=20))
    step = 0.01
    envelope = [
        math.log1p(200 * math.sqrt(sum(x*x for x in samples[n:n + 40]) / 40))
        for n in range(0, len(samples) - 40, 40)
    ]
    onsets = [0.0] * 3 + [max(0, envelope[n] - sum(envelope[n-3:n]) / 3)
                           for n in range(3, len(envelope))]
    if len(onsets) < 500 or not max(onsets):
        return duration, None, []
    mean = sum(onsets) / len(onsets)
    centered = [value - mean for value in onsets]
    energy = sum(value * value for value in centered)
    correlations = {lag: sum(x * y for x, y in zip(centered, centered[lag:])) / energy
                    for lag in range(33, 86)}
    lag = max(correlations, key=correlations.get)
    if correlations[lag] < 0.12:
        return duration, None, []

    def score(period: float, phase: float) -> float:
        indices = [round((phase + n*period)/step) for n in range(1, int(len(onsets)*step/period) - 1)]
        return sum(max(onsets[i-1:i+2]) for i in indices) / max(1, len(indices))

    periods = [(lag + offset / 10) * step for offset in range(-8, 9)]
    _, period, phase = max((score(period, n*step), period, n*step)
                           for period in periods for n in range(round(period/step)))
    end = min(duration, len(onsets)*step)
    beats = [round(phase + n*period, 4) for n in range(int((end-phase)/period))]
    return duration, round(60 / period, 1), beats


async def prepare(track_id: str, minimum_sec: float, ctx) -> MusicTrack:
    offered, ready = _session(ctx)
    if track_id in ready:
        return ready[track_id]
    if track_id not in offered:
        raise ValueError("Choose a track returned by live keyword search in this conversation.")
    choice = offered[track_id]
    number = track_id.split(":", 1)[1]
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        response = await client.get(f"{api_base()}/song/url/v1",
                                    params=_params(id=number, level="standard", ua="pc"))
        response.raise_for_status()
        entries = response.json().get("data") or []
        source = entries[0] if entries else {}
        if not source.get("url") or source.get("freeTrialInfo") or source.get("type") != "mp3":
            raise ValueError("NetEase did not return a full MP3 for this track. Choose another search result.")
        parts = urlsplit(source["url"])
        if (parts.scheme not in {"https", "http"} or not (parts.hostname or "").endswith(".music.126.net")
                or parts.username or parts.port not in {None, 443}):
            raise ValueError("NetEase returned an unsupported audio address. Choose another track.")
        with tempfile.TemporaryDirectory(prefix="netease-live-") as tmp:
            path = Path(tmp) / "audio.mp3"
            digest = hashlib.sha256()
            size = 0
            async with asyncio.timeout(35), client.stream("GET", parts._replace(scheme="https").geturl()) as download:
                download.raise_for_status()
                with path.open("wb") as output:
                    async for chunk in download.aiter_bytes():
                        size += len(chunk)
                        if size > 32 * 1024 * 1024:
                            raise ValueError("This recording exceeds the source size limit. Choose another track.")
                        digest.update(chunk)
                        output.write(chunk)
            sec, bpm, beats = await asyncio.to_thread(_beat_grid, path)
            if sec < minimum_sec or sec < choice["duration_sec"] - 3:
                raise ValueError("The audio returned is shorter than the full recording or your edit.")
            from core.oss import get_oss

            key = f"music/catalog/{number}/{digest.hexdigest()}.mp3"
            oss = get_oss()
            if not await oss.head_object_info(key):
                await oss.put_object_file(key, path, content_type="audio/mpeg")
    result = MusicTrack(
        id=track_id, title=choice["title"], artist=choice["artist"], moods=[], sec=sec, bpm=bpm,
        beats=beats, oss_key=key, source_url=f"https://music.163.com/song?id={number}", license_url="",
        attribution=f"{choice['title']} — {choice['artist']}",
    )
    ready[track_id] = result
    return result
