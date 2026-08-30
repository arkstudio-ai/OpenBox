"""Multi-channel video routing: model canonicalization, payloads, statuses.

Pure functions, no DB. The edge cases here are the ones the reference system
(bossip) learned the hard way — the U+2160 Ⅰ spelling, the sd2 poll-id trap,
silent reference discarding, wan3's wider duration window.
"""
from dataclasses import replace
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
    provider_route_fingerprint,
    provider_route_mismatch,
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


# ── durable provider route identity ─────────────────────────────────────────

def test_provider_route_fingerprint_is_non_secret_and_covers_route_identity():
    route = _route()
    fingerprint = provider_route_fingerprint(route)

    assert fingerprint.startswith("v1:")
    assert len(fingerprint) == 67
    assert route.api_key not in fingerprint
    # A transport-equivalent trailing slash is normalized.
    assert provider_route_fingerprint(replace(route, base_url=route.base_url + "/")) == fingerprint

    variants = [
        replace(route, provider="another-provider"),
        replace(route, channel="sd2"),
        replace(route, wire_format="bossip_videos"),
        replace(route, base_url="https://another-gw.test"),
        replace(route, auth_scheme="raw"),
        replace(route, api_key="sk-rotated"),
    ]
    assert all(provider_route_fingerprint(candidate) != fingerprint for candidate in variants)


