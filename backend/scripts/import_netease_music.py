"""Stage explicitly reviewed NetEase recordings in the configured OSS bucket.

Input is a JSON list of objects with id (NetEase numeric ID), title, artist,
moods, bpm (from the author), source_url (the licensed recording), and
license_url. Run once from backend with an existing local NetEase API.
"""
from __future__ import annotations

import argparse
import asyncio
from array import array
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile

from dotenv import load_dotenv
import httpx

from core.oss import get_oss
from video.music_catalog import MusicTrack


def measure(path: Path, bpm: float) -> tuple[float, list[float]]:
    json_probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path),
    ])
    duration = float(json.loads(json_probe)["format"]["duration"])
    samples = array("f")
    samples.frombytes(subprocess.check_output([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", "45",
        "-ac", "1", "-ar", "4000", "-af", "highpass=f=55,lowpass=f=1200", "-f", "f32le", "-",
    ]))
    envelope = [
        math.log1p(250 * math.sqrt(sum(x * x for x in samples[n:n + 20]) / 20))
        for n in range(0, len(samples) - 20, 20)
    ]
    onsets = [0.0] * 4 + [
        max(0.0, envelope[n] - sum(envelope[n - 4:n]) / 4)
        for n in range(4, len(envelope))
    ]
    period = 60 / bpm
    step = 0.005

    def score(phase: float) -> float:
        indices = [round((phase + n * period) / step) for n in range(1, int(len(onsets) * step / period) - 1)]
        return sum(max(onsets[i - 2:i + 3]) for i in indices)

    phase = max((n * step for n in range(round(period / step))), key=score)
    return duration, [round(phase + n * period, 4) for n in range(math.floor((duration - phase) / period))]


async def stage(entry: dict, folder: Path, http: httpx.AsyncClient, api: str) -> dict:
    track_id = int(entry["id"])
    search = await http.get(f"{api}/cloudsearch", params={
        "keywords": f'{entry["title"]} {entry["artist"]}', "type": 1, "limit": 20,
    })
    search.raise_for_status()
    songs = search.json().get("result", {}).get("songs", [])
    if not any(song.get("id") == track_id and song.get("name") == entry["title"]
               and any(artist.get("name") == entry["artist"] for artist in song.get("ar", []))
               for song in songs):
        raise ValueError(f"Search did not return the reviewed recording {track_id} ({entry['title']})")
    print(f"NetEase search matched {entry['title']} ({track_id})", flush=True)
    response = await http.get(f"{api}/song/url/v1", params={"id": track_id, "level": "standard", "ua": "pc"})
    response.raise_for_status()
    track = response.json()["data"][0]
    if not track.get("url") or track.get("freeTrialInfo") or track.get("type") != "mp3":
        raise ValueError(f"{entry['title']} does not have a complete MP3 available")
    path = folder / f"{track_id}.mp3"
    async with http.stream("GET", track["url"].replace("http://", "https://", 1)) as response:
        response.raise_for_status()
        with path.open("wb") as stream:
            async for chunk in response.aiter_bytes():
                stream.write(chunk)
    duration, beats = await asyncio.to_thread(measure, path, entry["bpm"])
    if duration < 30 or abs(duration - track["time"] / 1000) > 2:
        raise ValueError(f"{entry['title']} duration differs from the complete recording")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    key = f"music/catalog/{track_id}/{digest}.mp3"
    row = MusicTrack.model_validate({
        **entry, "id": f"netease:{track_id}", "sec": round(duration, 3), "beats": beats, "oss_key": key,
        "attribution": f'{entry["title"]} — {entry["artist"]} (incompetech.com), CC BY 4.0',
    })
    oss = get_oss()
    if not await oss.head_object_info(key):
        await oss.put_object_file(key, path, content_type="audio/mpeg", forbid_overwrite=True)
    print(f"{row.id} {row.title} {row.bpm:g} BPM, {row.sec:g}s", flush=True)
    return row.model_dump(mode="json")


async def run(opts):
    with tempfile.TemporaryDirectory(prefix="openbox-music-") as temporary:
        entries = json.loads(Path(opts.reviewed).read_text())
        async with httpx.AsyncClient(timeout=35, follow_redirects=True) as http:
            tracks = await asyncio.gather(*(stage(item, Path(temporary), http, opts.api) for item in entries))
    output = Path(opts.output)
    staged = output.with_suffix(".tmp")
    staged.write_text(json.dumps(tracks, ensure_ascii=False, indent=2) + "\n")
    staged.replace(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reviewed")
    parser.add_argument("output")
    parser.add_argument("--api", default="http://127.0.0.1:8791")
    parser.add_argument("--env", default=".env")
    options = parser.parse_args()
    load_dotenv(options.env)
    asyncio.run(run(options))
