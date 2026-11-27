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


async def test_mixed_media_and_generated_archive_titles_use_asset_and_session_scope(monkeypatch):
    from api import assets
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.workspace import WorkspaceMember
    from db.repository.user_repo import PgUserRepo
    from session.session import create_session

    repo = PgUserRepo()
    owner = await repo.create(id="archive-owner", username="archive-owner", password_hash=None)
    reader = await repo.create(id="archive-reader", username="archive-reader", password_hash=None)
    workspace = owner["default_workspace_id"]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(WorkspaceMember(
            workspace_id=workspace, user_id=reader["id"], role="member", status="active",
            created_at=now, updated_at=now,
        ))

    shared = await create_session(title="Launch", user_id=owner["id"], workspace_id=workspace)
    subagent = await create_session(
        title="Internal task", parent_id=shared.id, user_id=owner["id"], workspace_id=workspace,
    )
    untitled = await create_session(user_id=reader["id"], workspace_id=workspace)
    private = await create_session(
        title="Private archive", user_id=owner["id"], workspace_id=workspace,
        visibility="private",
    )
    async with get_db_session() as db:
        for asset_id, name, source, session_id, user_id in [
            ("01-doc", "menu.pdf", "agent", shared.id, owner["id"]),
            ("02-photo", "menu.jpg", "agent", subagent.id, owner["id"]),
            ("03-clip", "shop.mp4", "agent", untitled.id, reader["id"]),
            ("04-legacy", "legacy.jpg", "agent", private.id, reader["id"]),
            ("05-private", "private.jpg", "agent", private.id, owner["id"]),
            ("06-upload", "upload.jpg", "user", None, reader["id"]),
        ]:
            db.add(FileAsset(
                id=asset_id, user_id=user_id, workspace_id=workspace, session_id=session_id,
                name=name, mime="application/octet-stream", oss_key=asset_id,
                source=source, status="ready", created_at=now,
            ))

    monkeypatch.setattr(assets, "_oss_or_503", lambda: SimpleNamespace(presign_get=lambda key: key))
    identity = {"user_id": reader["id"], "workspace_id": workspace}
    opts = {"project": "all", "kind": "media", "current_user": identity, "_workspace": {}}
    first = await assets.list_assets(**opts, source="agent", limit=2, offset=0)
    second = await assets.list_assets(**opts, source="agent", limit=2, offset=2)
    assert first["total"] == second["total"] == 3
    assert first["hasMore"] and not second["hasMore"]
    assert [(item["id"], item["sessionTitle"]) for item in first["items"]] == [
        ("02-photo", "Launch"),
        ("03-clip", ""),
    ]
    assert first["items"][0]["sessionId"] == subagent.id
    assert first["items"][0]["archiveSessionId"] == shared.id
    assert second["items"][0]["id"] == "04-legacy"
    assert "sessionTitle" not in second["items"][0]

    mixed = await assets.list_assets(**opts, source="all", offset=0, limit=10, owned_only=True)
    assert [item["id"] for item in mixed["items"]] == ["03-clip", "04-legacy", "06-upload"]
    assert all("sessionTitle" not in item for item in mixed["items"])
