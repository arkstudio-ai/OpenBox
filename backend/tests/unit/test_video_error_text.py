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


def test_a_connection_that_never_opened_says_so():
    """Scrubbing a transport failure blamed the provider for our own network.

    On 2026-09-01 a few seconds of local packet loss surfaced to the user as
    "the generation service is down", while the paid task was fine and the
    sweep recovered it minutes later. A connection that never opened carries
    no provider response to leak, so it can say what actually happened.
    """
    import httpx

    from tool.video_production import _public_error

    for failure in (httpx.ConnectError("[Errno 61] Connection refused"),
                    httpx.ConnectTimeout("timed out"),
                    httpx.ReadTimeout("timed out")):
        message = _public_error(failure)
        assert "could not reach the video provider" in message
        assert "do not resubmit" in message


def test_a_reply_that_did_arrive_is_still_scrubbed():
    """Anything with a response body keeps the old treatment."""
    import httpx

    from tool.video_production import _public_error

    request = httpx.Request("POST", "https://relay.example/v1/videos?sig=SECRET")
    response = httpx.Response(500, request=request)
    message = _public_error(
        httpx.HTTPStatusError("boom", request=request, response=response)
    )

    assert message == "HTTP 500: Internal Server Error"
    assert "SECRET" not in message
