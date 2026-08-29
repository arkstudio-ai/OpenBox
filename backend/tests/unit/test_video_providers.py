"""Multi-channel video routing: model canonicalization, payloads, statuses.

Pure functions, no DB. The edge cases here are the ones the reference system
(bossip) learned the hard way — the U+2160 Ⅰ spelling, the sd2 poll-id trap,
silent reference discarding, wan3's wider duration window.
"""
from types import SimpleNamespace

import pytest

from tool.video_providers import (
    SD2_MODELS,
    VideoRoute,
    WAN3_MODEL_TYPE,
    auth_header,
    build_payload,
    canonicalize_sd2_model_name,
    canonicalize_wan3_model_name,
    clamp_wan3_duration,
    compute_prompt_hash,
    extract_task_id,
    is_sd2_model,
    is_wan3_model,
    map_wan3_resolution,
    normalize_state,
    resolve_route,
    result_video_url,
    sd2_native_resolution,
    validate_request,
)


def _route(channel="ark", model="doubao-seedance-2-0-260128", model_type="seedance", auth="bearer"):
    return VideoRoute(
        provider="test",
        model=model,
        api_key="sk-test",
        base_url="https://gw.test",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
        channel=channel,
        model_type=model_type,
        auth_scheme=auth,
    )


def _config(channel_providers=None, allowed_models=None, provider_options=None):
    return SimpleNamespace(
        video_generation=SimpleNamespace(
            provider="doubao",
            model="doubao-seedance-2-0-260128",
            submit_timeout_seconds=30,
            status_timeout_seconds=10,
            channel_providers=channel_providers or {},
            allowed_models=allowed_models or [],
        ),
        provider={
            "newapi": SimpleNamespace(
                api_key="sk-newapi",
                base_url="https://newapi.test/",
                options=provider_options if provider_options is not None else {"auth_scheme": "raw"},
            ),
            "doubao": SimpleNamespace(
                api_key="sk-doubao", base_url="https://api.tokenspace.net.cn", options={}
            ),
        },
    )


# ── model canonicalization ──────────────────────────────────────────────────

def test_sd2_roman_numeral_canonicalization():
    # All three spellings of the suffix resolve to the canonical U+2160 form.
    for spelling in ("video-sd-720p-proI", "video-sd-720p-proi", "video-sd-720p-proⅠ"):
        assert canonicalize_sd2_model_name(spelling) == "video-sd-720p-proⅠ"
        assert is_sd2_model(spelling)
    assert canonicalize_sd2_model_name("seedance-2.0-480-fastI") == SD2_MODELS[0]
    # A non-sd2 model passes through untouched.
    assert canonicalize_sd2_model_name("doubao-seedance-2-0-260128") == "doubao-seedance-2-0-260128"
    assert not is_sd2_model("doubao-seedance-2-0-260128")


def test_sd2_native_resolution_tiers():
    assert sd2_native_resolution("seedance-2.0-480-fastⅠ") == "480p"
    assert sd2_native_resolution("video-sd-720p-proI") == "720p"
    assert sd2_native_resolution("video-sd-1080p-pro") == "1080p"


def test_wan3_model_detection_and_canonicalization():
    assert is_wan3_model("wan3.0-video")
    assert is_wan3_model("wan3.0-video-prime")
    assert is_wan3_model("Wan3.0-Video")
    assert not is_wan3_model("video-sd-1080p-pro")
    assert canonicalize_wan3_model_name("wan3") == "wan3.0-video"
    assert canonicalize_wan3_model_name("WAN3.0-VIDEO-PRIME") == "wan3.0-video-prime"


# ── wan3 parameter mapping ──────────────────────────────────────────────────

def test_wan3_duration_smart_and_clamp():
    assert clamp_wan3_duration(-1) == -1  # smart duration is native on wan3
    assert clamp_wan3_duration(1) == 2
    assert clamp_wan3_duration(45) == 30
    assert clamp_wan3_duration(15) == 15
    assert clamp_wan3_duration("bogus") == 5


def test_wan3_resolution_tiers():
    assert map_wan3_resolution("480p") == "480p"
    assert map_wan3_resolution("512") == "480p"
    assert map_wan3_resolution("720p") == "720p"
    assert map_wan3_resolution("768P") == "720p"
    assert map_wan3_resolution("1080p") == "1080p"
    assert map_wan3_resolution("") == "1080p"
    assert map_wan3_resolution(None) == "1080p"


