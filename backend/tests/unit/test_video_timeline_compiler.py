"""OpenBox timeline → IMS Timeline.

Each rule here corresponds to a way IMS rendered the wrong thing in the
2026-09-09 spike (docs/VIDEO_RENDER_ENGINE_SELECTION.md §4.2). If a test
fails after an IMS behaviour change, update the rule *and* the doc.
"""
import json

import pytest
from pydantic import ValidationError

from video.ims_compiler import TimelineCompileError, compile_ims
from video.timeline import (
    Canvas, CaptionStyle, Cue, Motion, Shot, TextClip, TextStyle, Timeline, Transition, spoken_preset,
)

B = "https://bossip-media-sh.oss-cn-shanghai.aliyuncs.com/ims-spike"
OUT = f"{B}/out/final.mp4"


def _two_shots(**kw) -> Timeline:
    return Timeline(
        shots=[
            Shot(asset=f"{B}/shot1.mp4", duration_sec=5, transition_out=Transition(type="fade", seconds=0.5)),
            Shot(asset=f"{B}/shot2.mp4", duration_sec=5),
        ],
        **kw,
    )


# ── rule 1: explicit geometry ────────────────────────────────────────────────

def test_every_video_clip_carries_explicit_geometry_and_adapt_mode():
    job = compile_ims(_two_shots(), output_url=OUT)
    for clip in job.timeline["VideoTracks"][0]["VideoTrackClips"]:
        assert (clip["X"], clip["Y"], clip["Width"], clip["Height"]) == (0, 0, 720, 1280)
        assert clip["AdaptMode"] == "Cover"


def test_fit_maps_to_adapt_mode_and_contain_is_opt_in():
    tl = Timeline(shots=[Shot(asset=f"{B}/wide.mp4", duration_sec=5, fit="contain")], canvas=Canvas(width=1080, height=1920))
    clip = compile_ims(tl, output_url=OUT).timeline["VideoTracks"][0]["VideoTrackClips"][0]
    assert clip["AdaptMode"] == "Contain"
    assert (clip["Width"], clip["Height"]) == (1080, 1920)


def test_trim_becomes_in_out_in_source_seconds():
    tl = Timeline(shots=[Shot(asset=f"{B}/shot1.mp4", in_sec=1.25, duration_sec=2.5)])
    clip = compile_ims(tl, output_url=OUT).timeline["VideoTracks"][0]["VideoTrackClips"][0]
    assert clip["In"] == 1.25 and clip["Out"] == 3.75


# ── rule 3: transitions ──────────────────────────────────────────────────────

def test_transition_hangs_off_the_outgoing_clip_and_shortens_total():
    job = compile_ims(_two_shots(), output_url=OUT)
    first, second = job.timeline["VideoTracks"][0]["VideoTrackClips"]
    assert first["Effects"] == [{"Type": "Transition", "SubType": "fade", "Duration": 0.5}]
    assert "Effects" not in second
    assert job.duration_sec == 9.5


def test_transition_on_last_shot_is_dropped_with_a_warning():
    tl = Timeline(shots=[Shot(asset=f"{B}/shot1.mp4", duration_sec=5, transition_out=Transition())])
    job = compile_ims(tl, output_url=OUT)
    assert "Effects" not in job.timeline["VideoTracks"][0]["VideoTrackClips"][0]
    assert any("transition_out ignored" in w for w in job.warnings)


def test_transition_longer_than_a_clip_is_rejected_at_schema_level():
    with pytest.raises(ValidationError, match="not shorter"):
        Timeline(shots=[
            Shot(asset=f"{B}/a.mp4", duration_sec=1, transition_out=Transition(seconds=1)),
            Shot(asset=f"{B}/b.mp4", duration_sec=5),
        ])


# ── rule 2: text placement ───────────────────────────────────────────────────

def test_centered_text_is_topcenter_with_x_zero_and_wraps():
    tl = _two_shots(texts=[TextClip(text="标题", from_sec=0, to_sec=3, y=0.125, max_width=0.9)])
    clip = compile_ims(tl, output_url=OUT).timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]
    assert clip["Alignment"] == "TopCenter" and clip["X"] == 0 and clip["Y"] == 160
    assert clip["TextWidth"] == 0.9 and clip["AdaptMode"] == "AutoWrap"
    assert clip["Type"] == "Text" and clip["Content"] == "标题"


def test_left_text_uses_pixel_x_from_fraction():
    tl = _two_shots(texts=[TextClip(text="@手柄", from_sec=3, to_sec=9.5, align="left", x=40 / 720, y=930 / 1280)])
    clip = compile_ims(tl, output_url=OUT).timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]
    assert clip["Alignment"] == "TopLeft" and clip["X"] == 40 and clip["Y"] == 930


def test_right_alignment_compiles_but_warns_unverified():
    tl = _two_shots(texts=[TextClip(text="右", from_sec=0, to_sec=1, align="right", x=0.9, y=0.1)])
    job = compile_ims(tl, output_url=OUT)
    assert job.timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]["Alignment"] == "TopRight"
    assert any("TopRight" in w for w in job.warnings)


