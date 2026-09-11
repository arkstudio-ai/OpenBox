"""Authorization-centre use cases: bind, keep alive, inspect, unbind, publish.

The only module that sees token plaintext. Rows go out as `to_public()` dicts
that never include a token or its ciphertext.
"""
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from cache import get_cache
from core.config import get_config
from core.crypto import decrypt_secret, encrypt_secret
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.platform_account import PlatformAccount
from db.models.publish_job import PublishJob
from platforms.base import TokenGrant
from platforms.douyin.publish import (
    ALLOWED_VIDEO_MIMES,
    MAX_VIDEO_BYTES,
    build_share_schema,
    clean_hashtags,
    get_share_payload,
)
from platforms.errors import PlatformApiError, PlatformAuthRequired, PlatformError, PlatformNotConfigured
from platforms.registry import get_provider

log = create_logger("platforms.service")

STATE_TTL_SECONDS = 600
#: Refresh the access token this long before it lapses.
ACCESS_REFRESH_LEAD = timedelta(days=3)
#: Renew the refresh token this long before it lapses (when renewals remain).
REFRESH_RENEW_LEAD = timedelta(days=7)
MAX_RENEWALS = 5
#: share_id lives an hour; the signed video URL must outlive the QR code.
PUBLISH_TTL_SECONDS = 60 * 60
VIDEO_URL_TTL_SECONDS = 2 * 60 * 60