# ── routing ─────────────────────────────────────────────────────────────────

def test_wan3_routing_precedes_other_families():
    route = resolve_route("wan3.0-video-prime", _config({"task": "newapi"}))
    assert route.channel == "task"
    assert route.model_type == WAN3_MODEL_TYPE
    assert route.model == "wan3.0-video-prime"
    assert route.auth_scheme == "raw"


def test_sd2_routing_uses_sd2_channel():
    route = resolve_route("video-sd-720p-proI", _config({"sd2": "newapi"}))
    assert route.channel == "sd2"
    assert route.model == "video-sd-720p-proⅠ"
    assert route.base_url == "https://newapi.test"  # trailing slash stripped


def test_resolve_route_legacy_doubao_unchanged():
    route = resolve_route(None, _config())
    assert route.channel == "ark"
    assert route.model == "doubao-seedance-2-0-260128"
    assert route.api_key == "sk-doubao"
    assert route.auth_scheme == "bearer"


def test_resolve_route_missing_channel_provider_errors():
    with pytest.raises(RuntimeError, match="channel_providers"):
        resolve_route("wan3.0-video", _config({}))


def test_resolve_route_allowed_models_whitelist():
    config = _config({"task": "newapi"}, allowed_models=["wan3.0-video"])
    assert resolve_route("wan3.0-video", config).channel == "task"
    with pytest.raises(RuntimeError, match="allowed_models"):
        resolve_route("wan3.0-video-prime", config)


def test_raw_auth_scheme_header():
    assert auth_header(_route(auth="raw", model="wan3.0-video")) == "sk-test"
    assert auth_header(_route(auth="bearer")) == "Bearer sk-test"


# ── validation ──────────────────────────────────────────────────────────────

def test_720p_pro_rejects_video_refs():
    route = _route("sd2", "video-sd-720p-proⅠ", "sd2_video")
    with pytest.raises(RuntimeError, match="silently discards video references"):
        validate_request(
            route,
            resolution="720p",
            ratio="9:16",
            duration=5,
            generate_audio=True,
            input_mimes=["image/png", "video/mp4"],
        )
    # Image-only references are fine.
    validate_request(
        route,
        resolution="720p",
        ratio="9:16",
        duration=5,
        generate_audio=True,
        input_mimes=["image/png"],
    )


def test_sd2_resolution_must_match_model_tier():
    route = _route("sd2", "video-sd-720p-proⅠ", "sd2_video")
    with pytest.raises(RuntimeError, match="720p natively"):
        validate_request(
            route,
            resolution="1080p",
            ratio="9:16",
            duration=5,
            generate_audio=True,
            input_mimes=[],
        )


def test_wan3_validation_rejects_21_9_and_bad_duration():
    route = _route("task", "wan3.0-video", WAN3_MODEL_TYPE)
    with pytest.raises(RuntimeError, match="21:9"):
        validate_request(
            route, resolution="720p", ratio="21:9", duration=5,
            generate_audio=True, input_mimes=[],
        )
    with pytest.raises(RuntimeError, match="2-30"):
        validate_request(
            route, resolution="720p", ratio="9:16", duration=45,
            generate_audio=True, input_mimes=[],
        )
    validate_request(  # -1 smart duration is valid
        route, resolution="720p", ratio="9:16", duration=-1,
        generate_audio=True, input_mimes=[],
    )


def test_ark_validation_keeps_seedance_rules():
    route = _route()
    validate_request(
        route, resolution="1080p", ratio="9:16", duration=-1,
        generate_audio=True, input_mimes=[],
    )
    with pytest.raises(RuntimeError, match="1080p"):
        validate_request(
            _route(model="doubao-seedance-2-0-fast-260128"),
            resolution="1080p", ratio="9:16", duration=5,
            generate_audio=False, input_mimes=[],
        )


# ── payloads ────────────────────────────────────────────────────────────────

def test_sd2_payload_top_level_refs_and_duration_omission():
    route = _route("sd2", "video-sd-1080p-pro", "sd2_video")
    refs = [
        {"kind": "image", "url": "https://oss/a.png", "role": "reference_image"},
        {"kind": "image", "url": "https://oss/b.png", "role": "reference_image"},
        {"kind": "video", "url": "https://oss/c.mp4", "role": "reference_video"},
    ]
    path, body = build_payload(
        route, prompt="p", refs=refs, resolution="1080p", ratio="9:16",
        duration=-1, generate_audio=True, watermark=False,
    )
    assert path == "/v1/videos"
    assert body["image_url"] == "https://oss/a.png"
    assert body["extra_images"] == ["https://oss/b.png"]
    assert body["extra_videos"] == ["https://oss/c.mp4"]
    assert "duration" not in body  # -1 is not on this channel; omit, don't error
    assert body["resolution"] == "1080p"

    _, timed = build_payload(
        route, prompt="p", refs=[], resolution="1080p", ratio="adaptive",
        duration=10, generate_audio=True, watermark=False,
    )
    assert timed["duration"] == 10
    assert "ratio" not in timed  # adaptive is omitted