def test_captions_sit_one_line_above_bottom_margin_on_their_own_track():
    tl = _two_shots(captions=[Cue(from_sec=0.2, to_sec=2.4, text="第一句"), Cue(from_sec=2.6, to_sec=4.8, text="第二句")])
    tracks = compile_ims(tl, output_url=OUT).timeline["SubtitleTracks"]
    assert len(tracks) == 1
    clips = tracks[0]["SubtitleTrackClips"]
    assert [c["Content"] for c in clips] == ["第一句", "第二句"]
    # 1280 * (1 - 0.095) - 44 * 1.35 = 1098.6 → 1099
    assert all(c["Y"] == 1099 and c["Alignment"] == "TopCenter" and c["X"] == 0 for c in clips)
    assert all(c["TextWidth"] == 0.88 and c["AdaptMode"] == "AutoWrap" for c in clips)
    assert clips[0]["AaiMotionInEffect"] == "zoomin_in" and clips[0]["AaiMotionIn"] == 0.15


def test_style_and_motion_fields_are_emitted():
    text = TextClip(
        text="钩子", from_sec=0, to_sec=3, y=0.1,
        style=TextStyle(size=56, color="#ffd700", outline=2, outline_color="#000000"),
        motion=Motion(in_effect="slide_down_in", in_sec=0.5, out_effect="fade_out", out_sec=0.6),
    )
    clip = compile_ims(_two_shots(texts=[text]), output_url=OUT).timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]
    assert clip["Font"] == "AlibabaPuHuiTi" and clip["FontSize"] == 56
    assert clip["FontColor"] == "#FFD700" and clip["Outline"] == 2 and clip["OutlineColour"] == "#000000"
    assert clip["AaiMotionInEffect"] == "slide_down_in" and clip["AaiMotionIn"] == 0.5
    assert clip["AaiMotionOutEffect"] == "fade_out" and clip["AaiMotionOut"] == 0.6


def test_font_url_replaces_font_and_skips_catalog_check():
    style = TextStyle(font="NotAFont", font_url=f"{B}/fonts/x.ttf")
    clip = compile_ims(_two_shots(texts=[TextClip(text="x", from_sec=0, to_sec=1, y=0.1, style=style)]),
                       output_url=OUT).timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]
    assert clip["FontURL"].endswith("x.ttf") and "Font" not in clip


# ── rule 4: enum gates ───────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    dict(texts=[TextClip(text="x", from_sec=0, to_sec=1, y=0.1, motion=Motion(in_effect="explode_in"))]),
    dict(texts=[TextClip(text="x", from_sec=0, to_sec=1, y=0.1, style=TextStyle(font="Comic Sans"))]),
    dict(texts=[TextClip(text="x", from_sec=0, to_sec=1, y=0.1, style=TextStyle(color_style="CS9999-000001"))]),
])
def test_unknown_enum_values_fail_before_submission(bad):
    with pytest.raises(TimelineCompileError, match="unknown"):
        compile_ims(_two_shots(**bad), output_url=OUT)


def test_unknown_transition_fails():
    tl = Timeline(shots=[
        Shot(asset=f"{B}/a.mp4", duration_sec=5, transition_out=Transition(type="starwipe")),
        Shot(asset=f"{B}/b.mp4", duration_sec=5),
    ])
    with pytest.raises(TimelineCompileError, match="starwipe"):
        compile_ims(tl, output_url=OUT)


def test_documented_but_unverified_values_compile_with_a_warning():
    tl = _two_shots(texts=[TextClip(text="x", from_sec=0, to_sec=1, y=0.1,
                                    style=TextStyle(color_style="golden"), motion=Motion(in_effect="typewriter1_in"))])
    job = compile_ims(tl, output_url=OUT)
    clip = job.timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]
    assert clip["EffectColorStyle"] == "golden" and clip["AaiMotionInEffect"] == "typewriter1_in"
    assert sum("not yet verified" in w for w in job.warnings) == 2


def test_verified_values_produce_no_warnings():
    job = compile_ims(_two_shots(captions=[Cue(from_sec=0, to_sec=1, text="x")]), output_url=OUT)
    assert job.warnings == []


# ── rule 5: timing ───────────────────────────────────────────────────────────

def test_text_past_the_end_is_an_error_and_overrun_is_clamped():
    with pytest.raises(TimelineCompileError, match="ends at 9.5s"):
        compile_ims(_two_shots(texts=[TextClip(text="x", from_sec=10, to_sec=11, y=0.1)]), output_url=OUT)
    job = compile_ims(_two_shots(texts=[TextClip(text="x", from_sec=8, to_sec=12, y=0.1)]), output_url=OUT)
    assert job.timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]["TimelineOut"] == 9.5
    assert any("clamped" in w for w in job.warnings)


def test_overlapping_captions_are_rejected():
    tl = _two_shots(captions=[Cue(from_sec=0, to_sec=3, text="a"), Cue(from_sec=2, to_sec=4, text="b")])
    with pytest.raises(TimelineCompileError, match="overlap"):
        compile_ims(tl, output_url=OUT)


