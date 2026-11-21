from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from api.assets import _to_item
from core.oss import OssClient


def test_library_requests_a_signed_first_frame_only_for_video_items():
    oss = OssClient("media", "cn-shanghai", "oss-cn-shanghai.aliyuncs.com", "key-id", "test-secret")
    video = SimpleNamespace(
        id="asset-1", name="video.mp4", mime="application/octet-stream",
        description="", description_state=None, size=1024, source="user",
        project_id=None, session_id=None, status="ready",
        created_at=datetime.now(timezone.utc), oss_key="uploads/video.mp4",
    )
    assert "thumbnail" not in _to_item(video, oss)

    item = _to_item(video, oss, video_thumbnails=True)
    original = urlsplit(item["url"])
    cover = urlsplit(item["thumbnail"])
    assert (cover.netloc, cover.path) == (original.netloc, original.path)
    assert "x-oss-process" not in parse_qs(original.query)
    assert parse_qs(cover.query)["x-oss-process"] == ["video/snapshot,t_0,f_jpg,w_480,m_fast,ar_auto"]
    assert parse_qs(cover.query)["Signature"] != parse_qs(original.query)["Signature"]

    photo = SimpleNamespace(**{**vars(video), "mime": "image/jpeg", "name": "photo.jpg"})
    assert "thumbnail" not in _to_item(photo, oss, video_thumbnails=True)
