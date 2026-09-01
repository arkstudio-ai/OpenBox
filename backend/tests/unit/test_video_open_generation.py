"""The generation tool as a standalone primitive.

These cover what atomization actually changed: a caller can describe a shot
and pay for it without a production, capability limits are enforced from the
declared registry, and two guards stand in for the credits ledger that will
eventually price this.
"""
import pytest
from pydantic import ValidationError

from core.config import VideoGenerationConfig, VideoModelConfig
from tool import video_providers
from tool.video_production import VideoGenerateArgs, VideoInputRef, _model_capability_lines


WAN3 = VideoModelConfig(
    id="wan3.0-video",
    channel="sd2",
    ratios=["adaptive", "16:9", "9:16"],
    duration_range=(2, 30),
    supports_seed=True,
    supports_first_last_frame=True,
    supports_reference_audio=True,
    resolutions=["1080p"],
)
SEEDANCE = VideoModelConfig(
    id="video-sd-720p-proⅠ",
    channel="sd2",
    resolutions=["720p"],
    duration_range=(4, 15),
    supports_reference_video=False,
)


def _route(model: str, channel: str = "sd2"):
    from types import SimpleNamespace

    return SimpleNamespace(channel=channel, model=model, model_type="sd2_video")


def _validate(entry, route, **kw):
    params = dict(
        resolution="", ratio="", duration=-1, generate_audio=True, input_mimes=[]
    )
    params.update(kw)
    video_providers.validate_request(route, declared=entry, **params)


def test_ratio_outside_the_declared_set_is_refused():
    """wan3 rejects 21:9 upstream rather than substituting, so catch it free."""
    with pytest.raises(RuntimeError, match="supports ratios"):
        _validate(WAN3, _route("wan3.0-video"), resolution="1080p", ratio="21:9")


def test_declared_duration_range_replaces_the_old_seedance_clamp():
    """A 24s wan3 request is legal; the old shared 4-15 clamp dropped it."""
    _validate(WAN3, _route("wan3.0-video"), resolution="1080p", ratio="9:16", duration=24)

    with pytest.raises(RuntimeError, match="accepts 4-15s"):
        _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p", duration=24)


def test_frame_roles_are_refused_on_models_that_lack_them():
    with pytest.raises(RuntimeError, match="first_frame/last_frame"):
        _validate(
            SEEDANCE,
            _route("video-sd-720p-proⅠ"),
            resolution="720p",
            roles=("last_frame",),
        )


def test_sd2_refuses_roles_it_cannot_express_even_when_the_model_declares_them():
    """A role the body has no field for would arrive as a plain reference.

    That produces a paid take which quietly ignores the continuity that was
    asked for, so the request is refused instead of being silently downgraded.
    """
    with pytest.raises(RuntimeError, match="cannot express"):
        _validate(
            WAN3,
            _route("wan3.0-video"),
            resolution="1080p",
            ratio="9:16",
            roles=("last_frame",),
        )


def test_sd2_payload_carries_an_explicit_duration_and_seed():
    _path, body = video_providers.build_payload(
        _route("wan3.0-video"),
        prompt="一只猫跳上窗台",
        refs=[],
        resolution="1080p",
        ratio="9:16",
        duration=24,
        generate_audio=True,
        watermark=False,
        seed=42,
    )

    assert body["duration"] == 24
    assert body["seed"] == 42


def test_smart_duration_sends_no_duration_field():
    _path, body = video_providers.build_payload(
        _route("wan3.0-video"),
        prompt="x",
        refs=[],
        resolution="1080p",
        ratio="9:16",
        duration=-1,
        generate_audio=True,
        watermark=False,
    )

    assert "duration" not in body


def test_audio_input_must_name_its_role():
    """Guessing that an audio file is a reference track would change the take."""
    assert VideoInputRef(asset_id="a", role="reference_audio").role == "reference_audio"
    assert VideoInputRef(asset_id="a").role is None


def test_capability_lines_describe_each_model():
    config = type(
        "C", (), {"video_generation": VideoGenerationConfig(model="wan3.0-video", models=[WAN3, SEEDANCE])}
    )()
    text = "\n".join(_model_capability_lines(config))

    assert "default_model=wan3.0-video" in text
    assert "duration=2-30s" in text
    assert "seed" in text
    assert "ratios=adaptive/16:9/9:16" in text


