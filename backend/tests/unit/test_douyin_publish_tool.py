"""douyin_publish: the agent's door into the 授权中心 (status / authorize / publish / result)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from cache import set_cache
from cache.memory_cache import MemoryCache
from core.config import get_config
from core.markdown import parse_frontmatter
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.platform_account import PlatformAccount
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from platforms import service
from platforms.registry import get_provider, set_provider
from tests.unit.test_platform_accounts import KEY, NOW, FakeClient, FakeOss, FakeProvider
from tool import douyin_publish as mod
from tool.douyin_publish import DouyinPublishArgs, douyin_publish_tool, execute_douyin_publish
from tool.tool import ToolContext


@pytest.fixture
def fake_platform(monkeypatch):
    monkeypatch.setattr(get_config(), "wuying_channel_key", KEY)
    monkeypatch.setattr(get_config(), "secrets_master_key", "")
    set_cache(MemoryCache())
    original = get_provider("douyin")
    fake = FakeProvider()
    fake.client = FakeClient(short_link="snssdk1128://webview?url=short", share=f"share-{uuid4().hex[:8]}")
    set_provider(fake)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    pinned: list[dict] = []

    async def fake_pin(ctx, png, *, name, label, caption, kind):
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        pinned.append({"name": name, "label": label, "kind": kind, "caption": caption})
        return f"asset_qr_{len(pinned)}"

    monkeypatch.setattr(mod, "_pin_png", fake_pin)
    monkeypatch.setattr(mod, "get_oss", lambda: FakeOss())
    fake.pinned = pinned
    yield fake
    set_provider(original)
    set_cache(None)


async def _ctx(role: str = "owner") -> ToolContext:
    suffix = uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    user_id, ws_id = f"user_{suffix}", f"ws_{suffix}"
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"dy-{suffix}", created_at=now, updated_at=now))
        db.add(Workspace(id=ws_id, name="w", owner_user_id=user_id, kind="personal", created_at=now, updated_at=now))
        db.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role, status="active", created_at=now, updated_at=now))
        await db.commit()
    return ToolContext(session_id=f"s_{suffix}", user_id=user_id, workspace_id=ws_id, message_id="m1", part_id="p1")


def test_tool_is_registered_build_only_and_identity_free():
    assert douyin_publish_tool.sandbox_required is False
    assert douyin_publish_tool.parallel_safe is False
    assert "user_id" not in DouyinPublishArgs.model_fields and "workspace_id" not in DouyinPublishArgs.model_fields
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    from tool.registry import get_tool, register_builtin_tools

    register_builtin_tools()
    assert get_tool("douyin_publish") is not None
    assert "douyin_publish" in AGENTS["build"].tools and "douyin_publish" in BUILD_ONLY_WORKFLOW_TOOLS


def test_skill_frontmatter_names_the_tool():
    root = Path(__file__).resolve().parents[2] / ".openbox" / "skills"
    text = (root / "douyin-publish" / "SKILL.md").read_text(encoding="utf-8")
    metadata, _ = parse_frontmatter(text)
    assert metadata["name"] == "douyin-publish"
    assert "douyin_publish" in metadata["allowed-tools"] and "question" in metadata["allowed-tools"]
    assert len(text.splitlines()) <= 200
    # a delivered film is handed to the desktop route; the QR skill is reached from there as the fallback
    assert "douyin-desktop-publish" in (root / "video-production" / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_status_then_authorize_when_nothing_is_bound(fake_platform):
    ctx = await _ctx()
    status = await execute_douyin_publish(DouyinPublishArgs(action="status"), ctx)
    assert status.metadata["bound"] is False and "authorize" in status.output

    auth = await execute_douyin_publish(DouyinPublishArgs(action="authorize"), ctx)
    assert "is_call_app=1" in auth.metadata["authorizeUrl"]
    assert auth.metadata["asset_id"] == "asset_qr_1"
    assert datetime.fromisoformat(auth.metadata["expiresAt"]) > datetime.now(timezone.utc)
    assert fake_platform.pinned[0]["kind"] == "qr_code"
    # Publishing before anyone scanned is refused with the structured code.
    denied = await execute_douyin_publish(DouyinPublishArgs(action="publish", asset_id="x"), ctx)
    assert denied.metadata["code"] == "PLATFORM_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_member_cannot_authorize(fake_platform):
    ctx = await _ctx(role="member")
    auth = await execute_douyin_publish(DouyinPublishArgs(action="authorize"), ctx)
    assert auth.metadata["code"] == "PLATFORM_ROLE_REQUIRED"


@pytest.mark.asyncio
async def test_publish_and_result_after_binding(fake_platform):
    ctx = await _ctx()
    started = await service.start_authorize(user_id=ctx.user_id, workspace_id=ctx.workspace_id, platform="douyin")
    await service.complete_callback(platform="douyin", code="c", state=started["state"], granted_scopes="user_info")
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id="asset_clip", user_id=ctx.user_id, workspace_id=ctx.workspace_id, name="clip.mp4",
                oss_key=f"assets/{ctx.user_id}/asset_clip/clip.mp4", mime="video/mp4", size=2048,
                status="ready", created_at=NOW,
            )
        )
        await db.commit()

    status = await execute_douyin_publish(DouyinPublishArgs(action="status"), ctx)
    assert status.metadata["bound"] is True and "预计到期" in status.output

    missing = await execute_douyin_publish(DouyinPublishArgs(action="publish", asset_id="nope"), ctx)
    assert missing.metadata["code"] == "PUBLISH_FILE_NOT_FOUND"

    published = await execute_douyin_publish(
        DouyinPublishArgs(action="publish", asset_id="asset_clip", title="老房改造第一集", hashtags=["装修", "#老房改造"]),
        ctx,
    )
    assert "job_id=" in published.output and published.metadata["schemaSource"] == "get_share"
    assert published.metadata["launchUrl"] == "snssdk1128://webview?url=short"
    assert published.metadata["expiresAt"]
    assert fake_platform.pinned[-1]["label"] == "抖音投稿二维码" and "老房改造第一集" in fake_platform.pinned[-1]["caption"]
    job_id = published.metadata["job_id"]

    pending = await execute_douyin_publish(DouyinPublishArgs(action="result", job_id=job_id), ctx)
    assert pending.metadata["status"] == "pending"

    await service.handle_douyin_event(
        {"event": "create_video", "from_user_id": "open-1", "content": {"share_id": published.metadata["share_id"], "item_id": "item-1"}}
    )
    done = await execute_douyin_publish(DouyinPublishArgs(action="result", job_id=job_id), ctx)
    assert done.metadata["status"] == "published" and "item-1" in done.output

    # Another workspace cannot read this job.
    other = await _ctx()
    foreign = await execute_douyin_publish(DouyinPublishArgs(action="result", job_id=job_id), other)
    assert foreign.metadata["code"] == "PUBLISH_JOB_NOT_FOUND"


def test_qr_png_is_a_png():
    assert mod.qr_png("snssdk1128://openplatform/share?share_type=h5")[:8] == b"\x89PNG\r\n\x1a\n"
