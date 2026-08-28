"""Verified-person material sessions stay private, owned, and provider-backed."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.processor import persisted_tool_metadata
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.user import User
from db.models.video_material import VideoMaterialAsset, VideoMaterialGroup
import tool.video_identity as identity_tool_mod
import tool.video_production as production_tool_mod
from tool.tool import ToolContext
from tool.video_identity import VideoIdentityArgs, execute_video_identity
from tool.video_workflow import SegmentSpec, VideoProjectArgs, execute_project
from video.materials import (
    MaterialProviderError,
    MaterialTarget,
    call_material_api,
    create_liveness_session,
    ensure_material_asset,
    get_identity,
    refresh_liveness_session,
)
import video.materials as material_mod


def _target() -> MaterialTarget:
    return MaterialTarget(
        provider="doubao",
        api_key="test-key",
        base_url="https://api.tokenspace.test",
        project_name="default",
        request_timeout_seconds=30,
        poll_interval_seconds=0.01,
        liveness_session_ttl_seconds=300,
        input_url_ttl_seconds=600,
    )


async def _user(prefix: str) -> str:
    suffix = uuid4().hex[:12]
    user_id = f"user_{prefix}_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=f"{prefix}-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
    return user_id


@pytest.mark.asyncio
async def test_material_api_recognizes_result_envelope_errors(monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"

        @staticmethod
        def json():
            return {
                "Result": {
                    "Error": {
                        "Code": "VisualValidatePending",
                        "Message": "真人认证尚未完成",
                    }
                }
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, params, headers, json):
            assert url == "https://api.tokenspace.test/api/material"
            assert params == {
                "Action": "GetVisualValidateResult",
                "Version": "2024-01-01",
            }
            assert headers["Authorization"] == "Bearer test-key"
            assert json == {"BytedToken": "private-token"}
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(MaterialProviderError, match="尚未完成") as caught:
        await call_material_api(
            _target(),
            "GetVisualValidateResult",
            {"BytedToken": "private-token"},
        )

    assert caught.value.code == "VisualValidatePending"


@pytest.mark.asyncio
async def test_liveness_session_hides_polling_token_and_enforces_owner(monkeypatch):
    user_id = await _user("liveness")
    monkeypatch.setattr(material_mod, "configured_material_target", _target)

    async def create_call(_target_value, action, body):
        assert action == "CreateVisualValidateSession"
        assert body == {}
        return {
            "BytedToken": "private-byted-token",
            "H5Link": "https://api.tokenspace.test/real-validate?token=short-lived",
            "QrCode": "data:image/png;base64,dGVzdA==",
            "ExpiresIn": 300,
        }

    monkeypatch.setattr(material_mod, "call_material_api", create_call)
    created = await create_liveness_session(user_id, "主持人本人")

    assert created["status"] == "awaiting_user"
    assert created["authorization_url"].startswith("https://")
    assert created["qr_code"].startswith("data:image/png;base64,")
    assert "provider_token" not in created
    assert "BytedToken" not in created
    assert await get_identity("another-user", created["identity_id"]) is None

    async with get_db_session() as db:
        stored = await db.get(VideoMaterialGroup, created["identity_id"])
        assert stored.provider_token == "private-byted-token"

    async def pending_call(_target_value, action, body):
        assert action == "GetVisualValidateResult"
        assert body == {"BytedToken": "private-byted-token"}
        raise MaterialProviderError(
            "素材组不存在或Token无效",
            code="VisualValidatePending",
            status=200,
        )

    monkeypatch.setattr(material_mod, "call_material_api", pending_call)
    pending = await refresh_liveness_session(user_id, created["identity_id"])
    assert pending["status"] == "awaiting_user"

    async def active_call(_target_value, action, body):
        assert action == "GetVisualValidateResult"
        assert body == {"BytedToken": "private-byted-token"}
        return {"GroupId": "group-verified-person"}

    monkeypatch.setattr(material_mod, "call_material_api", active_call)
    active = await refresh_liveness_session(user_id, created["identity_id"])
    assert active["status"] == "active"
    assert active["provider_group_id"] == "group-verified-person"
    assert active["authorization_url"] is None
    assert active["qr_code"] is None

    async with get_db_session() as db:
        stored = await db.get(VideoMaterialGroup, created["identity_id"])
        assert stored.provider_token is None


@pytest.mark.asyncio
async def test_verified_asset_is_uploaded_once_and_reused(monkeypatch):
    import core.oss

    user_id = await _user("asset")
    suffix = uuid4().hex[:12]
    source_id = f"asset_source_{suffix}"
    identity_id = f"identity_{suffix}"
    provider_group_id = f"group-verified-{suffix}"
    provider_asset_id = f"asset-provider-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=source_id,
                user_id=user_id,
                session_id=None,
                project_id=None,
                name="portrait.png",
                oss_key=f"assets/{user_id}/{source_id}/portrait.png",
                mime="image/png",
                size=1024,
                status="ready",
                source="user",
                transient=False,
                created_at=now,
            )
        )
        db.add(
            VideoMaterialGroup(
                id=identity_id,
                user_id=user_id,
                provider="doubao",
                project_name="default",
                group_type="LivenessFace",
                label="主持人本人",
                provider_group_id=provider_group_id,
                status="active",
                provider_token=None,
                authorization_url=None,
                qr_code=None,
                error=None,
                expires_at=None,
                authorized_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    class FakeOss:
        @staticmethod
        def presign_get(key, expires_sec):
            assert key.endswith("/portrait.png")
            assert expires_sec == 600
            return "https://oss.test/private-portrait.png?signature=test"

    calls: list[tuple[str, dict]] = []

    async def provider_call(_target_value, action, body):
        calls.append((action, body))
        if action == "CreateAsset":
            assert body == {
                "GroupId": provider_group_id,
                "URL": "https://oss.test/private-portrait.png?signature=test",
                "Name": "openbox-portrait.png",
                "AssetType": "Image",
            }
            return {"Id": provider_asset_id}
        assert action == "GetAsset"
        assert body == {"Id": provider_asset_id}
        return {"Id": provider_asset_id, "Status": "Active"}

    monkeypatch.setattr(material_mod, "configured_material_target", _target)
    monkeypatch.setattr(material_mod, "call_material_api", provider_call)
    monkeypatch.setattr(core.oss, "get_oss", lambda: FakeOss())

    first = await ensure_material_asset(user_id, source_id, identity_id=identity_id)
    second = await ensure_material_asset(user_id, source_id, identity_id=identity_id)

    assert first["status"] == "active"
    assert first["provider_uri"] == f"asset://{provider_asset_id}"
    assert second["material_asset_id"] == first["material_asset_id"]
    assert [action for action, _body in calls] == ["CreateAsset", "GetAsset"]

    async with get_db_session() as db:
        stored = await db.get(VideoMaterialAsset, first["material_asset_id"])
        assert stored.user_id == user_id
        assert stored.group_id == identity_id


@pytest.mark.asyncio
async def test_aigc_material_group_is_private_per_user_and_reused(monkeypatch):
    user_id = await _user("aigc")
    suffix = uuid4().hex[:12]
    provider_group_id = f"group-aigc-{suffix}"
    calls: list[tuple[str, dict]] = []

    async def provider_call(_target_value, action, body):
        calls.append((action, body))
        if action == "ListAssetGroups":
            assert body["Filter"]["GroupType"] == "AIGC"
            assert body["Filter"]["Name"].startswith("openbox-aigc-")
            assert body["ProjectName"] == "default"
            return {"Items": []}
        assert action == "CreateAssetGroup"
        assert body == {
            "Name": calls[0][1]["Filter"]["Name"],
            "Description": "OpenBox user-scoped generated-video references",
        }
        return {"Id": provider_group_id}

    monkeypatch.setattr(material_mod, "call_material_api", provider_call)
    first = await material_mod._ensure_aigc_group(user_id, _target())
    second = await material_mod._ensure_aigc_group(user_id, _target())

    assert first.provider_group_id == provider_group_id
    assert second.id == first.id
    assert [action for action, _body in calls] == ["ListAssetGroups", "CreateAssetGroup"]


@pytest.mark.asyncio
async def test_real_character_uses_liveness_group_and_other_inputs_use_aigc(monkeypatch):
    portrait = SimpleNamespace(id="asset_portrait", mime="image/png")
    scene = SimpleNamespace(id="asset_scene", mime="video/mp4")
    calls: list[tuple[str, str | None]] = []

    async def materialize(_user_id, asset_id, *, identity_id=None):
        calls.append((asset_id, identity_id))
        provider_id = f"asset-provider-{asset_id.removeprefix('asset_')}"
        return (
            f"asset://{provider_id}",
            {
                "material_asset_id": f"binding-{asset_id}",
                "provider_asset_id": provider_id,
                "identity_id": identity_id or "group-aigc",
            },
        )

    monkeypatch.setattr(material_mod, "materialize_generation_asset", materialize)

    content, bindings = await production_tool_mod._materialize_provider_inputs(
        [portrait, scene],
        portrait,
        character_reference_type="real_person",
        character_identity_id="identity-real",
        ctx=SimpleNamespace(user_id="user-real"),
    )

    assert calls == [
        ("asset_portrait", "identity-real"),
        ("asset_scene", None),
    ]
    assert content[0]["image_url"]["url"] == "asset://asset-provider-portrait"
    assert content[1]["video_url"]["url"] == "asset://asset-provider-scene"
    assert [item["group_type"] for item in bindings] == ["LivenessFace", "AIGC"]


@pytest.mark.asyncio
async def test_identity_tool_exposes_h5_only_as_public_ui_metadata(monkeypatch):
    identity = {
        "identity_id": "identity-safe",
        "label": "主持人本人",
        "provider": "doubao",
        "group_type": "LivenessFace",
        "status": "awaiting_user",
        "provider_group_id": None,
        "authorization_url": "https://api.tokenspace.test/real-validate?token=public-h5",
        "qr_code": "data:image/png;base64,dGVzdA==",
        "expires_at": "2026-08-27T12:00:00+00:00",
        "authorized_at": None,
        "created_at": "2026-08-27T11:55:00+00:00",
        "updated_at": "2026-08-27T11:55:00+00:00",
        "error": None,
    }

    async def fake_create(_user_id, _label):
        return identity

    monkeypatch.setattr(identity_tool_mod, "create_liveness_session", fake_create)
    result = await execute_video_identity(
        VideoIdentityArgs(action="create", label="主持人本人"),
        ToolContext(
            session_id="session-safe",
            user_id="user-safe",
            message_id="message-safe",
            part_id="part-safe",
        ),
    )

    assert "public-h5" not in result.output
    assert "BytedToken" not in result.output
    persisted = persisted_tool_metadata(result.metadata)
    assert persisted["identity"]["authorization_url"].endswith("public-h5")
    assert "provider_token" not in persisted["identity"]


@pytest.mark.asyncio
async def test_real_person_segment_plan_requires_owned_active_material(monkeypatch):
    user_id = await _user("plan")
    suffix = uuid4().hex[:12]
    portrait_id = f"asset_portrait_{suffix}"
    identity_id = f"identity_plan_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=portrait_id,
                user_id=user_id,
                session_id=None,
                project_id=None,
                name="host.png",
                oss_key=f"assets/{user_id}/{portrait_id}/host.png",
                mime="image/png",
                size=1024,
                status="ready",
                source="user",
                transient=False,
                created_at=now,
            )
        )
        db.add(
            VideoMaterialGroup(
                id=identity_id,
                user_id=user_id,
                provider="doubao",
                project_name="default",
                group_type="LivenessFace",
                label="真人主持人",
                provider_group_id=f"group-plan-{suffix}",
                status="active",
                provider_token=None,
                authorization_url=None,
                qr_code=None,
                error=None,
                expires_at=None,
                authorized_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    async def approve_first(*, questions, **_kwargs):
        return [[questions[0].options[0].label]]

    monkeypatch.setattr("question.question.ask", approve_first)
    ctx = ToolContext(
        session_id=f"session_{suffix}",
        user_id=user_id,
        message_id="message-plan",
        part_id="part-plan",
    )
    created = await execute_project(
        VideoProjectArgs(
            action="create",
            title="真人口播",
            brief="测试真人授权素材绑定",
            target_duration_seconds=15,
        ),
        ctx,
    )
    production_id = created.metadata["production_id"]
    script = "今天分享一个简单实用的旅行建议"
    await execute_project(
        VideoProjectArgs(
            action="set_script",
            production_id=production_id,
            script_text=script,
        ),
        ctx,
    )
    await execute_project(
        VideoProjectArgs(
            action="request_approval",
            production_id=production_id,
            approval_kind="script",
        ),
        ctx,
    )
    anchor = "参考图片1中的真人主持人在明亮演播室，服装、灯光和机位全程一致"
    prompt = (
        f"固定镜头中景，{anchor}，面对镜头开口说出@{script}，"
        "手势随语气自然舒展，语气亲切，无字幕"
    )
    args = VideoProjectArgs(
        action="set_segments",
        production_id=production_id,
        visual_anchor=anchor,
        character_reference_asset=portrait_id,
        character_reference_type="real_person",
        character_identity_id=identity_id,
        segments=[
            SegmentSpec(
                ordinal=1,
                role="hook",
                script_text=script,
                prompt=prompt,
            )
        ],
    )

    blocked = await execute_project(args, ctx)
    assert blocked.title == "真人参考素材尚未入库"

    async with get_db_session() as db:
        db.add(
            VideoMaterialAsset(
                id=f"material_binding_{suffix}",
                user_id=user_id,
                group_id=identity_id,
                source_asset_id=portrait_id,
                provider_asset_id=f"asset-plan-{suffix}",
                asset_type="Image",
                status="active",
                error=None,
                created_at=now,
                updated_at=now,
            )
        )

    planned = await execute_project(args, ctx)
    assert planned.metadata["character_reference_type"] == "real_person"
    assert planned.metadata["character_identity_id"] == identity_id
    assert f"character_identity_id={identity_id}" in planned.output