def test_estimate_needs_no_idempotency_key():
    """Validating a request costs nothing, so it must not demand a paid key."""
    args = VideoGenerateArgs(action="estimate", prompt="一只猫")

    assert args.idempotency_key is None


def test_duplicate_override_is_explicit():
    assert VideoGenerateArgs(action="models").allow_duplicate is False
    with pytest.raises(ValidationError):
        VideoGenerateArgs(action="submit", prompt="x")


def test_a_zero_valued_optional_is_read_as_absent():
    """Some callers populate every schema field, including ones they never set.

    Such a caller sends seed=0 and duration=0 for parameters it has no opinion
    about. Reading those as real requests made every seedless model refuse work
    nobody had asked for, and left the caller no way to express "no seed" —
    it cannot omit a field its own serializer always writes.
    """
    args = VideoGenerateArgs(
        action="submit", prompt="一只猫", idempotency_key="k:1", seed=0, duration=0
    )

    assert (args.seed or None) is None
    assert (args.duration or None) is None


def test_a_real_seed_still_travels():
    args = VideoGenerateArgs(
        action="submit", prompt="一只猫", idempotency_key="k:1", seed=42
    )

    assert (args.seed or None) == 42


def test_an_unusable_seed_is_dropped_rather_than_refusing_the_shot():
    """A seed the model cannot use is worth less than the generation itself.

    Missing it costs reproducibility; the video is still the one that was
    asked for. Refusing costs the whole request — and a caller whose
    serializer always writes every field cannot express "no seed" at all,
    so the refusal made every seedless model unreachable from it.
    """
    _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p")

    import inspect
    source = inspect.getsource(video_providers._validate_declared)
    assert "does not accept a seed" not in source


def test_content_changing_roles_are_still_refused():
    """first/last frame and reference audio change what the video IS."""
    with pytest.raises(RuntimeError, match="first_frame/last_frame"):
        _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p",
                  roles=("first_frame",))
    with pytest.raises(RuntimeError, match="audio reference"):
        _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p",
                  roles=("reference_audio",))


def _sd2_body(model, refs, prompt="她自然看向镜头说话。"):
    _path, body = video_providers.build_payload(
        _route(model), prompt=prompt, refs=refs, resolution="1080p",
        ratio="9:16", duration=5, generate_audio=True, watermark=False,
    )
    return body


IMG = [{"kind": "image", "url": "https://oss.test/a.png", "role": "reference_image"}]


def test_wan3_sends_references_through_the_multi_material_path():
    """Measured 2026-09-01: wan3 behind this relay ignores image_url.

    image_url, first_frame_url and a doubao content[] each came back as a
    different person; `images` plus an @image_file_N mention in the prompt is
    the one shape that actually holds the face. Called adapter-to-adapter,
    wan3 locks identity perfectly — so this was never the model's limit, only
    how the request reached it.
    """
    body = _sd2_body("wan3.0-video-prime", IMG)

    assert body["images"] == ["https://oss.test/a.png"]
    assert "image_url" not in body
    assert "@image_file_1" in body["prompt"]


def test_seedance_keeps_the_image_url_path_that_already_works():
    body = _sd2_body("video-sd-1080p-pro", IMG)

    assert body["image_url"] == "https://oss.test/a.png"
    assert "images" not in body
    assert "@image_file" not in body["prompt"]


def test_a_prompt_that_already_names_its_material_is_left_alone():
    body = _sd2_body(
        "wan3.0-video-prime", IMG, prompt="@image_file_1 是主播，她开口说话。"
    )

    assert body["prompt"] == "@image_file_1 是主播，她开口说话。"


def test_every_supplied_image_gets_named():
    """An image the prompt never mentions is simply not used by the relay."""
    two = IMG + [{"kind": "image", "url": "https://oss.test/b.png", "role": "reference_image"}]
    body = _sd2_body("wan3.0-video-prime", two, prompt="@image_file_1 是主播。")

    assert "@image_file_2" in body["prompt"]
