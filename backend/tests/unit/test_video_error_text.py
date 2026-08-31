"""User-facing video errors expose only explicitly public diagnostics."""

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