def test_wan3_refs_forced_into_content_with_roles():
    route = _route("task", "wan3.0-video", WAN3_MODEL_TYPE)
    refs = [{"kind": "image", "url": "https://oss/a.png", "role": ""}]
    path, body = build_payload(
        route, prompt="p", refs=refs, resolution="720p", ratio="9:16",
        duration=-1, generate_audio=True, watermark=False,
    )
    assert path == "/v1/video/generations"
    assert "images" not in body  # the task payload has no top-level images field
    content = body["metadata"]["content"]
    assert content[0]["role"] == "reference_image"  # role is mandatory
    assert body["metadata"]["duration"] == -1
    assert body["metadata"]["resolution"] == "720p"
    assert body["model"] == "wan3.0-video"


# ── task ids and statuses ───────────────────────────────────────────────────

def test_sd2_task_id_uses_id_not_task_id():
    route = _route("sd2", "video-sd-1080p-pro", "sd2_video")
    raw = {"id": "task_abc", "task_id": "overwritten-later"}
    assert extract_task_id(route, raw) == "task_abc"
    with pytest.raises(RuntimeError):
        extract_task_id(route, {"task_id": "only-the-trap-field"})


def test_sd2_completed_without_url_stays_in_progress():
    route = _route("sd2", "video-sd-1080p-pro", "sd2_video")
    assert normalize_state(route, {"status": "completed"}) == "in_progress"
    done = {"status": "completed", "video_url": "https://cdn/x.mp4"}
    assert normalize_state(route, done) == "completed"
    assert result_video_url(route, done) == "https://cdn/x.mp4"
    # URL priority chain fallbacks
    assert (
        result_video_url(route, {"result": {"url": "https://cdn/y.mp4"}})
        == "https://cdn/y.mp4"
    )


def test_task_channel_status_envelope_and_result_url():
    route = _route("task", "wan3.0-video", WAN3_MODEL_TYPE)
    assert normalize_state(route, {"status": "SUCCESS"}) == "completed"
    assert normalize_state(route, {"status": "FAILURE"}) == "failed"
    assert normalize_state(route, {"status": "IN_PROGRESS"}) == "in_progress"
    assert normalize_state(route, {"status": "QUEUED"}) == "queued"
    assert normalize_state(route, {"status": "???"}) == "in_progress"
    assert result_video_url(route, {"result_url": "https://cdn/z.mp4"}) == "https://cdn/z.mp4"


# ── prompt hash ─────────────────────────────────────────────────────────────

def test_prompt_hash_stability_and_key_order():
    base = dict(
        prompt="p", model_type="sd2_video", model_name="video-sd-1080p-pro",
        duration=5, ratio="9:16", resolution="1080p",
        inputs=[{"digest": "etag1:100", "kind": "image"}],
        extra_params={"generate_audio": True, "watermark": False},
    )
    a = compute_prompt_hash(**base)
    # Key order inside extra_params must not fork the hash.
    b = compute_prompt_hash(**{**base, "extra_params": {"watermark": False, "generate_audio": True}})
    assert a == b
    assert len(a) == 64


def test_prompt_hash_differs_on_inputs_and_extra_params():
    base = dict(
        prompt="p", model_type="sd2_video", model_name="video-sd-1080p-pro",
        duration=5, ratio="9:16", resolution="1080p",
        inputs=[{"digest": "etag1:100", "kind": "image"}],
        extra_params={"generate_audio": True, "watermark": False},
    )
    a = compute_prompt_hash(**base)
    # The two documented false-hit traps: different reference bytes, and
    # generate_audio flipping.
    different_input = compute_prompt_hash(
        **{**base, "inputs": [{"digest": "etag2:100", "kind": "image"}]}
    )
    different_audio = compute_prompt_hash(
        **{**base, "extra_params": {"generate_audio": False, "watermark": False}}
    )
    assert a != different_input
    assert a != different_audio
