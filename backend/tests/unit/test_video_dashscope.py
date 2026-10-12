"""Native Wan requests preserve approved roles/audio and durable task identity."""
import json
from types import SimpleNamespace

import httpx
import pytest

from core.config import VideoModelConfig, ProviderConfig
from tool import video_providers as providers


@pytest.fixture
def route():
    entry = VideoModelConfig(id="wan3.0-video", channel="dashscope", provider="aliyun",
                             supports_first_last_frame=True, supports_reference_audio=True,
                             supports_seed=True, duration_range=(2, 30))
    config = SimpleNamespace(
        video_generation=SimpleNamespace(models=[entry], allowed_models=[],
            model=entry.id, submit_timeout_seconds=30, status_timeout_seconds=10),
        provider={"aliyun": ProviderConfig(api_key="test-only", base_url="https://dashscope.aliyuncs.com")})
    return providers.resolve_route(entry.id, config)


def payload(route, **changes):
    args = dict(prompt="Approved script", refs=[], resolution="720p", ratio="9:16",
                duration=5, generate_audio=True, watermark=False, seed=42)
    return providers.build_payload(route, **(args | changes))


def test_native_frames_and_audio_preserve_the_approved_request(route):
    path, body = payload(route, refs=[{"kind": "image", "role": "first_frame", "url": "https://asset.invalid/first.png"},
                                     {"kind": "image", "role": "last_frame", "url": "https://asset.invalid/last.png"}])
    assert route.wire_format == "dashscope_video" and route.model_type == "wan3_video"
    assert path == "/api/v1/services/aigc/video-generation/video-synthesis"
    assert body == {"model": "wan3.0-video", "input": {"prompt": "Approved script", "media": [
        {"type": "first_frame", "url": "https://asset.invalid/first.png"},
        {"type": "last_frame", "url": "https://asset.invalid/last.png"}]}, "parameters": {
        "resolution": "720P", "ratio": "9:16", "duration": 5, "audio": True,
        "watermark": False, "prompt_extend": False, "seed": 42}}
    assert payload(route, generate_audio=False, ratio="21:9", duration=30)[1]["parameters"]["audio"] is False


def test_reference_audio_is_not_encoded_as_video(route):
    _, body = payload(route, refs=[{"kind": "image", "url": "https://asset.invalid/ref.png"},
                                   {"kind": "audio", "role": "reference_audio", "url": "https://asset.invalid/ref.wav"}])
    assert [ref["type"] for ref in body["input"]["media"]] == ["reference_image", "reference_audio"]


@pytest.mark.parametrize("changes", [
    {"refs": [{"kind": "image", "role": "last_frame", "url": "x"}]},
    {"refs": [{"kind": "image", "role": "first_frame", "url": "x"},
              {"kind": "audio", "role": "reference_audio", "url": "y"}]},
    {"refs": [{"kind": "image", "role": "reference_video", "url": "x"}]},
    {"refs": [{"kind": "image", "role": "first_frame", "url": "x"}] * 2},
    {"refs": [{"kind": "image", "url": "x"}] * 11},
    {"duration": 0}, {"duration": 31}, {"seed": 2147483648}, {"prompt": ""},
])
def test_invalid_inputs_are_rejected_before_paid_dispatch(route, changes):
    with pytest.raises(providers.VideoRequestError):
        payload(route, **changes)


async def test_submit_then_poll_preserves_nested_identity_and_usage(route, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-only"
        if request.method == "POST":
            assert request.headers["X-DashScope-Async"] == "enable"
            assert json.loads(request.content)["parameters"]["audio"] is True
            return httpx.Response(200, json={"output": {"task_id": "wan-task", "task_status": "PENDING"}})
        assert request.url.path == "/api/v1/tasks/wan-task"
        return httpx.Response(200, json={"output": {"task_id": "wan-task", "task_status": "SUCCEEDED",
            "video_url": "https://asset.invalid/result.mp4"}, "usage": {"fps": 30, "output_video_duration": 5}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    path, body = payload(route)
    accepted = await providers.submit(route, path, body)
    identity = providers.extract_task_id(route, accepted)
    assert identity == "wan-task" and providers.normalize_state(route, accepted) == "queued"
    result = await providers.status(route, identity)
    assert providers.normalize_state(route, result) == "completed"
    assert providers.result_video_url(route, result) == "https://asset.invalid/result.mp4"
    assert result["usage"]["fps"] == 30
    assert len(requests) == 2


@pytest.mark.parametrize("http_status", [400, 429])
def test_a_rejection_with_nested_task_identity_is_never_safe_to_resubmit(route, http_status):
    response = httpx.Response(http_status, json={"output": {"task_id": "already-accepted"}},
                              request=httpx.Request("POST", route.base_url))
    error = httpx.HTTPStatusError("rejected", request=response.request, response=response)
    assert providers.submission_rejection(error) is None
    assert providers.rate_limit_retry_delay(error) is None


def test_async_validation_failure_and_expiry_are_terminal_and_do_not_leak_payloads(route):
    failure = {"output": {"task_id": "accepted-invalid", "task_status": "FAILED",
                          "code": "InvalidParameter", "message": "secret signed url"}}
    assert providers.extract_task_id(route, failure) == "accepted-invalid"
    assert providers.normalize_state(route, failure) == "failed"
    assert "参数" in providers.failure_detail(route, failure)
    assert "secret" not in providers.failure_detail(route, failure)
    expired = {"output": {"task_id": "expired", "task_status": "UNKNOWN"}}
    assert providers.normalize_state(route, expired) == "failed"
    assert "不要自动重新生成" in providers.failure_detail(route, expired)