def test_provider_route_mismatch_requires_complete_identity_even_when_legacy_wire_matches():
    direct = _route()
    relay = replace(
        direct,
        base_url="https://openapi.bossipai.com.cn",
        wire_format="bossip_videos",
    )
    snapshot = {"provider_route_fingerprint": provider_route_fingerprint(direct)}

    assert provider_route_mismatch(snapshot, direct) is None
    assert "fingerprint differs" in str(provider_route_mismatch(snapshot, relay))
    assert "no complete provider route fingerprint" in str(
        provider_route_mismatch({"provider_wire_format": "bossip_videos"}, relay)
    )
    assert "legacy submitted wire" in str(provider_route_mismatch({}, relay))
    assert "no complete provider route fingerprint" in str(
        provider_route_mismatch({}, direct)
    )

    # Wire compatibility alone cannot detect endpoint or account rotation.
    legacy_direct = {"provider_wire_format": "tokenspace_contents"}
    assert provider_route_mismatch(legacy_direct, replace(direct, api_key="sk-rotated"))
    assert provider_route_mismatch(
        legacy_direct,
        replace(direct, base_url="https://new-account-gateway.test"),
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
        character_reference_type="virtual",
        character_identity_id=None,
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
        character_reference_type="virtual",
        character_identity_id=None,
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


def test_prompt_hash_separates_real_person_from_virtual():
    """A virtual render must never be served as a verified real-person one.

    Same prompt, same portrait bytes, same everything else — only the
    character reference type differs. The portrait routes to an AIGC group
    under "virtual" and a LivenessFace group under "real_person", so these are
    materially different submissions and must not share a reuse key.
    """
    base = dict(
        prompt="p", model_type="seedance", model_name="doubao-seedance-2-0-260128",
        duration=5, ratio="9:16", resolution="720p",
        inputs=[{"digest": "etag1:100", "kind": "image"}],
        extra_params={"generate_audio": True, "watermark": False},
    )
    virtual = compute_prompt_hash(
        **base, character_reference_type="virtual", character_identity_id=None
    )
    real_person = compute_prompt_hash(
        **base, character_reference_type="real_person", character_identity_id="identity_a",
    )
    assert virtual != real_person


def test_prompt_hash_separates_distinct_verified_identities():
    """A consented render must not cross to a different identity."""
    base = dict(
        prompt="p", model_type="seedance", model_name="doubao-seedance-2-0-260128",
        duration=5, ratio="9:16", resolution="720p",
        inputs=[{"digest": "etag1:100", "kind": "image"}],
        extra_params={"generate_audio": True, "watermark": False},
        character_reference_type="real_person",
    )
    a = compute_prompt_hash(**base, character_identity_id="identity_a")
    b = compute_prompt_hash(**base, character_identity_id="identity_b")
    assert a != b
    # Same identity still dedupes — the feature keeps working.
    assert a == compute_prompt_hash(**base, character_identity_id="identity_a")


def test_prompt_hash_requires_the_character_fields():
    """Omission was the original bug, so it must fail loudly, not silently."""
    import pytest

    with pytest.raises(TypeError):
        compute_prompt_hash(
            prompt="p", model_type="seedance", model_name="m",
            duration=5, ratio="9:16", resolution="720p",
            inputs=None, extra_params=None,
        )


# ── declared models (config-driven routing) ─────────────────────────────────

def _declared_config(models=None, channel_providers=None, allowed=None):
    """A config stub shaped like the real one, with a declared model list."""
    from core.config import ProviderConfig, VideoModelConfig

    return SimpleNamespace(
        video_generation=SimpleNamespace(
            provider="doubao",
            model="doubao-seedance-2-0-260128",
            models=[VideoModelConfig(**m) for m in (models or [])],
            channel_providers=channel_providers or {},
            allowed_models=allowed or [],
            submit_timeout_seconds=180,
            status_timeout_seconds=60,
        ),
        provider={
            "doubao": ProviderConfig(api_key="sk-ark", base_url="https://api.tokenspace.net.cn"),
            "newapi": ProviderConfig(api_key="sk-gw", base_url="https://gw.test"),
        },
    )


def test_declared_model_routes_without_touching_code():
    """The point of the feature: a new model is a config entry, not a release."""
    cfg = _declared_config(
        models=[{"id": "brand-new-video", "channel": "task", "provider": "newapi"}]
    )
    route = resolve_route("brand-new-video", cfg)
    assert route.channel == "task"
    assert route.model == "brand-new-video"
    assert route.base_url == "https://gw.test"
    assert route.model_type == WAN3_MODEL_TYPE


def test_declaration_overrides_name_inference():
    """A declared entry wins over the historical family guess.

    Without this, a model whose name merely starts with "wan3" could never be
    moved to another channel without editing the predicate.
    """
    cfg = _declared_config(models=[{"id": "wan3.0-video", "channel": "sd2", "provider": "newapi"}])
    assert resolve_route("wan3.0-video", cfg).channel == "sd2"


def test_undeclared_deployment_keeps_the_old_inference():
    """Declaring nothing must not change an existing deployment's behaviour."""
    cfg = _declared_config(channel_providers={"task": "newapi"})
    assert resolve_route("wan3.0-video", cfg).channel == "task"
    assert resolve_route(None, cfg).channel == "ark"


def test_declared_ark_model_uses_its_own_credential():
    cfg = _declared_config(models=[{"id": "doubao-seedance-2-0-260128", "channel": "ark"}])
    route = resolve_route("doubao-seedance-2-0-260128", cfg)
    assert route.channel == "ark"
    assert route.base_url == "https://api.tokenspace.net.cn"
    assert route.wire_format == "tokenspace_contents"


def test_declared_gateway_model_without_a_credential_fails_actionably():
    cfg = _declared_config(models=[{"id": "orphan-video", "channel": "task"}])
    with pytest.raises(RuntimeError) as excinfo:
        resolve_route("orphan-video", cfg)
    assert "channel_providers" in str(excinfo.value)


def test_allowed_models_still_gates_a_declared_model():
    cfg = _declared_config(
        models=[{"id": "pricey-video", "channel": "task", "provider": "newapi"}],
        allowed=["cheap-video"],
    )
    with pytest.raises(RuntimeError):
        resolve_route("pricey-video", cfg)


# ── declared capabilities ───────────────────────────────────────────────────

def _declared(**kw):
    from core.config import VideoModelConfig

    return VideoModelConfig(id="m1", **kw)


def test_declared_capability_refuses_a_resolution_the_gateway_would_swap():
    """The relay substitutes silently and bills; this is the only place to catch it."""
    with pytest.raises(RuntimeError) as excinfo:
        validate_request(
            _route(channel="sd2", model="video-sd-1080p-pro"),
            resolution="1080p", ratio="9:16", duration=5,
            generate_audio=False, input_mimes=[],
            declared=_declared(resolutions=["480p", "720p"]),
        )
    assert "silently substituted" in str(excinfo.value)


def test_declared_capability_caps_duration_and_reference_kinds():
    base = dict(ratio="9:16", generate_audio=False, resolution="")
    with pytest.raises(RuntimeError):
        validate_request(_route(channel="task", model_type=WAN3_MODEL_TYPE), duration=20,
                         input_mimes=[], declared=_declared(max_duration_seconds=10), **base)
    with pytest.raises(RuntimeError):
        validate_request(_route(channel="task", model_type=WAN3_MODEL_TYPE), duration=5,
                         input_mimes=["video/mp4"],
                         declared=_declared(supports_reference_video=False), **base)
    with pytest.raises(RuntimeError):
        validate_request(_route(channel="task", model_type=WAN3_MODEL_TYPE), duration=5,
                         input_mimes=["image/png"],
                         declared=_declared(supports_reference_image=False), **base)


def test_smart_duration_is_not_capped():
    """-1 means "let the provider choose" and must survive a declared ceiling."""
    validate_request(
        _route(channel="task", model_type=WAN3_MODEL_TYPE),
        resolution="", ratio="9:16", duration=-1, generate_audio=False,
        input_mimes=[], declared=_declared(max_duration_seconds=10),
    )


def test_no_declaration_leaves_validation_exactly_as_before():
    validate_request(
        _route(channel="task", model_type=WAN3_MODEL_TYPE),
        resolution="720p", ratio="9:16", duration=30, generate_audio=False,
        input_mimes=["video/mp4"], declared=None,
    )


def test_sd2_result_url_reads_the_relays_metadata_shape():
    """Verified against the live BossIP relay, which puts it only there.

    A completed task whose URL we cannot find is the worst failure mode in the
    pipeline: the generation is paid for, the provider says success, and the
    segment still fails.
    """
    route = _route(channel="sd2", model="wan3.0-video")
    completed = {
        "id": "task_x", "status": "completed", "progress": 100,
        "metadata": {"url": "https://dashscope-a717.oss-accelerate.aliyuncs.com/a.mp4"},
    }
    assert result_video_url(route, completed).endswith("/a.mp4")
    # The older top-level shapes still win when present.
    assert result_video_url(route, {"url": "https://cdn/top.mp4", **completed}) == "https://cdn/top.mp4"
    assert result_video_url(route, {"status": "completed"}) == ""


def test_an_undeclared_id_is_refused_instead_of_inferred():
    """A near-miss must not silently escape the deployment's declaration.

    Observed in the browser: an agent passed "wan3.0" where the deployment
    declared "wan3.0-video" on the sd2 channel. Exact-match then missed, name
    inference took over, and it routed to the `task` channel this relay does
    not even expose — a paid call aimed at the wrong endpoint.
    """
    cfg = _declared_config(
        models=[{"id": "wan3.0-video", "channel": "sd2", "provider": "newapi"}],
        channel_providers={"task": "newapi"},
    )
    with pytest.raises(RuntimeError) as excinfo:
        resolve_route("wan3.0", cfg)
    message = str(excinfo.value)
    assert "not declared" in message
    # Naming the valid ids is what lets the agent correct itself.
    assert "wan3.0-video" in message


def test_inference_still_applies_when_nothing_is_declared():
    cfg = _declared_config(channel_providers={"task": "newapi"})
    assert resolve_route("wan3.0-video", cfg).channel == "task"
