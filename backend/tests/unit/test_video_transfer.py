"""A rejected signed URL must not strand an already-paid generation forever."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from video import transfer
from tool.media.video_production import _copy_provider_video_to_oss, _public_error


@pytest.mark.parametrize("query,expired", [
    ("X-Tos-Date=20260911T064231Z&X-Tos-Expires=86400", True),
    ("X-Tos-Date=20260916T064231Z&X-Tos-Expires=86400", False),
    ("X-Amz-Date=20260911T064231Z&X-Amz-Expires=86400", True),
    ("Expires=1", True),
    ("X-Tos-Date=invalid&X-Tos-Expires=86400", False),
    ("X-Tos-Date=20260911T064231Z&X-Tos-Expires=999999999999999999999", False),
    ("signature=secret", False),
])
def test_only_recognized_expired_signatures_are_terminal(query, expired):
    now = datetime(2026, 9, 16, 1, 45, tzinfo=timezone.utc)
    assert transfer.signed_url_expired("https://video.invalid/video?" + query, now) is expired


def test_backoff_cannot_extend_the_recovery_deadline():
    now = datetime(2026, 9, 16, 1, 45, tzinfo=timezone.utc)
    attempt = {"attempts": 2, "first_attempt_at": (now - timedelta(hours=24) + timedelta(seconds=30)).isoformat()}
    result = transfer.failed(attempt, transfer.TransferError("download", 503), now)
    job = SimpleNamespace(status="transfer_failed", result_data={"transfer": result})
    assert transfer.retry_after(job, now) == 30
    assert transfer.retry_after(job, now + timedelta(seconds=30)) == 0
    assert transfer.exhausted(job.result_data, now + timedelta(seconds=30))


@pytest.mark.parametrize("status,query,expired", [
    (403, "X-Tos-Date=20200101T000000Z&X-Tos-Expires=86400", True),
    (403, "X-Tos-Date=20990101T000000Z&X-Tos-Expires=86400", False),
    (403, "", False),
    (503, "X-Tos-Date=20200101T000000Z&X-Tos-Expires=86400", False),
])
async def test_download_failure_is_classified_without_leaking_signed_url(monkeypatch, status, query, expired):
    calls = []

    def reply(request):
        calls.append((request.method, request.url.host))
        return httpx.Response(status, text="private provider response sig=secret")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw))
    oss = SimpleNamespace(presign_put=lambda *_args, **_kw: "https://oss.invalid/put?sig=private")

    with pytest.raises(transfer.TransferError) as caught:
        await _copy_provider_video_to_oss(
            "https://video.invalid/video?" + query + "&X-Tos-Signature=secret", oss, "object", 100,
        )

    assert caught.value.expired is expired
    assert caught.value.stage == "download"
    assert caught.value.status_code == status
    assert calls == [("GET", "video.invalid")]
    assert "secret" not in _public_error(caught.value)
    assert "video.invalid" not in _public_error(caught.value)


async def test_oss_403_is_not_misclassified_as_an_expired_provider_link(monkeypatch):
    def reply(request):
        return httpx.Response(200, content=b"video") if request.method == "GET" else httpx.Response(403)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw))
    oss = SimpleNamespace(presign_put=lambda *_args, **_kw: "https://oss.invalid/put")

    with pytest.raises(transfer.TransferError) as caught:
        await _copy_provider_video_to_oss(
            "https://video.invalid/video?X-Tos-Date=20200101T000000Z&X-Tos-Expires=1", oss, "object", 100,
        )

    assert caught.value.stage == "upload"
    assert caught.value.expired is False