_pending_states: dict[str, tuple[dict, datetime]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _aad(platform: str, kind: str) -> str:
    return f"openbox:platform:{platform}:{kind}:v1"


def _seal(platform: str, kind: str, token: str) -> str:
    return encrypt_secret(token, _aad(platform, kind))


def _open(platform: str, kind: str, ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    return decrypt_secret(ciphertext, _aad(platform, kind))


# ── OAuth state (CSRF + "who started this") ────────────────────────────────
async def _remember_state(state: str, payload: dict) -> None:
    cache = get_cache()
    if cache is not None:
        await cache.set(f"oauth:state:{state}", payload, ttl=STATE_TTL_SECONDS)
        return
    # No cache configured (bare unit tests): keep it in-process with the same TTL.
    _pending_states[state] = (payload, _now() + timedelta(seconds=STATE_TTL_SECONDS))


async def _consume_state(state: str) -> dict | None:
    cache = get_cache()
    if cache is not None:
        key = f"oauth:state:{state}"
        payload = await cache.get(key)
        if payload is not None:
            await cache.delete(key)
        return payload if isinstance(payload, dict) else None
    entry = _pending_states.pop(state, None)
    if entry is None or entry[1] < _now():
        return None
    return entry[0]


# ── Public shape ───────────────────────────────────────────────────────────
#: How long one renew_refresh_token buys, per the docs.
RENEWAL_EXTENSION = timedelta(days=30)


def estimated_expiry(row: PlatformAccount) -> datetime | None:
    """When the person will have to scan again if every automatic renewal succeeds.

    refresh_expires_at is the hard stop for the current refresh token; each of
    the remaining renewals pushes it out another 30 days. If the app has been
    told it lacks the renewal permission, the current expiry is the answer.
    """
    if row.status != "bound":
        return None
    base = _aware(row.refresh_expires_at)
    if base is None:
        return None
    if row.last_error and "renew_refresh_token" in row.last_error:
        return base
    renewals_left = max(0, MAX_RENEWALS - (row.renew_count or 0))
    return base + RENEWAL_EXTENSION * renewals_left


def to_public(row: PlatformAccount) -> dict:
    def iso(when: datetime | None) -> str | None:
        aware = _aware(when)
        return aware.isoformat() if aware else None

    return {
        "id": row.id,
        "platform": row.platform,
        "authKind": row.auth_kind,
        "estimatedExpiresAt": iso(estimated_expiry(row)),
        "externalId": row.external_id,
        "unionId": row.union_id,
        "nickname": row.nickname,
        "avatarUrl": row.avatar_url,
        "scopes": [s for s in (row.scopes or "").split(",") if s],
        "status": row.status,
        "accessExpiresAt": iso(row.access_expires_at),
        "refreshExpiresAt": iso(row.refresh_expires_at),
        "renewCount": row.renew_count,
        "renewalsLeft": max(0, MAX_RENEWALS - (row.renew_count or 0)),
        "lastRefreshAt": iso(row.last_refresh_at),
        "lastProbeAt": iso(row.last_probe_at),
        "lastOkAt": iso(row.last_ok_at),
        "lastError": row.last_error,
        "boundAt": iso(row.bound_at),
        "boundByUserId": row.bound_by_user_id,
        **_desktop_extras(row),
    }


def _desktop_extras(row: PlatformAccount) -> dict:
    if row.auth_kind != "desktop_cookie":
        return {}
    from platforms.desktop import service as desktop_service

    return desktop_service.public_extras(row)


def job_to_public(row: PublishJob) -> dict:
    def iso(when: datetime | None) -> str | None:
        aware = _aware(when)
        return aware.isoformat() if aware else None

    return {
        "id": row.id,
        "platform": row.platform,
        "platformAccountId": row.platform_account_id,
        "fileAssetId": row.file_asset_id,
        "title": row.title,
        "hashtags": list(row.hashtags or []),
        "shareId": row.share_id,
        "status": row.status,
        "itemId": row.item_id,
        "videoId": row.video_id,
        "fromOpenId": row.from_open_id,
        "error": row.error,
        "expiresAt": iso(row.expires_at),
        "publishedAt": iso(row.published_at),
        "createdAt": iso(row.created_at),
    }


# ── Bind ───────────────────────────────────────────────────────────────────
async def start_authorize(*, user_id: str, workspace_id: str, platform: str, call_app: bool = False) -> dict:
    provider = get_provider(platform)
    if not provider.info().configured:
        raise PlatformNotConfigured(f"{platform} is not configured on this deployment")
    state = secrets.token_urlsafe(32)
    await _remember_state(state, {"user_id": user_id, "workspace_id": workspace_id, "platform": platform})
    return {"authorizeUrl": provider.build_authorize_url(state, call_app=call_app), "state": state}


def _apply_grant(row: PlatformAccount, grant: TokenGrant, now: datetime) -> None:
    row.access_token_ciphertext = _seal(row.platform, "access", grant.access_token)
    if grant.refresh_token:
        row.refresh_token_ciphertext = _seal(row.platform, "refresh", grant.refresh_token)
    row.access_expires_at = grant.access_expires_at
    row.refresh_expires_at = grant.refresh_expires_at
    if grant.scopes:
        row.scopes = grant.scopes
    row.status = "bound"
    row.last_error = None
    row.last_refresh_at = now
    row.last_ok_at = now
    row.updated_at = now

async def complete_callback(*, platform: str, code: str, state: str, granted_scopes: str = "") -> PlatformAccount:
    """Turn the redirect back from the platform into a bound account row."""
    pending = await _consume_state(state)
    if not pending or pending.get("platform") != platform:
        raise PlatformError("authorization state is unknown or expired", code="PLATFORM_STATE_INVALID")
    provider = get_provider(platform)
    grant = await provider.exchange_code(code)
    if not grant.external_id or not grant.access_token:
        raise PlatformApiError(-1, "token response lacks open_id or access_token")
    if granted_scopes and not grant.scopes:
        grant.scopes = granted_scopes

    now = _now()
    workspace_id = pending["workspace_id"]
    user_id = pending["user_id"]
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(PlatformAccount).where(
                    PlatformAccount.workspace_id == workspace_id,
                    PlatformAccount.platform == platform,
                    PlatformAccount.external_id == grant.external_id,
                    PlatformAccount.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = PlatformAccount(
                id=ascending("pacc"),
                workspace_id=workspace_id,
                bound_by_user_id=user_id,
                platform=platform,
                auth_kind="oauth",
                external_id=grant.external_id,
                scopes=grant.scopes or "",
                status="bound",
                renew_count=0,
                bound_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        else:
            # A fresh scan resets the renewal budget: Douyin counts renewals
            # per grant, and this is a new grant.
            row.bound_by_user_id = user_id
            row.bound_at = now
            row.renew_count = 0
            row.deleted_at = None
        _apply_grant(row, grant, now)
        row.union_id = grant.union_id or row.union_id
        try:
            profile = await provider.fetch_profile(grant.access_token, grant.external_id)
            row.nickname = profile.nickname or row.nickname
            row.avatar_url = profile.avatar_url or row.avatar_url
            row.union_id = profile.union_id or row.union_id
            row.last_probe_at = now
        except PlatformError as exc:
            # Profile is cosmetic; the grant is what matters.
            log.warning("platform profile fetch failed platform=%s code=%s", platform, exc.code)
        await db.commit()
        await db.refresh(row)
        return row


# ── Read ───────────────────────────────────────────────────────────────────
async def list_accounts(workspace_id: str) -> list[PlatformAccount]:
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(PlatformAccount)
                .where(
                    PlatformAccount.workspace_id == workspace_id,
                    PlatformAccount.deleted_at.is_(None),
                )
                .order_by(PlatformAccount.platform.asc(), PlatformAccount.bound_at.desc())
            )
        ).scalars()
        return list(rows)


async def _owned(db, account_id: str, workspace_id: str) -> PlatformAccount:
    row = (
        await db.execute(
            select(PlatformAccount).where(
                PlatformAccount.id == account_id,
                PlatformAccount.workspace_id == workspace_id,
                PlatformAccount.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise PlatformError("platform account not found", code="PLATFORM_ACCOUNT_NOT_FOUND")
    return row


# ── Keep alive ─────────────────────────────────────────────────────────────
async def _mark_expired(db, row: PlatformAccount, reason: str, now: datetime) -> None:
    if row.status != "expired":
        await add_notification(
            db,
            workspace_id=row.workspace_id,
            user_id=None,
            kind="platform_auth_expired",
            title=f"{row.platform} 授权已失效",
            body=f"{row.nickname or row.external_id} 的授权已失效（{reason}），请到授权中心重新授权。",
            source_key=f"auth:{row.id}:expired:{now.date().isoformat()}", action_id=row.id,
        )
    row.status = "expired"
    row.last_error = reason
    row.updated_at = now
    from notifications.events import auth_blocked
    await auth_blocked(db, row)


async def keep_alive(db, row: PlatformAccount, now: datetime | None = None, *, force_access: bool = False) -> bool:
    """Refresh/renew one row in place. Returns True when the grant is usable."""
    now = now or _now()
    if row.status != "bound":
        return False
    provider = get_provider(row.platform)
    refresh_token = _open(row.platform, "refresh", row.refresh_token_ciphertext)
    refresh_expires = _aware(row.refresh_expires_at)
    if not refresh_token or (refresh_expires and refresh_expires <= now):
        await _mark_expired(db, row, "refresh_token expired", now)
        return False

    try:
        access_expires = _aware(row.access_expires_at)
        if force_access or access_expires is None or access_expires - now <= ACCESS_REFRESH_LEAD:
            grant = await provider.refresh_access(refresh_token, refresh_expires_at=refresh_expires)
            _apply_grant(row, grant, now)
            refresh_expires = _aware(row.refresh_expires_at)

        if (
            refresh_expires is not None
            and refresh_expires - now <= REFRESH_RENEW_LEAD
            and (row.renew_count or 0) < MAX_RENEWALS
        ):
            try:
                new_refresh, new_expiry = await provider.renew_refresh(refresh_token)
            except PlatformApiError as exc:
                if exc.platform_code == 10004:
                    # App lacks renew_refresh_token; the grant simply ends at
                    # refresh_expires_at. Remember why so the page can say so.
                    row.last_error = "renew_refresh_token not permitted for this app"
                else:
                    raise
            else:
                if new_refresh:
                    row.refresh_token_ciphertext = _seal(row.platform, "refresh", new_refresh)
                    row.refresh_expires_at = new_expiry
                    row.renew_count = (row.renew_count or 0) + 1
                    row.last_refresh_at = now
                    row.last_error = None
        row.last_ok_at = now
        row.updated_at = now
        return True
    except PlatformAuthRequired as exc:
        await _mark_expired(db, row, str(exc), now)
        return False
    except PlatformApiError as exc:
        row.last_error = str(exc)
        row.updated_at = now
        log.warning("platform keep-alive failed account=%s code=%s", row.id, exc.platform_code)
        return False


async def probe(account_id: str, workspace_id: str) -> PlatformAccount:
    """Prove the grant still works by reading the profile; refresh on the way."""
    now = _now()
    async with get_db_session() as db:
        row = await _owned(db, account_id, workspace_id)
        if row.auth_kind == "desktop_cookie":
            from platforms.desktop import service as desktop_service

            return await desktop_service.probe_account(row)
        row.last_probe_at = now
        if row.status == "bound":
            provider = get_provider(row.platform)
            access = _open(row.platform, "access", row.access_token_ciphertext)
            access_expires = _aware(row.access_expires_at)
            if not access or access_expires is None or access_expires <= now:
                await keep_alive(db, row, now, force_access=True)
                access = _open(row.platform, "access", row.access_token_ciphertext)
            if row.status == "bound" and access:
                try:
                    profile = await provider.fetch_profile(access, row.external_id)
                    row.nickname = profile.nickname or row.nickname
                    row.avatar_url = profile.avatar_url or row.avatar_url
                    row.union_id = profile.union_id or row.union_id
                    row.last_ok_at = now
                    row.last_error = None
                except PlatformAuthRequired:
                    # The access token died early (person revoked in-app, or
                    # the platform pruned it). One refresh attempt, then give up.
                    if await keep_alive(db, row, now, force_access=True):
                        access = _open(row.platform, "access", row.access_token_ciphertext)
                        try:
                            profile = await provider.fetch_profile(access or "", row.external_id)
                            row.nickname = profile.nickname or row.nickname
                            row.avatar_url = profile.avatar_url or row.avatar_url
                            row.last_ok_at = now
                        except PlatformAuthRequired as exc:
                            await _mark_expired(db, row, str(exc), now)
                except PlatformApiError as exc:
                    row.last_error = str(exc)
            await keep_alive(db, row, now)
        row.updated_at = now
        await db.commit()
        await db.refresh(row)
        return row


async def refresh_account(account_id: str, workspace_id: str) -> PlatformAccount:
    now = _now()
    async with get_db_session() as db:
        row = await _owned(db, account_id, workspace_id)
        await keep_alive(db, row, now, force_access=True)
        await db.commit()
        await db.refresh(row)
        return row


async def unbind(account_id: str, workspace_id: str) -> PlatformAccount:
    now = _now()
    async with get_db_session() as db:
        row = await _owned(db, account_id, workspace_id)
        row.status = "revoked"
        row.access_token_ciphertext = None
        row.refresh_token_ciphertext = None
        row.deleted_at = now
        row.updated_at = now
        await db.commit()
        await db.refresh(row)
        return row


async def refresh_due() -> None:
    """Internal task: keep every bound grant alive before it lapses."""
    now = _now()
    async with get_db_session() as db:
        stmt = (
            select(PlatformAccount)
            .where(
                PlatformAccount.status == "bound",
                PlatformAccount.auth_kind == "oauth",
                PlatformAccount.deleted_at.is_(None),
            )
            .order_by(PlatformAccount.access_expires_at.asc())
            .limit(500)
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        rows = list((await db.execute(stmt)).scalars())
        touched = 0
        for row in rows:
            access_due = (_aware(row.access_expires_at) or now) - now <= ACCESS_REFRESH_LEAD
            refresh_due_soon = (
                _aware(row.refresh_expires_at) is not None
                and _aware(row.refresh_expires_at) - now <= REFRESH_RENEW_LEAD
            )
            if not (access_due or refresh_due_soon):
                continue
            touched += 1
            await keep_alive(db, row, now)
        await db.commit()
    if touched:
        log.info("platform keep-alive tick touched=%d", touched)


# ── Notifications ──────────────────────────────────────────────────────────
async def add_notification(db, *, workspace_id: str, user_id: str | None, kind: str, title: str, body: str = "",
                           source_key: str | None = None, action_id: str | None = None) -> None:
    from notifications.inbox import add_inbox, link_for
    await add_inbox(db, workspace_id=workspace_id, user_id=user_id, kind=kind, title=title, body=body,
                    source_key=source_key, link=link_for(kind, workspace_id=workspace_id, action_id=action_id))


# ── Publish (Douyin H5 share) ──────────────────────────────────────────────
def _ascii_key(job_id: str, name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "mp4"
    if ext not in ("mp4", "mov", "3gp"):
        ext = "mp4"
    return f"publish/{job_id}/video.{ext}"


async def create_publish_job(
    *,
    oss,
    user_id: str,
    workspace_id: str,
    file_asset_id: str,
    title: str = "",
    hashtags: list[str] | None = None,
    private_status: int = 0,
    download_type: int = 1,
) -> tuple[PublishJob, str]:
    """Sign a Douyin H5 share schema for one resource-centre video."""
    provider = get_provider("douyin")
    if not provider.info().configured:
        raise PlatformNotConfigured("douyin is not configured on this deployment")
    now = _now()
    async with get_db_session() as db:
        asset = (
            await db.execute(
                select(FileAsset).where(
                    FileAsset.id == file_asset_id,
                    FileAsset.workspace_id == workspace_id,
                    FileAsset.is_deleted.is_(False),
                    FileAsset.status == "ready",
                )
            )
        ).scalar_one_or_none()
        if asset is None:
            raise PlatformError("file not found in this workspace", code="PUBLISH_FILE_NOT_FOUND")
        mime = (asset.mime or "").split(";")[0].strip().lower()
        if mime not in ALLOWED_VIDEO_MIMES:
            raise PlatformError("only mp4/mov videos can be published", code="PUBLISH_FILE_TYPE")
        if (asset.size or 0) > MAX_VIDEO_BYTES:
            raise PlatformError("video exceeds Douyin's 128 MB limit", code="PUBLISH_FILE_TOO_LARGE")

        job_id = ascending("pub")
        key = asset.oss_key
        if not key.isascii():
            # iOS Douyin cannot fetch a URL with non-ASCII path segments.
            ascii_key = _ascii_key(job_id, asset.name)
            copied = await oss.copy(key, ascii_key)
            if not copied:
                raise PlatformError("could not stage the video for Douyin", code="PUBLISH_STAGE_FAILED")
            key = ascii_key
        video_url = oss.presign_get(key, expires_sec=VIDEO_URL_TTL_SECONDS)

        client = provider.client
        ticket = await client.open_ticket()
        try:
            share_id: str | None = await client.share_id(need_callback=True)
        except PlatformApiError as exc:
            # share_id only buys result tracking; publishing works without it.
            log.warning("douyin share_id unavailable code=%s", exc.platform_code)
            share_id = None

        # Prefer Douyin's own short-link schema (scope jump.basic): the QR code
        # is far less dense and the parameters are packed by the platform, not
        # by us. Fall back to the locally signed schema when the app lacks it.
        schema_source = "get_share"
        try:
            schema = await client.get_share_schema(
                get_share_payload(
                    ticket=ticket,
                    video_url=video_url,
                    share_id=share_id,
                    expire_at=int((now + timedelta(seconds=PUBLISH_TTL_SECONDS)).timestamp()),
                    title=title,
                    hashtags=hashtags or [],
                )
            )
        except PlatformError as exc:
            log.info("douyin get_share unavailable (%s); using local schema", exc)
            schema_source = "local"
            schema = build_share_schema(
                client_key=client.client_key,
                ticket=ticket,
                video_url=video_url,
                share_id=share_id,
                title=title,
                hashtags=hashtags or [],
                private_status=private_status,
                download_type=download_type,
            )
        job = PublishJob(
            id=job_id,
            workspace_id=workspace_id,
            user_id=user_id,
            platform="douyin",
            file_asset_id=asset.id,
            title=title[:255],
            hashtags=clean_hashtags(hashtags),
            share_id=share_id,
            error=None if schema_source == "get_share" else "schema_source=local",
            status="pending",
            expires_at=now + timedelta(seconds=PUBLISH_TTL_SECONDS),
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job, schema


async def get_job(job_id: str, workspace_id: str) -> PublishJob:
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(PublishJob).where(PublishJob.id == job_id, PublishJob.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise PlatformError("publish job not found", code="PUBLISH_JOB_NOT_FOUND")
        if row.status == "pending" and _aware(row.expires_at) and _aware(row.expires_at) <= _now():
            row.status = "expired"
            row.updated_at = _now()
            await db.commit()
            await db.refresh(row)
        return row


async def list_jobs(workspace_id: str, limit: int = 50) -> list[PublishJob]:
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(PublishJob)
                .where(PublishJob.workspace_id == workspace_id)
                .order_by(PublishJob.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        return list(rows)


async def handle_douyin_event(payload: dict) -> bool:
    """Apply one webhook event. Returns True when something changed."""
    event = payload.get("event")
    if event != "create_video":
        return False
    content = payload.get("content") or {}
    share_id = str(content.get("share_id") or "")
    if not share_id:
        return False
    now = _now()
    async with get_db_session() as db:
        job = (
            await db.execute(
                select(PublishJob)
                .where(PublishJob.share_id == share_id)
                .order_by((PublishJob.status == "pending").desc(), PublishJob.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            log.info("douyin create_video for unknown share_id=%s", share_id)
            return False
        if job.status == "published":
            return False
        job.status = "published"
        job.item_id = str(content.get("item_id") or "") or job.item_id
        job.video_id = str(content.get("video_id") or "") or job.video_id
        from_open_id = str(payload.get("from_user_id") or "")
        if from_open_id:
            job.from_open_id = from_open_id
            account = (
                await db.execute(
                    select(PlatformAccount).where(
                        PlatformAccount.workspace_id == job.workspace_id,
                        PlatformAccount.platform == "douyin",
                        PlatformAccount.external_id == from_open_id,
                        PlatformAccount.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if account is not None:
                job.platform_account_id = account.id
        job.published_at = now
        job.updated_at = now
        await add_notification(
            db,
            workspace_id=job.workspace_id,
            user_id=job.user_id,
            kind="publish_done",
            title="抖音投稿已发布",
            body=f"《{job.title or job.file_asset_id}》已在抖音发布。",
            source_key=f"publish:{job.id}:terminal", action_id=job.id,
        )
        from notifications.events import publish_result
        await publish_result(db, job)
        await db.commit()
        return True


def public_base_url(fallback: str = "") -> str:
    return (get_config().public_base_url or fallback).rstrip("/")
