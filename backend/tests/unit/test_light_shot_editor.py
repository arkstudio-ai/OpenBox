from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from light import shot_editor


def test_small_wide_replacement_is_encoded_at_the_existing_clip_size():
    from PIL import Image

    original = BytesIO()
    Image.new("RGBA", (560, 220), "orange").save(original, format="PNG")
    with Image.open(BytesIO(shot_editor._matching_frame(original.getvalue(), (1470, 630)))) as result:
        assert (result.format, result.mode, result.size) == ("JPEG", "RGB", (1470, 630))


def _compose(identifier, asset, timeline, offset=0):
    return SimpleNamespace(id=identifier, output_asset_id=asset, kind="compose", status="completed",
                           created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset),
                           completed_at=None, idempotency_key="light:s:final" if not offset else "soundtrack",
                           request_data={"timeline": timeline, "duration_sec": 6})


def test_advanced_music_only_versions_inherit_the_editable_shots():
    form = {"run": {"wan_final_key": "light:s:final", "job_prefix": "light:s"}}
    base_timeline = {"shots": [{"asset": "asset_a"}, {"asset": "asset_b"}], "audio": [{"asset": "asset_music1"}]}
    base = _compose("base", "asset_base", base_timeline)
    new_audio = [{"asset": "asset_music2"}]
    edit = _compose("audio-edit", "asset_edit", {"shots": [
        {"asset": "asset_base", "volume": 0, "duration_sec": 6}], "audio": new_audio}, 1)
    master, version, timeline = shot_editor._assembly(form, [base, edit], ["asset_edit", "asset_base"])
    assert master is version is base
    assert timeline["shots"] == base_timeline["shots"]
    assert timeline["audio"] == new_audio
    edit.request_data["timeline"]["shots"][0]["in_sec"] = 1
    assert shot_editor._assembly(form, [base, edit], ["asset_edit"])[1] is None


def test_advanced_overlay_and_full_recomposition_keep_the_replaced_slot_in_history():
    def segment(asset_id, slot, message=None, offset=0):
        return SimpleNamespace(kind="segment", status="completed", output_asset_id=asset_id,
                               request_data={"shot": slot}, attached_message_id=message, completed_at=None,
                               created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset))

    first = segment("asset_first", 2)
    old = segment("asset_old", 1)
    retake = segment("asset_retake", 3, "retake-message", 1)
    base = _compose("base", "asset_base", {"shots": [
        {"asset": "asset_first", "duration_sec": 2}, {"asset": "asset_first", "duration_sec": 2},
        {"asset": "asset_old", "duration_sec": 2},
    ]})
    overlay = {"asset": "asset_with_lettering", "from_sec": 4, "to_sec": 6,
               "x": 0, "y": 0, "width": 1, "height": 1}
    repaired = _compose("repair", "asset_repaired", {"shots": [
        {"asset": "asset_base", "duration_sec": 6},
    ], "overlays": [overlay]}, 2)
    latest = _compose("recompose", "asset_latest", {"shots": [
        {"asset": "asset_repaired", "duration_sec": 6},
    ]}, 3)
    base.attached_message_id = None
    repaired.attached_message_id = "repair-message"
    latest.attached_message_id = "recompose-message"
    turns = {"retake-message": "user-repair", "repair-message": "user-repair", "recompose-message": "user-recompose"}
    jobs = [first, old, retake, latest, base, repaired]
    outputs = ["asset_latest", "asset_repaired", "asset_base"]

    sources = shot_editor._film_sources(jobs, outputs, {1, 2}, turns)
    assert sources == {
        "asset_base": {1: old, 2: first},
        "asset_repaired": {1: retake, 2: first},
        "asset_latest": {1: retake, 2: first},
    }
    latest.request_data["timeline"]["shots"][0]["duration_sec"] = 1
    assert shot_editor._film_sources(jobs, outputs, {1, 2}, turns)["asset_latest"] == {}
    turns["retake-message"] = "unrelated-turn"
    assert shot_editor._film_sources(jobs, outputs, {1, 2}, turns)["asset_repaired"] == {1: old, 2: first}


