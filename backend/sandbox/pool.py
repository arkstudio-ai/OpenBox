"""ECD pool state machine: adopt, assign, release, recycle, and retire."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import exists, func, select

from core.config import get_config
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.desktop_activation import DesktopActivation
from db.models.fleet import FleetAlert, PoolPurchase
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
_ensure_lock = asyncio.Lock()
_renew_lock = asyncio.Lock()


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


def _money(value: Any, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PoolStateError(f"{field} is not a valid amount: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise PoolStateError(f"{field} is not a valid amount: {value!r}")
    return amount


async def _clear_purchase_blocked() -> None:
    now = datetime.now(timezone.utc)
    async with get_db_session() as session:
        rows = (
            await session.execute(
                select(FleetAlert).where(
                    FleetAlert.rule == "purchase_blocked",
                    FleetAlert.resource_id == "purchase",
                    FleetAlert.resolved_at.is_(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.resolved_at = now


PAUSE_RULE = "auto_purchase_paused"
PAUSE_RESOURCE = "purchase"


async def auto_purchase_pause_state() -> dict[str, Any] | None:
    """The open churn-brake latch, or None when automatic purchasing may run.

    The latch is an operation-owned fleet alert: reconciliation never resolves
    rules outside RULE_SOURCES, so it survives snapshots until an admin resumes.
    """
    async with get_db_session() as session:
        row = await session.scalar(
            select(FleetAlert).where(
                FleetAlert.rule == PAUSE_RULE,
                FleetAlert.resource_id == PAUSE_RESOURCE,
                FleetAlert.resolved_at.is_(None),
            )
        )
        if row is None:
            return None
        detail = dict(row.detail or {})
        return {
            "paused_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
            "current": detail.get("current"),
            "threshold": detail.get("threshold"),
            "target": detail.get("target"),
        }


async def _pause_auto_purchase(
    current: int, threshold: int, target: int, actor: str | None,
) -> dict[str, Any]:
    from sandbox.fleet import Finding, open_operational_alert

    detail = {"current": current, "threshold": threshold, "target": target}
    await open_operational_alert(Finding(
        rule=PAUSE_RULE,
        severity="warn",
        resource_type="pool",
        resource_id=PAUSE_RESOURCE,
        message=(
            f"Prewarm capacity {current} exceeds {threshold}; automatic purchasing "
            "is paused until an admin resumes it"
        ),
        detail=detail,
    ))
    await _audit(actor or "system", None, "pool.auto_purchase_paused", PAUSE_RESOURCE, detail)
    log.warning(
        "Pool churn brake tripped: prewarm=%s threshold=%s; auto-purchase paused",
        current, threshold,
    )
    state = await auto_purchase_pause_state()
    return state or {"paused_at": None, **detail}


async def resume_auto_purchase(actor: str) -> dict[str, Any]:
    """Release the churn brake; returns the latch that was cleared, if any."""
    now = datetime.now(timezone.utc)
    previous = await auto_purchase_pause_state()
    async with get_db_session() as session:
        rows = (
            await session.execute(
                select(FleetAlert).where(
                    FleetAlert.rule == PAUSE_RULE,
                    FleetAlert.resource_id == PAUSE_RESOURCE,
                    FleetAlert.resolved_at.is_(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.resolved_at = now
    await _audit(actor, None, "pool.auto_purchase_resumed", PAUSE_RESOURCE, {
        "previous": previous,
    })
    return {"status": "resumed", "previous": previous}


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


async def _update_purchase(purchase_id: str, **values: Any) -> None:
    async with get_db_session() as session:
        row = await session.get(PoolPurchase, purchase_id)
        if row is None:
            raise PoolStateError(f"purchase ledger row {purchase_id} disappeared")
        for key, value in values.items():
            setattr(row, key, value)


async def _purchase_blocked(
    gate: str,
    message: str,
    actor: str | None,
    **detail: Any,
) -> dict[str, Any]:
    from sandbox.fleet import Finding, open_operational_alert

    payload = {"gate": gate, **detail}
    await open_operational_alert(Finding(
        rule="purchase_blocked",
        severity="warn",
        resource_type="pool",
        resource_id="purchase",
        message=message,
        detail=payload,
    ))
    await _audit(actor, None, "pool.purchase_blocked", "purchase", {
        "message": message,
        **payload,
    })
    return {"status": "blocked", "gate": gate, "message": message, **detail}


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
    from sandbox.browser_runtime import ensure_desktop_browser_runtime

    await ensure_desktop_browser_runtime(desktop_id)
    return {"hostname": output.splitlines()[0].strip() if output else ""}


class PoolService:
    async def ensure_prewarm(
        self,
        *,
        dry_run: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Fill the prewarm pool only after all purchase safety gates pass."""
        config = get_config()
        if not config.pool_enabled:
            return {
                "status": "disabled",
                "current": 0,
                "target": config.pool_target_prewarm,
                "gap": config.pool_target_prewarm,
                "quantity": 0,
            }

        async with _ensure_lock:
            remote = await wuying_ecd.list_fleet_desktops()
            usable = [
                item for item in remote
                if (item.get("tags") or {}).get(wuying_ecd.TAG_POOL) == "prewarm"
                and item.get("status") not in {"Expired", "Deleted", "Deleting", "Failed"}
            ]
            current = len(usable)
            target = config.pool_target_prewarm
            gap = max(0, target - current)
            # Churn brake: recycled subscriptions can pile prewarm capacity far
            # above the watermark; that is a signal to stop buying, not a
            # reason to keep the auto-purchase switch armed for the next dip.
            paused = await auto_purchase_pause_state()
            threshold = config.pool_auto_purchase_pause_above
            if (
                paused is None
                and config.pool_auto_purchase
                and threshold > 0
                and current > threshold
            ):
                paused = await _pause_auto_purchase(current, threshold, target, actor)
            base = {
                "current": current,
                "target": target,
                "gap": gap,
                "auto_purchase_paused": paused,
            }
            if gap == 0:
                await _clear_purchase_blocked()
                return {"status": "satisfied", **base, "quantity": 0}

            quote = await wuying_ecd.describe_price(
                "PrePaid",
                period=config.wuying_period,
                period_unit=config.wuying_period_unit,
            )
            unit_price = _money(quote.get("trade_price"), "trade_price")
            if unit_price <= 0:
                return {
                    **base,
                    **await _purchase_blocked(
                        "price_unavailable",
                        "Alibaba Cloud returned no positive pool purchase price",
                        actor,
                        unit_price=float(unit_price),
                    ),
                    "quantity": 0,
                }
            max_price = _money(config.pool_max_unit_price_cny, "POOL_MAX_UNIT_PRICE_CNY")
            if unit_price > max_price:
                return {
                    **base,
                    **await _purchase_blocked(
                        "unit_price",
                        "Pool purchase price exceeds POOL_MAX_UNIT_PRICE_CNY",
                        actor,
                        unit_price=float(unit_price),
                        max_unit_price=float(max_price),
                    ),
                    "quantity": 0,
                }

            balance_info = await wuying_ecd.query_account_balance()
            balance = _money(balance_info.get("available_balance"), "available_balance")
            required_balance = unit_price * Decimal(
                str(config.pool_min_account_balance_multiple)
            )
            if balance < required_balance:
                return {
                    **base,
                    **await _purchase_blocked(
                        "account_balance",
                        "Alibaba Cloud balance is below the pool purchase safety floor",
                        actor,
                        available_balance=float(balance),
                        required_balance=float(required_balance),
                        unit_price=float(unit_price),
                    ),
                    "quantity": 0,
                }

            now = datetime.now(timezone.utc)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            async with get_db_session() as session:
                purchased_today = await session.scalar(
                    select(func.coalesce(func.sum(PoolPurchase.quantity), 0)).where(
                        PoolPurchase.created_at >= start,
                        PoolPurchase.status.in_(("ordered", "created")),
                    )
                )
            purchased_today = int(purchased_today or 0)
            remaining_today = config.pool_max_purchases_per_day - purchased_today
            if remaining_today <= 0:
                return {
                    **base,
                    **await _purchase_blocked(
                        "daily_limit",
                        "Daily pool purchase limit has been reached",
                        actor,
                        purchased_today=purchased_today,
                        max_per_day=config.pool_max_purchases_per_day,
                    ),
                    "quantity": 0,
                }

            quantity = min(
                gap,
                config.pool_max_purchases_per_tick,
                remaining_today,
            )
            plan = {
                **base,
                "quantity": quantity,
                "unit_price": float(unit_price),
                "currency": quote.get("currency") or balance_info.get("currency") or "CNY",
                "purchased_today": purchased_today,
            }
            if dry_run or not config.pool_auto_purchase or paused is not None:
                await _clear_purchase_blocked()
                return {
                    "status": "paused" if paused is not None and not dry_run else "dry_run",
                    "auto_purchase": config.pool_auto_purchase,
                    **plan,
                }

            created: list[str] = []
            created_by = actor or "system"
            for _ in range(quantity):
                purchase_id = ascending("ppc")
                async with get_db_session() as session:
                    session.add(PoolPurchase(
                        id=purchase_id,
                        desktop_id=None,
                        unit_price=unit_price,
                        currency=plan["currency"],
                        quantity=1,
                        request_id=None,
                        status="ordered",
                        created_by=created_by,
                        created_at=datetime.now(timezone.utc),
                        error=None,
                    ))
                desktop_id: str | None = None
                try:
                    provisioned = await wuying_ecd.create_desktop_for_pool()
                    desktop_id = provisioned["desktop_id"]
                    await _update_purchase(
                        purchase_id,
                        desktop_id=desktop_id,
                        request_id=provisioned.get("request_id"),
                    )
                    await wuying_ecd.wait_desktop_ready(
                        desktop_id,
                        timeout_sec=900,
                        expected_image_id=config.wuying_image_id,
                    )
                    await verify_prewarm(desktop_id)
                    info = await wuying_ecd.describe_desktop(desktop_id) or {}
                    await cloud_desktop_repo.create(
                        None,
                        config.wuying_region_id,
                        status="running",
                        desktop_id=desktop_id,
                        end_user_id=None,
                        charge_type=info.get("charge_type") or "PrePaid",
                        expires_at=_expiry(info.get("expired_time")),
                        pool_state="prewarm",
                        pool="internal",
                        spec=info.get("desktop_type") or config.wuying_desktop_type,
                        golden_image_id=config.wuying_image_id,
                        tunnel_state="revoked",
                    )
                    await _update_purchase(purchase_id, status="created", error=None)
                    await _audit(actor, None, "pool.purchase", desktop_id, {
                        "purchase_id": purchase_id,
                        "unit_price": float(unit_price),
                        "currency": plan["currency"],
                        "request_id": provisioned.get("request_id"),
                    })
                    created.append(desktop_id)
                except Exception as exc:
                    await _update_purchase(
                        purchase_id,
                        desktop_id=desktop_id,
                        status="failed",
                        error=str(exc)[:2000],
                    )
                    await _purchase_blocked(
                        "create_failed",
                        "Pool desktop creation or verification failed",
                        actor,
                        purchase_id=purchase_id,
                        desktop_id=desktop_id,
                        error=str(exc)[:2000],
                    )
                    raise PoolStateError(
                        f"pool purchase {purchase_id} failed: {exc}"
                    ) from exc

            await _clear_purchase_blocked()
            return {"status": "purchased", **plan, "created": created}

    async def renew(
        self,
        desktop_id: str,
        actor: str,
        *,
        approve: bool,
    ) -> dict[str, Any]:
        """Renew one eligible pooled desktop after explicit paid-operation approval."""
        if not approve:
            raise PaidOperationApprovalRequired("renew requires approve=true")
        record = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
        if not record or record.get("pool_state") not in {"prewarm", "assigned"}:
            raise PoolStateError("only prewarm or assigned desktops can be renewed")
        if record.get("charge_type") != "PrePaid":
            raise PoolStateError("only PrePaid desktops can be renewed")
        config = get_config()
        response = await wuying_ecd.renew_desktop(
            desktop_id,
            config.wuying_period,
            config.wuying_period_unit,
            auto_pay=True,
            auto_renew=False,
        )
        refreshed = await wuying_ecd.describe_desktop(desktop_id)
        expires_at = _expiry((refreshed or {}).get("expired_time"))
        if expires_at is None:
            raise PoolStateError(
                f"desktop {desktop_id} renewed but its new expiry could not be read"
            )
        await cloud_desktop_repo.update(record["id"], expires_at=expires_at, error=None)
        await _audit(actor, record.get("workspace_id"), "pool.renew", desktop_id, {
            "order_id": response.get("order_id"),
            "expires_at": expires_at.isoformat(),
        })
        result = await cloud_desktop_repo.get(record["id"])
        if result is None:
            raise PoolStateError("renewed DB record disappeared")
        return result

    async def renew_expiring(
        self,
        *,
        dry_run: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Renew due capacity only when POOL_AUTO_RENEW is explicitly enabled."""
        config = get_config()
        if not config.pool_enabled:
            return {"status": "disabled", "due": [], "renewed": []}
        from sandbox.entitlement import subscription_sandbox_enabled
        # Workspace capacity has its own leased, entitlement-aware renewal.
        # Pool auto-renew must neither double-charge it nor renew a free tenant.
        managed_filter = [~exists(select(DesktopActivation.workspace_id).where(
            DesktopActivation.workspace_id == CloudDesktop.workspace_id,
        ))] if subscription_sandbox_enabled() else []
        deadline = datetime.now(timezone.utc) + timedelta(
            days=config.pool_renew_before_days
        )
        async with _renew_lock:
            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        select(CloudDesktop).where(
                            CloudDesktop.is_deleted.is_(False),
                            CloudDesktop.pool_state.in_(("prewarm", "assigned")),
                            CloudDesktop.charge_type == "PrePaid",
                            CloudDesktop.expires_at.is_not(None),
                            CloudDesktop.expires_at < deadline,
                            *managed_filter,
                        ).order_by(CloudDesktop.expires_at, CloudDesktop.desktop_id)
                    )
                ).scalars().all()
                due = [row.desktop_id for row in rows if row.desktop_id]
            if not due:
                return {"status": "satisfied", "due": [], "renewed": []}
            if dry_run or not config.pool_auto_renew:
                return {
                    "status": "dry_run",
                    "auto_renew": config.pool_auto_renew,
                    "due": due,
                    "renewed": [],
                }
            renewed = []
            for desktop_id in due:
                await self.renew(desktop_id, actor or "", approve=True)
                renewed.append(desktop_id)
            return {"status": "renewed", "due": due, "renewed": renewed}

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


async def run_ensure_prewarm_task() -> None:
    """Registered-task adapter; the auto-purchase flag remains the final gate."""
    if get_config().sandbox_provider != "wuying":
        return
    await pool_service.ensure_prewarm(dry_run=False)


async def run_renew_expiring_task() -> None:
    """Daily adapter; POOL_AUTO_RENEW=false keeps this read-only."""
    if get_config().sandbox_provider != "wuying":
        return
    await pool_service.renew_expiring(dry_run=False)
