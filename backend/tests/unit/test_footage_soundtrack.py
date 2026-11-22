from types import SimpleNamespace

import pytest

from tool import music_search, video_compose
from video import music_catalog
from video.ims_compiler import compile_ims
from video.timeline import Timeline

HOST = "test.oss-cn-shanghai.aliyuncs.com"


def track(number: int, mood: str) -> music_catalog.MusicTrack:
    return music_catalog.MusicTrack(
        id=f"netease:{number}", title=f"Track {number}", artist="Test",
        sec=40, bpm=120, beats=[0.1 + n / 2 for n in range(70)], moods=[mood],
        oss_key=f"music/catalog/{number}/{'a' * 64}.mp3",
        attribution="Artist", license_url="https://creativecommons.org/licenses/by/4.0/",
        source_url="https://example.com/track",
    )


def test_beat_nudge_exchanges_trim_without_changing_total_or_spoken_shots():
    cut = Timeline.model_validate({
        "shots": [
            {"asset": "a", "in_sec": 0.4, "duration_sec": 1.88, "volume": 0},
            {"asset": "b", "in_sec": 0.6, "duration_sec": 1.05, "volume": 0},
            {"asset": "c", "duration_sec": 2, "volume": 1},
        ],
        "captions": [{"text": "欢迎", "from_sec": 3.1, "to_sec": 4}],
    })
    music = track(1, "轻快")
    out, moved = music_catalog.snap_cuts(
        cut, music, music_catalog.MusicSelection(track_id=music.id, cut_after=[1, 2]), {"a": 4, "b": 3},
    )
    assert moved == [{"after_shot": 1, "before_sec": 1.88, "at_sec": 2.0}]
    assert out.shots[1].in_sec == 0.72
    assert out.shots[2] == cut.shots[2]
    assert out.duration_sec == cut.duration_sec
    assert out.captions == cut.captions


@pytest.mark.asyncio
async def test_search_can_exclude_the_current_track(monkeypatch):
    monkeypatch.setattr(music_catalog, "catalogue", lambda: [track(1, "轻快"), track(2, "俏皮")])
    result = await music_search.search(music_search.MusicSearchArgs(
        moods=["俏皮"], exclude_track_ids=["netease:1"],
    ), None)
    assert [t["track_id"] for t in result.metadata["tracks"]] == ["netease:2"]


@pytest.mark.asyncio
async def test_replacement_reuses_original_visuals_captions_and_non_music_audio(monkeypatch):
    base_url = f"https://{HOST}/assets/test-user/"
    edit = {
        "shots": [{"asset": base_url + "a.mp4", "in_sec": 0.7, "duration_sec": 3.4}],
        "captions": [{"text": "欢迎", "from_sec": 0.12, "to_sec": 1.2}],
        "audio": [{"asset": base_url + "original.mp4", "to_sec": 3, "volume": 0.65}],
    }
    ctx = SimpleNamespace(user_id="test-user", session_id="session")
    oss = SimpleNamespace(bucket="test", host=HOST)
    monkeypatch.setattr(music_catalog, "catalogue", lambda: [track(1, "轻快"), track(2, "俏皮")])
    first, data = await video_compose._prepare_timeline(
        video_compose.VideoComposeArgs(action="submit", timeline=edit, idempotency_key="old",
                                       music={"track_id": "netease:1"}), ctx, oss,
    )

    async def lookup(job_id, passed_ctx):
        assert (job_id, passed_ctx) == ("completed-old", ctx)
        return SimpleNamespace(id=job_id, request_data={"timeline": first.model_dump(mode="json"), **data})

    monkeypatch.setattr(video_compose, "_replacement_source", lookup)
    second, next_data = await video_compose._prepare_timeline(
        video_compose.VideoComposeArgs(action="replace_music", job_id="completed-old", idempotency_key="new",
                                       music={"track_id": "netease:2"}), ctx, oss,
    )
    first_ims = compile_ims(first, output_url=base_url + "old.mp4").timeline
    second_ims = compile_ims(second, output_url=base_url + "new.mp4").timeline
    assert first_ims["VideoTracks"] == second_ims["VideoTracks"]
    assert first_ims["SubtitleTracks"] == second_ims["SubtitleTracks"]
    assert first_ims["AudioTracks"][0] == second_ims["AudioTracks"][0]
    assert first_ims["AudioTracks"][1] != second_ims["AudioTracks"][1]
    assert next_data["soundtrack"]["replaces_job_id"] == "completed-old"
