"""TokenSpace material-library and verified-person orchestration.

The browser never receives the provider API key or ``BytedToken``. It receives
only the provider-hosted H5 URL/QR that the signed-in user must intentionally
open. Ordinary references are materialized into a per-user AIGC group; a real
person reference can only use an active, user-owned LivenessFace group.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.identifier import ascending
from core.log import create_logger

log = create_logger("video.materials")

_MATERIAL_API_VERSION = "2024-01-01"
#: Serves /v1/videos but not TokenSpace's /api/material (nginx 404, HTML body).
_RELAY_HOST_WITHOUT_MATERIALS = "openapi.bossipai.com.cn"
_IMAGE_LIMIT = 30 * 1024 * 1024
_VIDEO_LIMIT = 50 * 1024 * 1024
_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v"}


@dataclass(frozen=True)
class MaterialTarget:
    provider: str
    api_key: str
    base_url: str
    project_name: str
    request_timeout_seconds: int
    poll_interval_seconds: float
    liveness_session_ttl_seconds: int
    input_url_ttl_seconds: int


class MaterialProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "material_provider_error", status: int = 502):
        super().__init__(message[:1000])
        self.code = code
        self.status = status


class RealPersonAuthorizationRequired(RuntimeError):
    """A virtual/AIGC upload was rejected because the reference is a person."""


def _looks_like_liveness_restriction(value: object) -> bool:
    message = str(value).casefold()
    return any(
        marker in message
        for marker in (
            "真人",
            "隐私",
            "活体",
            "real person",
            "real-person",
            "liveness",
            "livenessface",
            "face privacy",
        )
    )


def _authorization_required(exc: Exception) -> RealPersonAuthorizationRequired:
    return RealPersonAuthorizationRequired(
        "参考图被供应商识别为真人，不能按虚拟/AIGC 素材使用。请先调用 "
        "video_identity.create，让本人完成 H5 实名与活体授权；再调用 "
        "video_identity.status、video_identity.add_asset，并以 "
        "character_reference_type=real_person 重新提交分段方案。"
    )


def configured_material_target() -> MaterialTarget:
    import os

    from core.config import get_config

    config = get_config()
    settings = config.video_generation
    provider = config.provider.get(settings.provider)
    # Materials may live on a different origin than generation: the BossIP
    # relay serves /v1/videos but returns an nginx 404 for /api/material, so a
    # relay deployment must point the material APIs at TokenSpace explicitly.
    # A different origin is a different account, so the key moves with it.
    material_base = (
        settings.material_base_url
        or os.environ.get("DOUBAO_MATERIAL_BASE_URL", "")
    ).strip()
    material_key = (
        settings.material_api_key
        or os.environ.get("DOUBAO_MATERIAL_API_KEY", "")
    ).strip()
    api_key = material_key or (provider.api_key if provider else None) or (
        os.environ.get("DOUBAO_API_KEY", "") if settings.provider == "doubao" else ""
    )
    configured_base = material_base or (provider.base_url if provider else None) or (
        os.environ.get("DOUBAO_BASE_URL", "") if settings.provider == "doubao" else ""
    )
    base_url = configured_base.rstrip("/")
    if not api_key:
        raise RuntimeError("DOUBAO_API_KEY is empty")
    if not base_url.startswith("https://") or base_url.endswith(".html"):
        raise RuntimeError("DOUBAO_BASE_URL must be an HTTPS API origin")
    if (urlsplit(base_url).hostname or "").lower() == _RELAY_HOST_WITHOUT_MATERIALS:
        # Failing here names the misconfiguration; letting the call through
        # only produces "returned a non-JSON response" from an HTML 404.
        raise MaterialProviderError(
            "当前视频供应商指向 BossIP 中继，它只转发 /v1/videos，不提供 TokenSpace "
            "素材库接口，因此真人实名/活体功能无法使用。请把 video_generation."
            "material_base_url（或 DOUBAO_MATERIAL_BASE_URL）配置为 TokenSpace "
            "origin，并同时配置 material_api_key（或 DOUBAO_MATERIAL_API_KEY）为该 "
            "TokenSpace 账号的密钥——中继密钥在 TokenSpace 上无效。",
            code="material_api_unavailable",
            status=501,
        )
    return MaterialTarget(
        provider=settings.provider,
        api_key=api_key,
        base_url=base_url,
        project_name="default",
        request_timeout_seconds=settings.material_timeout_seconds,
        poll_interval_seconds=settings.material_poll_interval_seconds,
        liveness_session_ttl_seconds=settings.liveness_session_ttl_seconds,
        input_url_ttl_seconds=settings.provider_input_url_ttl_seconds,
    )


def _auth_header(key: str) -> str:
    return key if key.lower().startswith("bearer ") else f"Bearer {key}"


def _provider_error(payload: Any) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("ResponseMetadata")
    metadata_error = metadata.get("Error") if isinstance(metadata, dict) else None
    result = payload.get("Result")
    result_error = result.get("Error") if isinstance(result, dict) else None
    body_error = payload.get("error")
    error = next(
        (
            candidate
            for candidate in (metadata_error, result_error, body_error)
            if isinstance(candidate, dict)
        ),
        None,
    )
    if isinstance(error, dict):
        return (
            str(error.get("Code") or error.get("code") or "material_provider_error"),
            str(error.get("Message") or error.get("message") or "Material provider rejected the request"),
        )
    code = payload.get("code")
    if code not in (None, 0, "0", 200, "200"):
        return str(code), str(payload.get("message") or payload.get("msg") or "Material provider error")
    return None


async def call_material_api(
    target: MaterialTarget,
    action: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Call one TokenSpace material Action and return its Result envelope."""
    import httpx

    url = f"{target.base_url}/api/material"
    async with httpx.AsyncClient(
        timeout=target.request_timeout_seconds,
        follow_redirects=True,
    ) as client:
        response = await client.post(
            url,
            params={"Action": action, "Version": _MATERIAL_API_VERSION},
            headers={
                "Authorization": _auth_header(target.api_key),
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise MaterialProviderError(
            f"TokenSpace {action} returned a non-JSON response",
            status=response.status_code,
        ) from exc
    error = _provider_error(payload)
    if response.status_code >= 400 or error:
        code, message = error or (f"HTTP_{response.status_code}", response.reason_phrase)
        raise MaterialProviderError(message, code=code, status=response.status_code)
    result = payload.get("Result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        result = payload.get("data") if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else {}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized else None


def _clean_label(value: str) -> str:
    label = re.sub(r"\s+", " ", (value or "真人主持人").strip())
    return (label or "真人主持人")[:120]


def _aigc_label(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return f"openbox-aigc-{digest}"


def _public_group(row) -> dict[str, Any]:
    expires_at = _utc(row.expires_at)
    expired = bool(
        row.status == "awaiting_user"
        and expires_at
        and expires_at <= datetime.now(timezone.utc)
    )
    status = "expired" if expired else row.status
    waiting = status == "awaiting_user"
    return {
        "identity_id": row.id,
        "label": row.label,
        "provider": row.provider,
        "group_type": row.group_type,
        "status": status,
        "provider_group_id": row.provider_group_id if status == "active" else None,
        "authorization_url": row.authorization_url if waiting else None,
        "qr_code": row.qr_code if waiting else None,
        "expires_at": _iso(expires_at),
        "authorized_at": _iso(row.authorized_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "error": "真人认证链接已过期，请重新创建认证" if expired else row.error,
    }


def _public_asset(row) -> dict[str, Any]:
    return {
        "material_asset_id": row.id,
        "identity_id": row.group_id,
        "source_asset_id": row.source_asset_id,
        "provider_asset_id": row.provider_asset_id if row.status == "active" else None,
        "provider_uri": (
            f"asset://{row.provider_asset_id}"
            if row.status == "active" and row.provider_asset_id
            else None
        ),
        "asset_type": row.asset_type,
        "status": row.status,
        "error": row.error,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


async def get_identity(user_id: str, identity_id: str) -> dict[str, Any] | None:
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialGroup

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.id == identity_id,
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.group_type == "LivenessFace",
                )
            )
        ).scalar_one_or_none()
        return _public_group(row) if row else None


async def list_identities(user_id: str) -> list[dict[str, Any]]:
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialGroup

    async with get_db_session() as db:
        rows = list(
            (
                await db.execute(
                    select(VideoMaterialGroup)
                    .where(
                        VideoMaterialGroup.user_id == user_id,
                        VideoMaterialGroup.group_type == "LivenessFace",
                    )
                    .order_by(VideoMaterialGroup.updated_at.desc())
                )
            ).scalars()
        )
        return [_public_group(row) for row in rows]


async def create_liveness_session(user_id: str, label: str) -> dict[str, Any]:
    """Create or safely reuse one short-lived H5 authorization session."""
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialGroup

    target = configured_material_target()
    clean_label = _clean_label(label)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        existing = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.provider == target.provider,
                    VideoMaterialGroup.group_type == "LivenessFace",
                    VideoMaterialGroup.label == clean_label,
                )
            )
        ).scalar_one_or_none()
        if existing and existing.status == "active":
            return _public_group(existing)
        if (
            existing
            and existing.status == "awaiting_user"
            and _utc(existing.expires_at)
            and _utc(existing.expires_at) > now
            and existing.authorization_url
        ):
            return _public_group(existing)

    result = await call_material_api(target, "CreateVisualValidateSession", {})
    token = str(result.get("BytedToken") or "").strip()
    authorization_url = str(result.get("H5Link") or "").strip()
    qr_code = str(result.get("QrCode") or "").strip() or None
    if not token or not authorization_url.startswith("https://"):
        raise RuntimeError("TokenSpace did not return a valid real-person authorization session")
    if qr_code and not qr_code.startswith("data:image/"):
        qr_code = None
    raw_ttl = result.get("ExpiresIn")
    ttl = int(raw_ttl) if isinstance(raw_ttl, (int, float)) and int(raw_ttl) > 0 else target.liveness_session_ttl_seconds
    expires_at = now + timedelta(seconds=ttl)

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.provider == target.provider,
                    VideoMaterialGroup.group_type == "LivenessFace",
                    VideoMaterialGroup.label == clean_label,
                )
            )
        ).scalar_one_or_none()
        if not row:
            row = VideoMaterialGroup(
                id=ascending("identity"),
                user_id=user_id,
                provider=target.provider,
                project_name=target.project_name,
                group_type="LivenessFace",
                label=clean_label,
                provider_group_id=None,
                status="awaiting_user",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        row.status = "awaiting_user"
        row.provider_token = token
        row.authorization_url = authorization_url
        row.qr_code = qr_code
        row.expires_at = expires_at
        row.authorized_at = None
        row.provider_group_id = None
        row.error = None
        row.updated_at = now
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise RuntimeError("A真人认证 session for this label was created concurrently")
        return _public_group(row)


def _pending_validation_error(exc: MaterialProviderError) -> bool:
    message = str(exc).casefold()
    code = exc.code.casefold()
    return any(
        value in message or value in code
        for value in (
            "素材组不存在",
            "token无效",
            "validatepending",
            "pending",
            "not found",
            "not exist",
        )
    )


async def refresh_liveness_session(user_id: str, identity_id: str) -> dict[str, Any]:
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialGroup

    target = configured_material_target()
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.id == identity_id,
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.group_type == "LivenessFace",
                )
            )
        ).scalar_one_or_none()
        if not row:
            raise RuntimeError("真人身份不存在或不属于当前用户")
        if row.status == "active":
            return _public_group(row)
        expires_at = _utc(row.expires_at)
        if not row.provider_token or (expires_at and now >= expires_at):
            row.status = "expired"
            row.provider_token = None
            row.authorization_url = None
            row.qr_code = None
            row.error = "真人认证链接已过期，请重新创建认证"
            row.updated_at = now
            return _public_group(row)
        provider_token = row.provider_token

    try:
        result = await call_material_api(
            target,
            "GetVisualValidateResult",
            {"BytedToken": provider_token},
        )
    except MaterialProviderError as exc:
        if _pending_validation_error(exc):
            current = await get_identity(user_id, identity_id)
            if not current:
                raise RuntimeError("真人身份状态丢失") from exc
            return current
        async with get_db_session() as db:
            row = await db.get(VideoMaterialGroup, identity_id)
            if row and row.user_id == user_id:
                row.status = "failed"
                row.provider_token = None
                row.authorization_url = None
                row.qr_code = None
                row.error = str(exc)[:1000]
                row.updated_at = now
                return _public_group(row)
        raise

    provider_group_id = str(result.get("GroupId") or result.get("Id") or "").strip()
    if not provider_group_id:
        current = await get_identity(user_id, identity_id)
        if not current:
            raise RuntimeError("真人身份状态丢失")
        return current
    if not provider_group_id.startswith("group-"):
        raise RuntimeError("TokenSpace returned an invalid LivenessFace group ID")

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.id == identity_id,
                    VideoMaterialGroup.user_id == user_id,
                )
            )
        ).scalar_one()
        row.provider_group_id = provider_group_id
        row.status = "active"
        row.provider_token = None
        row.authorization_url = None
        row.qr_code = None
        row.error = None
        row.authorized_at = now
        row.updated_at = now
        return _public_group(row)


