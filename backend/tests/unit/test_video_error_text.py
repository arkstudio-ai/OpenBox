"""User-facing video errors expose only explicitly public diagnostics."""

import pytest

from tool.video_production import _public_error


def test_ordinary_exception_hides_its_message():
    text = _public_error(RuntimeError("token=sk-live url=https://signed.example"))

    assert text == "RuntimeError: operation failed"
    assert "sk-live" not in text


def test_http_exception_reports_only_status_and_reason():
    class Response:
        status_code = 503
        reason_phrase = "Service Unavailable"

    error = RuntimeError("provider body contains token=sk-secret")
    error.response = Response()

    assert _public_error(error) == "HTTP 503: Service Unavailable"


def test_explicit_public_error_exposes_the_actionable_message():
    error = RuntimeError("请检查视频网关配置")
    error.public_message = True

    assert _public_error(error) == "请检查视频网关配置"


def test_our_own_refusals_say_why_while_provider_failures_stay_scrubbed():
    """A caller told "invalid" without being told what is invalid cannot fix it.

    Capability refusals are authored here and name only the caller's own
    request, so they carry none of the response bodies or signed URLs that the
    scrubber exists to withhold. Hiding them made the free estimate useless.
    """
    from tool.video_production import _public_error
    from tool.video_providers import VideoRequestError

    refusal = VideoRequestError("model wan3.0-video supports ratios 16:9/9:16; requested 21:9")
    assert _public_error(refusal) == str(refusal)

    leaky = RuntimeError("connect to https://relay.example/v1/videos?sig=SECRET failed")
    assert "SECRET" not in _public_error(leaky)
    assert _public_error(leaky) == "RuntimeError: operation failed"


def test_capability_validators_raise_the_surfaceable_type():
    from types import SimpleNamespace

    from core.config import VideoModelConfig
    from tool import video_providers

    entry = VideoModelConfig(id="m", ratios=["9:16"], duration_range=(2, 30))
    route = SimpleNamespace(channel="sd2", model="m", model_type="sd2_video")

    with pytest.raises(video_providers.VideoRequestError):
        video_providers.validate_request(
            route, resolution="", ratio="21:9", duration=-1,
            generate_audio=True, input_mimes=[], declared=entry,
        )