def test_open_ended_shot_disables_duration_checks():
    tl = Timeline(shots=[Shot(asset=f"{B}/a.mp4")], texts=[TextClip(text="x", from_sec=100, to_sec=101, y=0.1)])
    job = compile_ims(tl, output_url=OUT)
    assert job.duration_sec is None
    assert job.timeline["SubtitleTracks"][0]["SubtitleTrackClips"][0]["TimelineIn"] == 100


# ── assets, output, idempotency ──────────────────────────────────────────────

def test_oss_scheme_is_expanded_with_region_and_rejected_without():
    tl = Timeline(shots=[Shot(asset="oss://bossip-media-sh/ims-spike/shot1.mp4", duration_sec=5)])
    job = compile_ims(tl, output_url="oss://bossip-media-sh/out/final.mp4", oss_region="cn-shanghai")
    assert job.timeline["VideoTracks"][0]["VideoTrackClips"][0]["MediaURL"] == f"{B}/shot1.mp4"
    assert job.output_media_config["MediaURL"] == "https://bossip-media-sh.oss-cn-shanghai.aliyuncs.com/out/final.mp4"
    with pytest.raises(TimelineCompileError, match="oss_region"):
        compile_ims(tl, output_url=OUT)


@pytest.mark.parametrize("ref", [
    "https://imgur.com/x.mp4",
    "http://bossip-media-sh.oss-cn-shanghai.aliyuncs.com/x.mp4",
    "https://bossip-media-sh.oss-cn-shanghai.aliyuncs.com/",
    "/workspace/uploads/x.mp4",
])
def test_non_oss_assets_are_refused(ref):
    with pytest.raises(TimelineCompileError, match="never leave the account"):
        compile_ims(Timeline(shots=[Shot(asset=ref, duration_sec=5)]), output_url=OUT)


def test_output_media_config_matches_canvas():
    job = compile_ims(_two_shots(), output_url=OUT, bitrate_kbps=3000)
    assert job.output_media_config == {"MediaURL": OUT, "Width": 720, "Height": 1280, "Bitrate": 3000}


def test_client_token_is_stable_ascii_and_changes_with_content():
    a = compile_ims(_two_shots(), output_url=OUT)
    b = compile_ims(_two_shots(), output_url=OUT)
    c = compile_ims(_two_shots(captions=[Cue(from_sec=0, to_sec=1, text="x")]), output_url=OUT)
    assert a.client_token == b.client_token != c.client_token
    assert a.client_token.isascii() and len(a.client_token) <= 64


def test_cli_args_are_compact_json_strings():
    args = compile_ims(_two_shots(), output_url=OUT).as_cli_args()
    assert args["--output-media-target"] == "oss-object"
    assert json.loads(args["--timeline"])["VideoTracks"]
    assert "\n" not in args["--timeline"]


def test_all_problems_are_reported_together():
    tl = Timeline(
        shots=[Shot(asset="https://imgur.com/x.mp4", duration_sec=5)],
        texts=[TextClip(text="x", from_sec=0, to_sec=1, y=0.1, motion=Motion(in_effect="nope_in"))],
    )
    with pytest.raises(TimelineCompileError) as info:
        compile_ims(tl, output_url=OUT)
    assert len(info.value.problems) == 2


# ── the spoken preset reproduces the hand-written spike timeline ─────────────

def test_spoken_preset_matches_the_verified_spike_layout():
    tl = spoken_preset(
        shots=[Shot(asset=f"{B}/shot1.mp4", duration_sec=5, transition_out=Transition(type="fade", seconds=0.5)),
               Shot(asset=f"{B}/shot2.mp4", duration_sec=5)],
        cues=[Cue(from_sec=0.2, to_sec=2.4, text="装修最容易被忽略的一件事")],
        hook="一个 90% 的人都忽略的细节", handle="@开箱装修",
    )
    job = compile_ims(tl, output_url=OUT)
    assert job.warnings == []
    hook, handle, captions = job.timeline["SubtitleTracks"]
    h = hook["SubtitleTrackClips"][0]
    assert (h["Alignment"], h["X"], h["Y"], h["FontSize"], h["FontColor"]) == ("TopCenter", 0, 160, 56, "#FFD700")
    assert (h["TimelineIn"], h["TimelineOut"], h["AaiMotionInEffect"], h["AaiMotionOutEffect"]) == (0, 3, "slide_down_in", "fade_out")
    l = handle["SubtitleTrackClips"][0]
    assert (l["Alignment"], l["X"], l["Y"], l["TimelineIn"], l["TimelineOut"]) == ("TopLeft", 40, 930, 3, 9.5)
    assert captions["SubtitleTrackClips"][0]["Content"] == "装修最容易被忽略的一件事"


def test_timeline_rejects_unknown_fields_so_agents_cannot_smuggle_ims_keys():
    with pytest.raises(ValidationError):
        Timeline.model_validate({"shots": [{"asset": f"{B}/a.mp4", "AdaptMode": "Cover"}]})