async def _ensure_aigc_group(user_id: str, target: MaterialTarget):
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialGroup

    label = _aigc_label(user_id)
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.provider == target.provider,
                    VideoMaterialGroup.group_type == "AIGC",
                    VideoMaterialGroup.label == label,
                    VideoMaterialGroup.status == "active",
                )
            )
        ).scalar_one_or_none()
        if row and row.provider_group_id:
            return row

    listed = await call_material_api(
        target,
        "ListAssetGroups",
        {
            "Filter": {"GroupType": "AIGC", "Name": label},
            "PageNumber": 1,
            "PageSize": 100,
            "ProjectName": target.project_name,
        },
    )
    items = listed.get("Items") if isinstance(listed.get("Items"), list) else []
    provider_group_id = next(
        (
            str(item.get("Id"))
            for item in items
            if isinstance(item, dict)
            and item.get("Name") == label
            and item.get("GroupType") == "AIGC"
            and str(item.get("Id") or "").startswith("group-")
        ),
        "",
    )
    if not provider_group_id:
        created = await call_material_api(
            target,
            "CreateAssetGroup",
            {
                "Name": label,
                "Description": "OpenBox user-scoped generated-video references",
            },
        )
        provider_group_id = str(created.get("Id") or "")
    if not provider_group_id.startswith("group-"):
        raise RuntimeError("TokenSpace did not return a valid AIGC material group")

    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.provider == target.provider,
                    VideoMaterialGroup.group_type == "AIGC",
                    VideoMaterialGroup.label == label,
                )
            )
        ).scalar_one_or_none()
        if not row:
            row = VideoMaterialGroup(
                id=ascending("material_group"),
                user_id=user_id,
                provider=target.provider,
                project_name=target.project_name,
                group_type="AIGC",
                label=label,
                provider_group_id=provider_group_id,
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        else:
            row.provider_group_id = provider_group_id
            row.status = "active"
            row.error = None
            row.updated_at = now
        await db.flush()
        return row


def _asset_type(source) -> str:
    if source.mime.startswith("image/"):
        if source.size > _IMAGE_LIMIT:
            raise RuntimeError("参考图片超过 TokenSpace 30 MB 素材限制")
        return "Image"
    if source.mime in _VIDEO_MIMES:
        if source.size > _VIDEO_LIMIT:
            raise RuntimeError("参考视频超过 TokenSpace 50 MB 素材限制")
        return "Video"
    raise RuntimeError("TokenSpace video materials must be image or video assets")


async def _owned_source_asset(user_id: str, source_asset_id: str):
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    value = source_asset_id[6:] if source_asset_id.startswith("asset:") else source_asset_id
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(FileAsset).where(
                    FileAsset.id == value,
                    FileAsset.user_id == user_id,
                    FileAsset.status == "ready",
                    FileAsset.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if not row:
            raise RuntimeError("素材不存在、尚未上传完成，或不属于当前用户")
        return row


async def get_active_material_binding(
    user_id: str,
    identity_id: str,
    source_asset_id: str,
) -> dict[str, Any] | None:
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialAsset, VideoMaterialGroup

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(VideoMaterialAsset)
                .join(VideoMaterialGroup, VideoMaterialGroup.id == VideoMaterialAsset.group_id)
                .where(
                    VideoMaterialAsset.user_id == user_id,
                    VideoMaterialAsset.group_id == identity_id,
                    VideoMaterialAsset.source_asset_id == source_asset_id,
                    VideoMaterialAsset.status == "active",
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.status == "active",
                )
            )
        ).scalar_one_or_none()
        return _public_asset(row) if row else None


