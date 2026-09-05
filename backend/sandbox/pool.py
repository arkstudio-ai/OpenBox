"""ECD pool state machine: adopt, assign, release, recycle, and retire."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import get_config
from core.log import create_logger
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import wuying_ecd
from sandbox.channel import run_desktop_command, wuying_channel


log = create_logger("sandbox.pool")
STABLE_STATES = frozenset({
    "reserve", "prewarm", "assigned", "released", "recycling", "retired",
})
TRANSIENT_STATES = frozenset({"assigning"})
LEGACY_TAG_KEYS = frozenset({
    "purpose", "pool", "codex-user", "spec", "environment", "managed-by",
})
CHANNEL_CLEAR_FIELDS = {
    "channel_kind": None,
    "private_ip": None,
    "tunnel_port": None,
    "tunnel_bind": None,
    "tunnel_pubkey": None,
    "tunnel_fingerprint": None,
    "action_api_key_hash": None,
    "action_api_key_ciphertext": None,
    "tunnel_state": "revoked",
    "last_seen_at": None,
    "channel_error": None,
}


class PoolStateError(RuntimeError):
    pass


class DestructiveApprovalRequired(PoolStateError):
    pass


class PaidOperationApprovalRequired(PoolStateError):
    pass


class LegacyGatewayReleaseRequired(PoolStateError):
    pass


def _expiry(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _allowlist() -> set[str]:
    return {
        item.strip()
        for item in get_config().pool_adopt_allowlist.split(",")
        if item.strip()
    }


async def _audit(
    actor: str | None,
    workspace_id: str | None,
    action: str,
    desktop_id: str,
    detail: dict | None = None,
) -> None:
    if not actor:
        return
    from audit import record

    await record(actor, workspace_id, action, "cloud_desktop", desktop_id, detail)


async def verify_prewarm(desktop_id: str) -> dict[str, Any]:
    """Verify Running, policy group, and the minimum golden-image toolset."""
    info = await wuying_ecd.describe_desktop(desktop_id)
    if not info or info.get("status") != "Running":
        raise PoolStateError(f"desktop {desktop_id} is not Running")
    config = get_config()
    expected_image = config.wuying_image_id
    if expected_image and info.get("image_id") != expected_image:
        raise PoolStateError(
            f"desktop {desktop_id} image is {info.get('image_id')}, "
            f"expected {expected_image}"
        )
    expected_policy = config.wuying_policy_group_id
    if expected_policy and info.get("policy_group_id") != expected_policy:
        raise PoolStateError(
            f"desktop {desktop_id} policy group is {info.get('policy_group_id')}, "
            f"expected {expected_policy}"
        )
    output = await run_desktop_command(
        desktop_id,
        "set -eu; hostname; test -x /usr/local/bin/obx-display",
        timeout=60,
    )
    return {"hostname": output.splitlines()[0].strip() if output else ""}


class PoolService:
    async def claim(
        self, workspace_id: str, triggered_by_user_id: str | None
    ) -> dict | None:
        if not get_config().pool_enabled or not get_config().pool_assign_on_provision:
            return None
        return await cloud_desktop_repo.claim_prewarm(workspace_id, triggered_by_user_id)

    async def assign_claimed(
        self,
        record: dict,
        workspace_id: str,
        triggered_by_user_id: str | None,
        *,
        approve_renew: bool = False,
    ) -> dict:
        if record.get("pool_state") != "assigning" or record.get("workspace_id") != workspace_id:
            raise PoolStateError("desktop is not claimed by this workspace")
        desktop_id = record.get("desktop_id")
        if not desktop_id:
            raise PoolStateError("claimed pool record has no ECD desktop id")

        channel_attempted = False
        assigned_end_user_id: str | None = None
        cleanup_errors: list[str] = []
        try:
            end_user_id, _ = await wuying_ecd.ensure_end_user(workspace_id)
            assigned_end_user_id = end_user_id
            await wuying_ecd.modify_entitlement(desktop_id, [end_user_id])
            await wuying_ecd.tag_desktop(desktop_id, {
                wuying_ecd.TAG_WORKSPACE: workspace_id,
                wuying_ecd.TAG_USER: workspace_id,
                wuying_ecd.TAG_EU: end_user_id,
                wuying_ecd.TAG_POOL: "assigned",
            })
            expires_at = _expiry(record.get("expires_at"))
            renew_before = datetime.now(timezone.utc) + timedelta(
                days=get_config().pool_renew_before_days
            )
            if expires_at is not None and expires_at < renew_before:
                if not approve_renew:
                    raise PaidOperationApprovalRequired(
                        f"desktop {desktop_id} needs renewal before assignment"
                    )
                config = get_config()
                await wuying_ecd.renew_desktop(
                    desktop_id, config.wuying_period, config.wuying_period_unit,
                    auto_pay=True, auto_renew=False,
                )
                refreshed = await wuying_ecd.describe_desktop(desktop_id)
                expires_at = _expiry((refreshed or {}).get("expired_time"))
                await _audit(
                    triggered_by_user_id, workspace_id, "pool.renew", desktop_id,
                    {"expires_at": expires_at.isoformat() if expires_at else None},
                )
            refreshed_record = await cloud_desktop_repo.get(record["id"])
            if refreshed_record is None:
                raise PoolStateError("claimed DB record disappeared")
            # install() can fail after writing guest credentials or starting a
            # reverse tunnel, so any attempted install must be revoked during
            # rollback rather than only installs that returned successfully.
            channel_attempted = True
            installed = await wuying_channel.install(refreshed_record, rotate_key=True)
            await wuying_channel.verify(installed)
            now = datetime.now(timezone.utc)
            await cloud_desktop_repo.update(
                record["id"], pool_state="assigned", status="running",
                workspace_id=workspace_id, user_id=triggered_by_user_id,
                end_user_id=end_user_id, assigned_at=now, released_at=None,
                expires_at=expires_at, error=None,
            )
            await _audit(
                triggered_by_user_id, workspace_id, "pool.assign", desktop_id,
                {"record_id": record["id"]},
            )
            result = await cloud_desktop_repo.get(record["id"])
            if result is None:
                raise PoolStateError("assigned DB record disappeared")
            return result
        except Exception as exc:
            if channel_attempted:
                try:
                    latest = await cloud_desktop_repo.get(record["id"])
                    if latest:
                        await wuying_channel.revoke(latest)
                except Exception as cleanup_error:
                    log.warning("Could not revoke failed assignment %s: %s", desktop_id, cleanup_error)
                    cleanup_errors.append(f"channel revoke: {cleanup_error}")
            try:
                await wuying_ecd.modify_entitlement(desktop_id, [])
            except Exception as cleanup_error:
                log.warning("Could not clear failed entitlement %s: %s", desktop_id, cleanup_error)
                cleanup_errors.append(f"entitlement clear: {cleanup_error}")
            restored_state = "released" if cleanup_errors else "prewarm"
            try:
                await wuying_ecd.untag_desktop(
                    desktop_id,
                    [wuying_ecd.TAG_WORKSPACE, wuying_ecd.TAG_USER, wuying_ecd.TAG_EU],
                )
                await wuying_ecd.tag_desktop(
                    desktop_id, {wuying_ecd.TAG_POOL: restored_state}
                )
            except Exception as cleanup_error:
                log.warning("Could not restore failed assignment tags %s: %s", desktop_id, cleanup_error)
                cleanup_errors.append(f"tag restore: {cleanup_error}")
                restored_state = "released"
            await cloud_desktop_repo.update(
                record["id"], pool_state=restored_state, workspace_id=None, user_id=None,
                end_user_id=None if not cleanup_errors else assigned_end_user_id,
                assigned_at=None,
                error=("; ".join(cleanup_errors)[:2000] if cleanup_errors else str(exc)[:2000]),
            )
            from sandbox.fleet import Finding, open_operational_alert

            await open_operational_alert(Finding(
                rule="assign_failed", severity="critical", resource_type="desktop",
                resource_id=desktop_id,
                message=f"Pool assignment failed for {desktop_id}",
                detail={
                    "workspace_id": workspace_id,
                    "error": str(exc)[:2000],
                    "quarantined": bool(cleanup_errors),
                    "cleanup_errors": cleanup_errors,
                },
            ))
            raise

    async def release(self, desktop_id: str, actor: str) -> dict:
        record = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
        if not record or record.get("pool_state") != "assigned":
            raise PoolStateError("only an assigned desktop can be released")
        workspace_id = record.get("workspace_id")
        await wuying_channel.revoke(record)
        try:
            await wuying_ecd.modify_entitlement(desktop_id, [])
            end_user_id = None
        except Exception as exc:
            log.warning("ECD refused empty entitlement for %s: %s", desktop_id, exc)
            end_user_id = record.get("end_user_id")
        await wuying_ecd.untag_desktop(
            desktop_id, [wuying_ecd.TAG_WORKSPACE, wuying_ecd.TAG_USER]
        )
        await wuying_ecd.tag_desktop(desktop_id, {wuying_ecd.TAG_POOL: "released"})
        now = datetime.now(timezone.utc)
        await cloud_desktop_repo.update(
            record["id"], pool_state="released", workspace_id=None, user_id=None,
            end_user_id=end_user_id, released_at=now, assigned_at=None,
        )
        await _audit(actor, workspace_id, "pool.release", desktop_id)
        result = await cloud_desktop_repo.get(record["id"])
        if result is None:
            raise PoolStateError("released DB record disappeared")
        return result

    async def recycle(self, desktop_id: str, actor: str, *, approve: bool) -> dict:
        if not approve:
            raise DestructiveApprovalRequired("recycle requires approve=true")
        record = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
        if not record or record.get("pool_state") not in {
            "reserve", "prewarm", "released",
        }:
            raise PoolStateError("desktop is not recyclable")
        config = get_config()
        if not config.wuying_image_id:
            raise PoolStateError("WUYING_IMAGE_ID is required for recycle")
        if record.get("channel_kind"):
            await wuying_channel.revoke(record)
        await cloud_desktop_repo.update(record["id"], pool_state="recycling")
        await wuying_ecd.rebuild_desktop(desktop_id, config.wuying_image_id)
        await wuying_ecd.wait_desktop_ready(
            desktop_id, timeout_sec=900, expected_image_id=config.wuying_image_id
        )
        await wuying_ecd.modify_policy_group(desktop_id, config.wuying_policy_group_id)
        await verify_prewarm(desktop_id)
        try:
            await wuying_ecd.modify_entitlement(desktop_id, [])
        except Exception as exc:
            await cloud_desktop_repo.update(
                record["id"],
                pool_state="recycling",
                error=f"could not clear entitlement after rebuild: {exc}"[:2000],
            )
            raise PoolStateError(
                f"desktop {desktop_id} rebuilt but EndUser entitlement could not be cleared"
            ) from exc
        await wuying_ecd.untag_desktop(
            desktop_id,
            [wuying_ecd.TAG_WORKSPACE, wuying_ecd.TAG_USER, wuying_ecd.TAG_EU],
        )
        await wuying_ecd.tag_desktop(desktop_id, {
            wuying_ecd.TAG_ENV: config.wuying_env_tag,
            wuying_ecd.TAG_POOL: "prewarm",
            wuying_ecd.TAG_SPEC: config.wuying_desktop_type,
            wuying_ecd.TAG_IMAGE: config.wuying_image_id,
        })
        remote = await wuying_ecd.describe_desktop(desktop_id) or {}
        await cloud_desktop_repo.update(
            record["id"], pool_state="prewarm", workspace_id=None, user_id=None,
            end_user_id=None, assigned_at=None, released_at=None,
            status="running", error=None, golden_image_id=config.wuying_image_id,
            spec=remote.get("desktop_type") or config.wuying_desktop_type,
            charge_type=remote.get("charge_type") or record.get("charge_type"),
            expires_at=_expiry(remote.get("expired_time")), is_deleted=False, deleted_at=None,
            **CHANNEL_CLEAR_FIELDS,
        )
        await _audit(actor, None, "pool.recycle", desktop_id, {"image_id": config.wuying_image_id})
        result = await cloud_desktop_repo.get(record["id"])
        if result is None:
            raise PoolStateError("recycled DB record disappeared")
        return result

    async def retire(self, desktop_id: str, actor: str) -> dict:
        record = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
        if not record or record.get("pool_state") in {"assigned", "assigning", "recycling"}:
            raise PoolStateError("assigned or in-flight desktops cannot be retired")
        await wuying_ecd.tag_desktop(desktop_id, {wuying_ecd.TAG_POOL: "retired"})
        await cloud_desktop_repo.update(record["id"], pool_state="retired")
        await _audit(actor, None, "pool.retire", desktop_id)
        result = await cloud_desktop_repo.get(record["id"])
        if result is None:
            raise PoolStateError("retired DB record disappeared")
        return result

    async def adopt(
        self,
        desktop_id: str,
        pool_state: str,
        actor: str,
        *,
        rebuild: bool = False,
        approve: bool = False,
        gateway_release_verified: bool = False,
    ) -> dict:
        if pool_state not in {"reserve", "prewarm"}:
            raise PoolStateError("adopt state must be reserve or prewarm")
        if desktop_id not in _allowlist():
            raise PoolStateError(f"desktop {desktop_id} is not in POOL_ADOPT_ALLOWLIST")
        remote = await wuying_ecd.describe_desktop(desktop_id)
        if remote is None:
            raise PoolStateError(f"desktop {desktop_id} does not exist")
        existing = await cloud_desktop_repo.get_any_by_desktop_id(desktop_id)
        if existing and not existing.get("is_deleted") and existing.get("workspace_id"):
            raise PoolStateError("desktop is actively assigned to a workspace")
        config = get_config()
        needs_rebuild = (
            pool_state == "prewarm" and remote.get("image_id") != config.wuying_image_id
        )
        if needs_rebuild and not (rebuild and approve):
            raise DestructiveApprovalRequired(
                "desktop image differs from WUYING_IMAGE_ID; rebuild=true and approve=true required"
            )
        if rebuild and not approve:
            raise DestructiveApprovalRequired("rebuild requires approve=true")
        original_tags = await wuying_ecd.desktop_tags(desktop_id)
        original_end_user_ids = list(remote.get("end_user_ids") or [])
        legacy_slot = original_tags.get("codex-user")
        if legacy_slot:
            legacy_pool = original_tags.get("pool")
            if legacy_pool not in {"reclaim", "prewarm"}:
                raise LegacyGatewayReleaseRequired(
                    f"desktop {desktop_id} is still in bossip pool={legacy_pool or 'unknown'}; "
                    "release its gateway registration first"
                )
            if not gateway_release_verified:
                raise LegacyGatewayReleaseRequired(
                    f"desktop {desktop_id} still carries codex-user={legacy_slot}; "
                    "verify the bossip gateway registration is gone and set "
                    "gateway_release_verified=true"
                )
        staged_fields = dict(
            workspace_id=None,
            user_id=None,
            status=(remote.get("status") or "Running").lower(),
            pool_state="reserve",
            charge_type=remote.get("charge_type"),
            expires_at=_expiry(remote.get("expired_time")),
            spec=remote.get("desktop_type"),
            golden_image_id=remote.get("image_id"),
            is_deleted=False,
            deleted_at=None,
            error=None,
            **CHANNEL_CLEAR_FIELDS,
        )
        if existing:
            await cloud_desktop_repo.update(existing["id"], **staged_fields)
            record_id = existing["id"]
        else:
            staged = await cloud_desktop_repo.create(
                None,
                config.wuying_region_id,
                desktop_id=desktop_id,
                **{key: value for key, value in staged_fields.items() if key not in {"workspace_id"}},
            )
            record_id = staged["id"]

        # Reserve adoption is intentionally non-destructive: record and label
        # it now, then require a separate approved recycle before use.
        if needs_rebuild:
            await cloud_desktop_repo.update(record_id, pool_state="recycling")
            await _audit(
                actor, None, "pool.adopt_rebuild_started", desktop_id,
                {
                    "from_image": remote.get("image_id"),
                    "to_image": config.wuying_image_id,
                    "original_tags": original_tags,
                    "original_end_user_ids": original_end_user_ids,
                },
            )
            try:
                await wuying_ecd.rebuild_desktop(desktop_id, config.wuying_image_id)
                await wuying_ecd.wait_desktop_ready(
                    desktop_id,
                    timeout_sec=900,
                    expected_image_id=config.wuying_image_id,
                )
            except Exception as exc:
                await cloud_desktop_repo.update(
                    record_id, pool_state="reserve", error=str(exc)[:2000]
                )
                raise
        if pool_state == "prewarm":
            await wuying_ecd.modify_policy_group(desktop_id, config.wuying_policy_group_id)
            await verify_prewarm(desktop_id)
            try:
                await wuying_ecd.modify_entitlement(desktop_id, [])
            except Exception as exc:
                await cloud_desktop_repo.update(
                    record_id,
                    pool_state="recycling" if needs_rebuild else "reserve",
                    error=f"could not clear entitlement during adopt: {exc}"[:2000],
                )
                raise PoolStateError(
                    f"desktop {desktop_id} cannot enter prewarm while its old EndUser remains"
                ) from exc

        remove_keys = sorted(
            (set(original_tags) & LEGACY_TAG_KEYS)
            | {wuying_ecd.TAG_WORKSPACE, wuying_ecd.TAG_USER, wuying_ecd.TAG_EU}
        )
        await wuying_ecd.untag_desktop(desktop_id, remove_keys)
        await wuying_ecd.tag_desktop(desktop_id, {
            wuying_ecd.TAG_ENV: config.wuying_env_tag,
            wuying_ecd.TAG_POOL: pool_state,
            wuying_ecd.TAG_SPEC: remote.get("desktop_type") or config.wuying_desktop_type,
            wuying_ecd.TAG_IMAGE: (
                config.wuying_image_id if pool_state == "prewarm" else remote.get("image_id") or "unknown"
            ),
        })
        refreshed = await wuying_ecd.describe_desktop(desktop_id) or remote
        fields = dict(
            workspace_id=None, user_id=None, region_id=config.wuying_region_id,
            status=(refreshed.get("status") or "Running").lower(), desktop_id=desktop_id,
            end_user_id=None if pool_state == "prewarm" else None,
            charge_type=refreshed.get("charge_type"),
            expires_at=_expiry(refreshed.get("expired_time")), pool_state=pool_state,
            spec=refreshed.get("desktop_type") or config.wuying_desktop_type,
            golden_image_id=(
                config.wuying_image_id if pool_state == "prewarm" else refreshed.get("image_id")
            ),
            is_deleted=False, deleted_at=None, error=None,
            **CHANNEL_CLEAR_FIELDS,
        )
        await cloud_desktop_repo.update(record_id, **fields)
        await _audit(
            actor, None, "pool.adopt", desktop_id,
            {"pool_state": pool_state, "rebuild": rebuild, "original_tags": original_tags,
             "original_end_user_ids": original_end_user_ids,
             "gateway_release_verified": gateway_release_verified},
        )
        result = await cloud_desktop_repo.get(record_id)
        if result is None:
            raise PoolStateError("adopted DB record disappeared")
        return result


pool_service = PoolService()