async def test_recomposition_replaces_each_occurrence_and_keeps_the_cut_timing(monkeypatch):
    from video import media_probe

    monkeypatch.setattr(media_probe, "ensure_meta", AsyncMock(return_value={"duration_sec": 5}))
    ctx = SimpleNamespace(user_id="owner")
    prior = SimpleNamespace(output_asset_id="asset_old")
    new = SimpleNamespace(output_asset_id="asset_new", request_data={"shot": 2})
    timeline = {
        "shots": [
            {"asset": "asset_keep", "duration_sec": 2},
            {"asset": "asset_old", "in_sec": 1, "duration_sec": 3, "volume": 0},
            {"asset": "asset_old", "in_sec": 7, "duration_sec": 3, "volume": 0},
        ],
        "audio": [{"asset": "asset_music", "to_sec": 8}],
    }
    rebuilt = await shot_editor._replace_sections(ctx, timeline, {2: prior}, [new])
    first, start, later = rebuilt["shots"]
    assert first["asset"] == "asset_keep"
    assert start["asset"] == later["asset"] == "asset_new"
    assert later["in_sec"] + later["duration_sec"] == pytest.approx(5)
    assert start["duration_sec"] / start["speed"] == pytest.approx(3)
    assert later["duration_sec"] / later["speed"] == pytest.approx(3)
    assert rebuilt["audio"][0]["asset"] == "asset_music"


async def test_silent_shortcut_retake_removes_only_the_replaced_dialogue_caption(monkeypatch):
    from video import media_probe

    monkeypatch.setattr(media_probe, "ensure_meta", AsyncMock(return_value={"duration_sec": 5}))
    original = SimpleNamespace(output_asset_id="asset_old", request_data={"spoken_line": "来，趁热。"})
    replacement = SimpleNamespace(output_asset_id="asset_new", request_data={"shot": 2})
    timeline = {
        "shots": [{"asset": "asset_keep", "duration_sec": 5}, {"asset": "asset_old", "duration_sec": 5}],
        "captions": [{"text": "来，趁热。", "from_sec": 2, "to_sec": 3},
                     {"text": "来，趁热。", "from_sec": 7, "to_sec": 8},
                     {"text": "孜然烤羊腿", "from_sec": 8, "to_sec": 9}],
        "audio": [{"asset": "asset_music", "from_sec": 5, "to_sec": 10, "duck_under_captions": True}],
    }
    rebuilt = await shot_editor._replace_sections(SimpleNamespace(user_id="owner"), timeline,
                                                    {2: original}, [replacement], drop_removed_dialogue=True)
    assert [(c["text"], c["from_sec"]) for c in rebuilt["captions"]] == [("来，趁热。", 2), ("孜然烤羊腿", 8)]
    assert rebuilt["audio"][0]["from_sec"] == 5
    assert rebuilt["shots"][1]["asset"] == "asset_new"


async def test_tail_edit_sends_two_native_endpoints_instead_of_the_motion_reference(monkeypatch):
    from tool import video_providers
    from video import media_probe

    monkeypatch.setattr(media_probe, "ensure_meta", AsyncMock(return_value={"duration_sec": 8.33}))
    monkeypatch.setattr(video_providers, "declared_model", lambda *args: SimpleNamespace(supports_first_last_frame=True))
    capture = AsyncMock(return_value="asset_head")
    monkeypatch.setattr(shot_editor, "_snapshot", capture)
    clip = SimpleNamespace(model="wan3.0-video", output_asset_id="asset_clip", prompt="原版",
                           request_data={"shot": 2, "roles": ["reference_video"], "input_asset_ids": ["asset_ref"],
                                         "duration": -1, "ratio": "21:9", "resolution": "720p", "generate_audio": False})
    body = shot_editor.ShotAction(action="retake", revision=1, request_id="a" * 16, last_frame="asset_new_tail", prompt="新版")
    args = await shot_editor._retake_args(SimpleNamespace(user_id="owner"), clip, body, "retake")
    assert [(item.asset_id, item.role) for item in args.input_assets] == [
        ("asset_head", "first_frame"), ("asset_new_tail", "last_frame"),
    ]
    assert args.shot == 2
    assert args.duration == 9
    assert capture.await_args.args[2] == "first_frame"