async def list_identity_assets(user_id: str, identity_id: str) -> list[dict[str, Any]]:
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialAsset, VideoMaterialGroup

    async with get_db_session() as db:
        group = (
            await db.execute(
                select(VideoMaterialGroup).where(
                    VideoMaterialGroup.id == identity_id,
                    VideoMaterialGroup.user_id == user_id,
                    VideoMaterialGroup.group_type == "LivenessFace",
                )
            )
        ).scalar_one_or_none()
        if not group:
            raise RuntimeError("真人身份不存在或不属于当前用户")
        rows = list(
            (
                await db.execute(
                    select(VideoMaterialAsset)
                    .where(
                        VideoMaterialAsset.user_id == user_id,
                        VideoMaterialAsset.group_id == identity_id,
                    )
                    .order_by(VideoMaterialAsset.updated_at.desc())
                )
            ).scalars()
        )
        return [_public_asset(row) for row in rows]


async def ensure_material_asset(
    user_id: str,
    source_asset_id: str,
    *,
    identity_id: str | None = None,
) -> dict[str, Any]:
    """Upload one owned OSS source into AIGC or an active LivenessFace group."""
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.video_material import VideoMaterialAsset, VideoMaterialGroup

    target = configured_material_target()
    source = await _owned_source_asset(user_id, source_asset_id)
    asset_type = _asset_type(source)
    if identity_id:
        async with get_db_session() as db:
            group = (
                await db.execute(
                    select(VideoMaterialGroup).where(
                        VideoMaterialGroup.id == identity_id,
                        VideoMaterialGroup.user_id == user_id,
                        VideoMaterialGroup.provider == target.provider,
                        VideoMaterialGroup.group_type == "LivenessFace",
                        VideoMaterialGroup.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if not group or not group.provider_group_id:
                raise RuntimeError("真人认证尚未完成，不能上传真人参考素材")
    else:
        group = await _ensure_aigc_group(user_id, target)

    async with get_db_session() as db:
        binding = (
            await db.execute(
                select(VideoMaterialAsset).where(
                    VideoMaterialAsset.group_id == group.id,
                    VideoMaterialAsset.source_asset_id == source.id,
                    VideoMaterialAsset.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if binding and binding.status == "active" and binding.provider_asset_id:
            return _public_asset(binding)
        provider_asset_id = binding.provider_asset_id if binding else None

    if provider_asset_id:
        try:
            current = await call_material_api(target, "GetAsset", {"Id": provider_asset_id})
            state = str(current.get("Status") or "").casefold()
            if state == "active":
                async with get_db_session() as db:
                    binding = await db.get(VideoMaterialAsset, binding.id)
                    binding.status = "active"
                    binding.error = None
                    binding.updated_at = datetime.now(timezone.utc)
                    return _public_asset(binding)
            if state == "failed":
                provider_asset_id = None
        except MaterialProviderError:
            provider_asset_id = None

    now = datetime.now(timezone.utc)
    if not provider_asset_id:
        oss = get_oss()
        source_url = oss.presign_get(source.oss_key, expires_sec=target.input_url_ttl_seconds)
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", source.name).strip("._") or source.id
        try:
            created = await call_material_api(
                target,
                "CreateAsset",
                {
                    "GroupId": group.provider_group_id,
                    "URL": source_url,
                    "Name": f"openbox-{safe_name[:96]}",
                    "AssetType": asset_type,
                },
            )
        except MaterialProviderError as exc:
            if identity_id is None and _looks_like_liveness_restriction(exc):
                raise _authorization_required(exc) from exc
            raise
        provider_asset_id = str(created.get("Id") or "").strip()
        if not provider_asset_id.startswith("asset-"):
            raise RuntimeError("TokenSpace did not return a valid material asset ID")
        async with get_db_session() as db:
            binding = (
                await db.execute(
                    select(VideoMaterialAsset).where(
                        VideoMaterialAsset.group_id == group.id,
                        VideoMaterialAsset.source_asset_id == source.id,
                        VideoMaterialAsset.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if not binding:
                binding = VideoMaterialAsset(
                    id=ascending("material_asset"),
                    user_id=user_id,
                    group_id=group.id,
                    source_asset_id=source.id,
                    provider_asset_id=provider_asset_id,
                    asset_type=asset_type,
                    status="processing",
                    created_at=now,
                    updated_at=now,
                )
                db.add(binding)
            else:
                binding.provider_asset_id = provider_asset_id
                binding.asset_type = asset_type
                binding.status = "processing"
                binding.error = None
                binding.updated_at = now
            await db.flush()
            binding_id = binding.id
    else:
        binding_id = binding.id

    deadline = asyncio.get_running_loop().time() + target.request_timeout_seconds
    while True:
        current = await call_material_api(target, "GetAsset", {"Id": provider_asset_id})
        state = str(current.get("Status") or "").casefold()
        if state in {"active", "failed"}:
            async with get_db_session() as db:
                binding = await db.get(VideoMaterialAsset, binding_id)
                binding.status = state
                binding.error = (
                    str((current.get("Error") or {}).get("Message") or "素材审核失败")[:1000]
                    if state == "failed"
                    else None
                )
                binding.updated_at = datetime.now(timezone.utc)
                public = _public_asset(binding)
            if state == "failed":
                failure = public["error"] or "TokenSpace素材审核失败"
                if identity_id is None and _looks_like_liveness_restriction(failure):
                    raise _authorization_required(RuntimeError(failure))
                raise RuntimeError(failure)
            return public
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("TokenSpace素材处理超时，可稍后用相同素材重试，不会重复上传")
        await asyncio.sleep(target.poll_interval_seconds)


async def materialize_generation_asset(
    user_id: str,
    source_asset_id: str,
    *,
    identity_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    binding = await ensure_material_asset(
        user_id,
        source_asset_id,
        identity_id=identity_id,
    )
    uri = str(binding.get("provider_uri") or "")
    if not uri.startswith("asset://asset-"):
        raise RuntimeError("Provider material is not active")
    return uri, binding
