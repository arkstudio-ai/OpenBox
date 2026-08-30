"""User-facing video errors expose only explicitly public diagnostics."""

from tool.video_production import _public_error
from video.materials import MaterialProviderError


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


def test_public_material_error_exposes_the_actionable_message():
    error = MaterialProviderError(
        "请配置 material_base_url",
        retryable=False,
        public=True,
    )

    assert _public_error(error) == "请配置 material_base_url"


def test_private_material_error_hides_the_provider_message():
    error = MaterialProviderError("provider said: token=sk-secret")

    text = _public_error(error)
    assert text == "MaterialProviderError: operation failed"
    assert "sk-secret" not in text
