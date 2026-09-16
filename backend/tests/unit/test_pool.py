"""Pool state transitions are serialized and cloud calls are explicit."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.fleet import FleetAlert, PoolPurchase
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from db.repository.user_repo import PgUserRepo
from sandbox import pool as pool_module
from sandbox.pool import (
    DestructiveApprovalRequired,
    LegacyGatewayReleaseRequired,
    PaidOperationApprovalRequired,
    PoolService,
    PoolStateError,
    STABLE_STATES,
    TRANSIENT_STATES,
    verify_prewarm,
)


def _config(**overrides):
    values = dict(
        pool_enabled=True,
        pool_assign_on_provision=True,
        pool_adopt_allowlist="ecd-allowed",
        wuying_image_id="img-v3",
        wuying_policy_group_id="pg-1080p",
        wuying_env_tag="prod",
        wuying_desktop_type="eds.enterprise_office.6c12g",
    )
    values.update(overrides)
    return OpenBoxConfig(**values)


async def test_verify_prewarm_rejects_source_image(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def describe(_desktop_id):
        return {
            "status": "Running",
            "image_id": "img-v2",
            "policy_group_id": "pg-1080p",
        }

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("tool verification must wait for the target image")

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(pool_module, "run_desktop_command", must_not_run)
    with pytest.raises(PoolStateError, match="image is img-v2, expected img-v3"):
        await verify_prewarm("ecd-source")


async def _user(prefix: str):
    suffix = uuid.uuid4().hex[:10]
    return await PgUserRepo().create(
        id=f"{prefix}-{suffix}", username=f"{prefix}-{suffix}", password_hash="unused"
    )


async def _prewarm(desktop_id: str, expires_days: int = 30):
    return await cloud_desktop_repo.create(
        None,
        "cn-shanghai",
        status="running",
        desktop_id=desktop_id,
        pool_state="prewarm",
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_days),
        charge_type="PrePaid",
    )


def test_state_model_is_six_stable_plus_one_transient():
    assert STABLE_STATES == {
        "reserve", "prewarm", "assigned", "released", "recycling", "retired",
    }
    assert TRANSIENT_STATES == {"assigning"}


async def test_ensure_prewarm_disabled_never_calls_cloud(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config(pool_enabled=False))

    async def forbidden():
        raise AssertionError("disabled pool must not call ECD")

    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", forbidden)
    result = await PoolService().ensure_prewarm(dry_run=False)
    assert result == {
        "status": "disabled", "current": 0, "target": 5, "gap": 5, "quantity": 0,
    }


async def test_ensure_prewarm_dry_run_applies_tick_limit_without_purchase(monkeypatch):
    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(pool_target_prewarm=5, pool_auto_purchase=False),
    )

    async def desktops():
        return [
            {"status": "Running", "tags": {"openbox-pool": "prewarm"}},
            {"status": "Stopped", "tags": {"openbox-pool": "prewarm"}},
            {"status": "Running", "tags": {"openbox-pool": "prewarm"}},
            {"status": "Expired", "tags": {"openbox-pool": "prewarm"}},
            {"status": "Running", "tags": {"openbox-pool": "assigned"}},
        ]

    async def price(*_args, **_kwargs):
        return {"trade_price": 200, "currency": "CNY"}

    async def balance():
        return {"available_balance": 1000, "currency": "CNY"}

    async def forbidden():
        raise AssertionError("automatic purchase is disabled")

    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", desktops)
    monkeypatch.setattr(pool_module.wuying_ecd, "describe_price", price)
    monkeypatch.setattr(pool_module.wuying_ecd, "query_account_balance", balance)
    monkeypatch.setattr(pool_module.wuying_ecd, "create_desktop_for_pool", forbidden)

    result = await PoolService().ensure_prewarm(dry_run=False)
    assert result["status"] == "dry_run"
    assert result["current"] == 3
    assert result["gap"] == 2
    assert result["quantity"] == 1
    assert result["unit_price"] == 200


async def test_ensure_prewarm_blocks_price_gate_and_opens_alert(monkeypatch):
    async with get_db_session() as session:
        await session.execute(delete(FleetAlert))
    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(pool_target_prewarm=1, pool_max_unit_price_cny=100),
    )

    async def desktops():
        return []

    async def price(*_args, **_kwargs):
        return {"trade_price": 105.75, "currency": "CNY"}

    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", desktops)
    monkeypatch.setattr(pool_module.wuying_ecd, "describe_price", price)
    result = await PoolService().ensure_prewarm(dry_run=True)
    assert result["status"] == "blocked"
    assert result["gate"] == "unit_price"
    async with get_db_session() as session:
        alert = await session.scalar(select(FleetAlert).where(
            FleetAlert.rule == "purchase_blocked",
            FleetAlert.resolved_at.is_(None),
        ))
    assert alert is not None
    assert alert.detail["gate"] == "unit_price"


async def test_ensure_prewarm_blocks_daily_limit(monkeypatch):
    async with get_db_session() as session:
        await session.execute(delete(PoolPurchase))
        session.add(PoolPurchase(
            id=f"ppc-{uuid.uuid4().hex}", desktop_id=None,
            unit_price=200, currency="CNY", quantity=2,
            request_id=None, status="ordered", created_by="system",
            created_at=datetime.now(timezone.utc), error=None,
        ))
    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(pool_target_prewarm=1, pool_max_purchases_per_day=2),
    )

    async def desktops():
        return []

    async def price(*_args, **_kwargs):
        return {"trade_price": 200, "currency": "CNY"}

    async def balance():
        return {"available_balance": 1000, "currency": "CNY"}

    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", desktops)
    monkeypatch.setattr(pool_module.wuying_ecd, "describe_price", price)
    monkeypatch.setattr(pool_module.wuying_ecd, "query_account_balance", balance)
    result = await PoolService().ensure_prewarm(dry_run=True)
    assert result["status"] == "blocked"
    assert result["gate"] == "daily_limit"


async def test_ensure_prewarm_purchase_persists_ledger_and_desktop(monkeypatch):
    async with get_db_session() as session:
        await session.execute(delete(PoolPurchase))
        await session.execute(delete(FleetAlert))
    desktop_id = f"ecd-purchased-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(pool_target_prewarm=1, pool_auto_purchase=True),
    )

    async def desktops():
        return []

    async def price(*_args, **_kwargs):
        return {"trade_price": 200, "currency": "CNY"}

    async def balance():
        return {"available_balance": 1000, "currency": "CNY"}

    async def create():
        return {"desktop_id": desktop_id, "request_id": "req-purchase"}

    async def noop(*_args, **_kwargs):
        return None

    async def describe(_desktop_id):
        return {
            "desktop_id": desktop_id,
            "status": "Running",
            "charge_type": "PrePaid",
            "expired_time": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "desktop_type": "eds.enterprise_office.6c12g",
        }

    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", desktops)
    monkeypatch.setattr(pool_module.wuying_ecd, "describe_price", price)
    monkeypatch.setattr(pool_module.wuying_ecd, "query_account_balance", balance)
    monkeypatch.setattr(pool_module.wuying_ecd, "create_desktop_for_pool", create)
    monkeypatch.setattr(pool_module.wuying_ecd, "wait_desktop_ready", noop)
    monkeypatch.setattr(pool_module, "verify_prewarm", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)

    result = await PoolService().ensure_prewarm(dry_run=False)
    assert result["status"] == "purchased"
    assert result["created"] == [desktop_id]
    record = await cloud_desktop_repo.get_by_desktop_id(desktop_id)
    assert record["pool_state"] == "prewarm"
    assert record["workspace_id"] is None
    async with get_db_session() as session:
        purchase = (await session.execute(select(PoolPurchase))).scalar_one()
    assert purchase.status == "created"
    assert purchase.desktop_id == desktop_id
    assert purchase.request_id == "req-purchase"


async def test_concurrent_claims_never_share_a_desktop(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    first, second = await _user("claim-a"), await _user("claim-b")
    suffix = uuid.uuid4().hex[:8]
    await _prewarm(f"ecd-claim-1-{suffix}")
    await _prewarm(f"ecd-claim-2-{suffix}")

    one, two = await asyncio.gather(
        PoolService().claim(first["default_workspace_id"], first["id"]),
        PoolService().claim(second["default_workspace_id"], second["id"]),
    )
    assert one and two
    assert one["desktop_id"] != two["desktop_id"]
    assert one["pool_state"] == two["pool_state"] == "assigning"


async def test_assign_claimed_completes_entitlement_tags_and_channel(monkeypatch):
    config = _config()
    monkeypatch.setattr(pool_module, "get_config", lambda: config)
    user = await _user("assign")
    source = await _prewarm(f"ecd-assign-{uuid.uuid4().hex[:8]}")
    service = PoolService()
    claimed = await service.claim(user["default_workspace_id"], user["id"])
    assert claimed and claimed["id"] == source["id"]

    calls = []

    async def ensure(workspace_id):
        calls.append(("ensure", workspace_id))
        return "eu-1", "password"

    async def entitlement(desktop_id, users):
        calls.append(("entitlement", desktop_id, users))

    async def tags(desktop_id, values):
        calls.append(("tags", desktop_id, values))

    async def install(record, rotate_key=False):
        calls.append(("install", record["desktop_id"], rotate_key))
        return record

    async def verify(record):
        calls.append(("verify", record["desktop_id"]))

    monkeypatch.setattr(pool_module.wuying_ecd, "ensure_end_user", ensure)
    monkeypatch.setattr(pool_module.wuying_ecd, "modify_entitlement", entitlement)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", tags)
    monkeypatch.setattr(pool_module.wuying_channel, "install", install)
    monkeypatch.setattr(pool_module.wuying_channel, "verify", verify)

    result = await service.assign_claimed(
        claimed, user["default_workspace_id"], user["id"]
    )
    assert result["pool_state"] == "assigned"
    assert result["workspace_id"] == user["default_workspace_id"]
    assert result["end_user_id"] == "eu-1"
    assert ("install", result["desktop_id"], True) in calls
    assert ("verify", result["desktop_id"]) in calls


async def test_failed_assignment_restores_prewarm(monkeypatch):
    config = _config()
    monkeypatch.setattr(pool_module, "get_config", lambda: config)
    user = await _user("assign-fail")
    source = await _prewarm(f"ecd-assign-fail-{uuid.uuid4().hex[:8]}")
    service = PoolService()
    claimed = await service.claim(user["default_workspace_id"], user["id"])

    async def ensure(_workspace_id):
        return "eu-1", "password"

    async def noop(*_args, **_kwargs):
        return None

    async def fail(_record, rotate_key=False):
        raise RuntimeError("channel install failed")

    monkeypatch.setattr(pool_module.wuying_ecd, "ensure_end_user", ensure)
    monkeypatch.setattr(pool_module.wuying_ecd, "modify_entitlement", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "untag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_channel, "install", fail)

    with pytest.raises(RuntimeError, match="channel install failed"):
        await service.assign_claimed(claimed, user["default_workspace_id"], user["id"])
    restored = await cloud_desktop_repo.get(source["id"])
    assert restored["pool_state"] == "prewarm"
    assert restored["workspace_id"] is None


async def test_failed_assignment_with_uncleared_entitlement_is_quarantined(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    user = await _user("assign-quarantine")
    source = await _prewarm(f"ecd-assign-quarantine-{uuid.uuid4().hex[:8]}")
    service = PoolService()
    claimed = await service.claim(user["default_workspace_id"], user["id"])

    async def ensure(_workspace_id):
        return "eu-still-bound", "password"

    async def entitlement(_desktop_id, users):
        if not users:
            raise RuntimeError("empty entitlement rejected")

    async def noop(*_args, **_kwargs):
        return None

    async def fail(_record, rotate_key=False):
        raise RuntimeError("channel install failed")

    monkeypatch.setattr(pool_module.wuying_ecd, "ensure_end_user", ensure)
    monkeypatch.setattr(pool_module.wuying_ecd, "modify_entitlement", entitlement)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "untag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_channel, "install", fail)

    with pytest.raises(RuntimeError, match="channel install failed"):
        await service.assign_claimed(claimed, user["default_workspace_id"], user["id"])
    quarantined = await cloud_desktop_repo.get(source["id"])
    assert quarantined["pool_state"] == "released"
    assert quarantined["workspace_id"] is None
    assert quarantined["end_user_id"] == "eu-still-bound"
    assert "empty entitlement rejected" in quarantined["error"]


async def test_adopt_requires_allowlist_before_cloud_io(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def forbidden(_desktop_id):
        raise AssertionError("cloud must not be called")

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", forbidden)
    with pytest.raises(PoolStateError, match="ALLOWLIST"):
        await PoolService().adopt("ecd-not-allowed", "reserve", "admin")


async def test_prewarm_adopt_requires_explicit_rebuild_approval(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def describe(_desktop_id):
        return {"desktop_id": "ecd-allowed", "status": "Running", "image_id": "img-old"}

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    with pytest.raises(DestructiveApprovalRequired, match="approve=true"):
        await PoolService().adopt("ecd-allowed", "prewarm", "admin")


@pytest.mark.parametrize(
    ("legacy_pool", "verified", "message"),
    [
        ("trial", True, "release its gateway registration first"),
        ("prewarm", False, "gateway_release_verified=true"),
    ],
)
async def test_legacy_adopt_requires_released_pool_and_gateway_verification(
    monkeypatch, legacy_pool, verified, message
):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def describe(_desktop_id):
        return {
            "desktop_id": "ecd-allowed",
            "status": "Running",
            "image_id": "img-v3",
            "desktop_type": "eds.enterprise_office.6c12g",
        }

    async def tags(_desktop_id):
        return {"codex-user": "slot15", "pool": legacy_pool}

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(pool_module.wuying_ecd, "desktop_tags", tags)
    with pytest.raises(LegacyGatewayReleaseRequired, match=message):
        await PoolService().adopt(
            "ecd-allowed",
            "reserve",
            "admin",
            gateway_release_verified=verified,
        )


async def test_adopted_history_clears_stale_channel_credentials(monkeypatch):
    desktop_id = "ecd-allowed"
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    user = await _user("adopt-history")
    history = await cloud_desktop_repo.create(
        user["default_workspace_id"],
        "cn-shanghai",
        status="running",
        desktop_id=desktop_id,
        pool_state="assigned",
        channel_kind="ssh",
        tunnel_port=18991,
        tunnel_bind="172.17.0.1",
        tunnel_pubkey="ssh-ed25519 old",
        tunnel_fingerprint=f"SHA256:{uuid.uuid4().hex}",
        action_api_key_hash="old-hash",
        action_api_key_ciphertext="old-ciphertext",
        tunnel_state="up",
    )
    await cloud_desktop_repo.soft_delete(history["id"])

    async def describe(_desktop_id):
        return {
            "desktop_id": desktop_id,
            "status": "Running",
            "image_id": "img-v3",
            "desktop_type": "eds.enterprise_office.6c12g",
            "charge_type": "PrePaid",
            "end_user_ids": ["legacy-user"],
        }

    async def tags(_desktop_id):
        return {"purpose": "codex"}

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(pool_module.wuying_ecd, "desktop_tags", tags)
    monkeypatch.setattr(pool_module.wuying_ecd, "untag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", noop)

    adopted = await PoolService().adopt(desktop_id, "reserve", "admin")
    assert adopted["pool_state"] == "reserve"
    assert adopted["workspace_id"] is None
    assert adopted["channel_kind"] is None
    assert adopted["tunnel_port"] is None
    assert adopted["tunnel_fingerprint"] is None
    assert adopted["action_api_key_ciphertext"] is None
    assert adopted["tunnel_state"] == "revoked"


async def test_retired_desktop_cannot_be_recycled(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    record = await cloud_desktop_repo.create(
        None,
        "cn-shanghai",
        status="running",
        desktop_id=f"ecd-retired-{uuid.uuid4().hex[:8]}",
        pool_state="retired",
    )
    with pytest.raises(PoolStateError, match="not recyclable"):
        await PoolService().recycle(record["desktop_id"], "admin", approve=True)


async def test_renew_expiring_is_dry_run_until_auto_renew_enabled(monkeypatch):
    desktop_id = f"ecd-renew-preview-{uuid.uuid4().hex[:8]}"
    await _prewarm(desktop_id, expires_days=2)
    monkeypatch.setattr(
        pool_module, "get_config", lambda: _config(pool_auto_renew=False)
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("renewal must not run while POOL_AUTO_RENEW=false")

    monkeypatch.setattr(pool_module.wuying_ecd, "renew_desktop", forbidden)
    result = await PoolService().renew_expiring(dry_run=False)
    assert result["status"] == "dry_run"
    assert desktop_id in result["due"]
    assert result["renewed"] == []


async def test_renew_expiring_renews_due_capacity_and_skips_retired(monkeypatch):
    due_id = f"ecd-renew-due-{uuid.uuid4().hex[:8]}"
    retired_id = f"ecd-renew-retired-{uuid.uuid4().hex[:8]}"
    due = await _prewarm(due_id, expires_days=2)
    await cloud_desktop_repo.create(
        None,
        "cn-shanghai",
        status="running",
        desktop_id=retired_id,
        pool_state="retired",
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        charge_type="PrePaid",
    )
    monkeypatch.setattr(
        pool_module, "get_config", lambda: _config(pool_auto_renew=True)
    )
    calls = []
    renewed_until = datetime.now(timezone.utc) + timedelta(days=32)

    async def renew(desktop_id, *_args, **_kwargs):
        calls.append(desktop_id)
        return {"order_id": "order-1"}

    async def describe(desktop_id):
        return {"desktop_id": desktop_id, "expired_time": renewed_until.isoformat()}

    monkeypatch.setattr(pool_module.wuying_ecd, "renew_desktop", renew)
    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    result = await PoolService().renew_expiring(dry_run=False)
    assert result["status"] == "renewed"
    assert due_id in result["renewed"]
    assert calls == result["renewed"]
    assert retired_id not in result["due"]
    refreshed = await cloud_desktop_repo.get(due["id"])
    assert refreshed["expires_at"].replace(tzinfo=timezone.utc) == renewed_until


async def test_manual_renew_requires_explicit_approval(monkeypatch):
    desktop_id = f"ecd-renew-approval-{uuid.uuid4().hex[:8]}"
    await _prewarm(desktop_id, expires_days=2)
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    with pytest.raises(PaidOperationApprovalRequired, match="approve=true"):
        await PoolService().renew(desktop_id, "admin", approve=False)


def _prewarm_fleet(count: int):
    async def desktops():
        return [
            {"status": "Running", "tags": {"openbox-pool": "prewarm"}}
            for _ in range(count)
        ]
    return desktops


async def _clear_pause_alerts():
    async with get_db_session() as session:
        await session.execute(delete(FleetAlert).where(FleetAlert.rule == "auto_purchase_paused"))


async def test_ensure_prewarm_trips_churn_brake_above_threshold(monkeypatch):
    await _clear_pause_alerts()
    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(
            pool_target_prewarm=5, pool_auto_purchase=True, pool_auto_purchase_pause_above=10,
        ),
    )
    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", _prewarm_fleet(11))

    result = await PoolService().ensure_prewarm(dry_run=False, actor="ops")

    assert result["status"] == "satisfied"
    assert result["auto_purchase_paused"]["current"] == 11
    assert result["auto_purchase_paused"]["threshold"] == 10
    async with get_db_session() as session:
        alert = await session.scalar(select(FleetAlert).where(
            FleetAlert.rule == "auto_purchase_paused",
            FleetAlert.resolved_at.is_(None),
        ))
    assert alert is not None
    assert alert.resource_id == "purchase"
    assert alert.detail["current"] == 11


async def test_churn_brake_stays_latched_when_pool_drains_again(monkeypatch):
    await _clear_pause_alerts()
    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(
            pool_target_prewarm=5, pool_auto_purchase=True, pool_auto_purchase_pause_above=10,
        ),
    )
    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", _prewarm_fleet(12))
    await PoolService().ensure_prewarm(dry_run=False)

    async def price(*_args, **_kwargs):
        return {"trade_price": 200, "currency": "CNY"}

    async def balance():
        return {"available_balance": 1000, "currency": "CNY"}

    async def forbidden():
        raise AssertionError("a latched brake must not purchase")

    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", _prewarm_fleet(2))
    monkeypatch.setattr(pool_module.wuying_ecd, "describe_price", price)
    monkeypatch.setattr(pool_module.wuying_ecd, "query_account_balance", balance)
    monkeypatch.setattr(pool_module.wuying_ecd, "create_desktop_for_pool", forbidden)

    latched = await PoolService().ensure_prewarm(dry_run=False)
    assert latched["status"] == "paused"
    assert latched["gap"] == 3
    assert latched["quantity"] == 1
    assert latched["auto_purchase_paused"]["current"] == 12

    preview = await PoolService().ensure_prewarm(dry_run=True)
    assert preview["status"] == "dry_run"
    assert preview["auto_purchase_paused"] is not None

    resumed = await pool_module.resume_auto_purchase("ops")
    assert resumed["status"] == "resumed"
    assert resumed["previous"]["current"] == 12
    assert await pool_module.auto_purchase_pause_state() is None


async def test_churn_brake_ignores_disabled_threshold_and_manual_mode(monkeypatch):
    await _clear_pause_alerts()
    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(
            pool_target_prewarm=5, pool_auto_purchase=True, pool_auto_purchase_pause_above=0,
        ),
    )
    monkeypatch.setattr(pool_module.wuying_ecd, "list_fleet_desktops", _prewarm_fleet(40))
    result = await PoolService().ensure_prewarm(dry_run=False)
    assert result["status"] == "satisfied"
    assert result["auto_purchase_paused"] is None

    monkeypatch.setattr(
        pool_module,
        "get_config",
        lambda: _config(
            pool_target_prewarm=5, pool_auto_purchase=False, pool_auto_purchase_pause_above=10,
        ),
    )
    result = await PoolService().ensure_prewarm(dry_run=False)
    assert result["auto_purchase_paused"] is None
    assert await pool_module.auto_purchase_pause_state() is None