def test_one_title_can_be_removed_and_restored_without_changing_audio_or_video():
    from light import silk_road
    from light.silk_road_lettering import CHAPTER_TIMES

    layers = [
        {"from_sec": start, "to_sec": end, "asset": f"asset_{index}"}
        for index, (start, end) in enumerate(CHAPTER_TIMES, 1)
    ]
    timeline = {"shots": [{"asset": "asset_clip"}], "audio": [{"asset": "asset_song"}], "overlays": layers}
    base = _compose("base", "asset_master", timeline)
    form = {"template": silk_road.TEMPLATE, "shot_title_edits": {"base": {"5": True}}}
    chapter_layers = shot_editor._title_layers(form, base)
    changes = shot_editor._title_changes(form, base, timeline, chapter_layers)
    assert changes == {5: True}
    revised = shot_editor._apply_title_changes(timeline, chapter_layers, changes)
    assert revised["overlays"] == layers[:4] + layers[5:]
    assert revised["audio"] == timeline["audio"]
    assert revised["shots"] == timeline["shots"]
    assert timeline["overlays"] == layers

    form["shot_title_edits"]["base"]["5"] = False
    assert shot_editor._title_changes(form, base, revised, chapter_layers) == {5: False}
    restored = shot_editor._apply_title_changes(revised, chapter_layers, {5: False})
    assert restored["overlays"] == layers


def test_clean_frame_references_use_cast_and_current_location():
    from light import shot_frame_edits, silk_road

    form = {
        "template": silk_road.TEMPLATE,
        "asset_ids": ["asset_cast", "asset_station", "asset_lamb", "asset_office"],
        "remake_adaptation": {"world": {
            "cast_material": 1,
            "product_material": 3,
            "takes": [{"location_material": 4}] * 3 + [{"location_material": 2}] * 6,
        }},
    }
    assert shot_frame_edits.references(form, 4) == ["asset_cast", "asset_station"]
    assert shot_frame_edits.references(form, 6) == ["asset_cast", "asset_station", "asset_lamb"]
    assert shot_frame_edits._dimensions({"width": 1680, "height": 660}) == "1536x608"


async def test_frame_status_can_recover_from_a_saved_image():
    from light import shot_frame_edits

    first = {"filename": "first.png", "status": "generating", "until": "2026-01-01"}
    last = {"filename": "last.png", "status": "generating", "until": "2026-12-31"}
    row = SimpleNamespace(
        id="session", user_id="owner", workspace_id="workspace",
        light_production={"shot_frame_drafts": {"version": {"first_frame": first, "last_frame": last}}},
    )
    ready = SimpleNamespace(name="first.png")
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [ready])))
    projected = await shot_frame_edits.read_drafts(db, row, now="2026-11-20")
    assert projected["version"]["first_frame"] == (first, ready, "ready")
    assert projected["version"]["last_frame"] == (last, None, "generating")


async def test_recovered_guided_retake_does_not_start_an_agent_turn(monkeypatch):
    from video import job_recovery

    wake = AsyncMock()
    monkeypatch.setattr(job_recovery, "_continuation_queued", wake)
    job = SimpleNamespace(session_id="session", user_id="owner",
                          idempotency_key=f"{shot_editor.KEY_PREFIX}master:retake:4:request")
    assert await job_recovery.resume_conversation(job) is False
    wake.assert_not_awaited()
