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


def test_seed_and_frame_roles_are_refused_on_models_that_lack_them():
    with pytest.raises(RuntimeError, match="does not accept a seed"):
        _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p", seed=7)

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
