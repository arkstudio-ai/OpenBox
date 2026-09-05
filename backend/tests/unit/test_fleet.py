"""Fleet reconciliation and source-health alert lifecycle."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.fleet import FleetAlert, FleetSnapshot
from sandbox import fleet as fleet_module
from sandbox.fleet import (
    Finding,
    FleetScopeError,
    open_operational_alert,
    persist_alerts,
    reconcile,
)
from sandbox.wuying_ecd import TAG_POOL, TAG_WORKSPACE


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)


def _remote(desktop_id: str = "ecd-1", **overrides):
    row = {
        "desktop_id": desktop_id,
        "status": "Running",
        "charge_type": "PrePaid",
        "expired_time": (NOW + timedelta(days=30)).isoformat(),
        "tags": {TAG_POOL: "assigned", TAG_WORKSPACE: "ws-1"},
    }
    row.update(overrides)
    return row


def _local(desktop_id: str = "ecd-1", **overrides):
    row = {
        "id": "cld-1",
        "desktop_id": desktop_id,
        "workspace_id": "ws-1",
        "status": "running",
        "pool_state": "assigned",
        "tunnel_state": "up",
        "last_seen_at": NOW,
        "created_at": NOW - timedelta(hours=1),
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def _rules(**kwargs) -> set[str]:
    defaults = dict(ecd=[_remote()], db=[_local()], account={
        "available_balance": 1000, "unit_price": 200,
    })
    defaults.update(kwargs)
    return {item.rule for item in reconcile(now=NOW, **defaults)}


def test_ghost_positive_and_negative():
    assert "ghost" in _rules(ecd=[], db=[_local()])
    assert "ghost" not in _rules()


def test_orphan_positive_and_negative():
    assert "orphan" in _rules(ecd=[_remote()], db=[])
    assert "orphan" not in _rules()


def test_tag_mismatch_positive_and_negative():
    assert "tag_mismatch" in _rules(ecd=[_remote(tags={TAG_POOL: "prewarm"})])
    assert "tag_mismatch" not in _rules()


def test_expiring_soon_positive_and_negative():
    soon = (NOW + timedelta(days=2)).isoformat()
    assert "expiring_soon" in _rules(ecd=[_remote(expired_time=soon)])
    assert "expiring_soon" not in _rules()
    assert "expiring_soon" not in _rules(ecd=[_remote(
        expired_time=soon, tags={TAG_POOL: "retired"}
    )])


def test_expired_positive_and_negative():
    assert "expired" in _rules(ecd=[_remote(status="Expired")])
    assert "expired" not in _rules()


def test_channel_down_positive_and_negative():
    stale = NOW - timedelta(minutes=11)
    assert "channel_down" in _rules(db=[_local(tunnel_state="down", last_seen_at=stale)])
    assert "channel_down" not in _rules(db=[_local(tunnel_state="down", last_seen_at=NOW)])


def test_postpaid_running_positive_and_negative():
    old = NOW - timedelta(hours=25)
    assert "postpaid_running" in _rules(
        ecd=[_remote(charge_type="PostPaid")], db=[_local(created_at=old)]
    )
    assert "postpaid_running" not in _rules(
        ecd=[_remote(charge_type="PostPaid")], db=[_local()]
    )


def test_prewarm_below_watermark_positive_and_negative():
    assert "prewarm_below_watermark" in _rules(target_prewarm=1)
    prewarm = _remote(tags={TAG_POOL: "prewarm", TAG_WORKSPACE: ""})
    assert "prewarm_below_watermark" not in _rules(
        ecd=[prewarm], db=[], target_prewarm=1
    )


def test_purchase_blocked_positive_and_negative():
    assert "purchase_blocked" in _rules(account={
        "available_balance": 1000, "unit_price": 200,
        "purchase_blocked": {"message": "daily limit", "gate": "daily"},
    })
    assert "purchase_blocked" not in _rules()


def test_account_balance_low_positive_and_negative():
    assert "account_balance_low" in _rules(account={
        "available_balance": 399, "unit_price": 200,
    })
    assert "account_balance_low" not in _rules()


def test_failed_sources_skip_dependent_rules():
    assert "ghost" not in _rules(ecd=None, db=[_local()])
    assert "channel_down" in _rules(
        ecd=None,
        db=[_local(tunnel_state="down", last_seen_at=NOW - timedelta(hours=1))],
    )


async def test_source_health_does_not_close_or_open_dependent_alerts():
    async with get_db_session() as session:
        await session.execute(delete(FleetAlert))
    ghost = Finding("ghost", "critical", "desktop", "ecd-ghost", "ghost", {})
    await persist_alerts([ghost], {"ecd": True, "db": True, "account": True}, NOW)
    await persist_alerts([], {"ecd": False, "db": True, "account": True}, NOW + timedelta(minutes=10))
    async with get_db_session() as session:
        row = (await session.execute(select(FleetAlert))).scalar_one()
        assert row.resolved_at is None

    await persist_alerts([], {"ecd": True, "db": True, "account": True}, NOW + timedelta(minutes=20))
    async with get_db_session() as session:
        row = (await session.execute(select(FleetAlert))).scalar_one()
        assert row.resolved_at.replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=20)


async def test_auto_resolve_requires_all_three_sources_healthy():
    async with get_db_session() as session:
        await session.execute(delete(FleetAlert))
    channel = Finding("channel_down", "warn", "desktop", "ecd-down", "down", {})
    await persist_alerts([channel], {"ecd": True, "db": True, "account": True}, NOW)
    await persist_alerts(
        [],
        {"ecd": True, "db": True, "account": False},
        NOW + timedelta(minutes=10),
    )
    async with get_db_session() as session:
        row = (await session.execute(select(FleetAlert))).scalar_one()
        assert row.resolved_at is None


async def test_operational_alert_does_not_resolve_reconciliation_alerts():
    async with get_db_session() as session:
        await session.execute(delete(FleetAlert))
    ghost = Finding("ghost", "critical", "desktop", "ecd-ghost", "ghost", {})
    await persist_alerts([ghost], {"ecd": True, "db": True, "account": True}, NOW)
    await open_operational_alert(
        Finding("assign_failed", "critical", "desktop", "ecd-pool", "failed", {})
    )
    async with get_db_session() as session:
        rows = (await session.execute(select(FleetAlert))).scalars().all()
        assert {row.rule for row in rows if row.resolved_at is None} == {
            "ghost",
            "assign_failed",
        }


async def test_take_snapshot_persists_three_sources_and_prunes_old_rows(monkeypatch):
    async with get_db_session() as session:
        await session.execute(delete(FleetSnapshot))
        await session.execute(delete(FleetAlert))
        session.add(FleetSnapshot(
            id="fsp-old",
            taken_at=datetime.now(timezone.utc) - timedelta(days=8),
            source="ecd",
            ok=True,
            payload=[],
        ))

    async def desktops():
        return []

    async def balance():
        return {"available_balance": 1000, "currency": "CNY"}

    async def price(_charge_type):
        return {"trade_price": 200, "currency": "CNY"}

    monkeypatch.setattr(fleet_module.wuying_ecd, "list_fleet_desktops", desktops)
    monkeypatch.setattr(fleet_module.wuying_ecd, "query_account_balance", balance)
    monkeypatch.setattr(fleet_module.wuying_ecd, "describe_price", price)
    monkeypatch.setattr(
        fleet_module,
        "get_config",
        lambda: OpenBoxConfig(pool_target_prewarm=0),
    )

    result = await fleet_module.take_snapshot()
    assert result["sources"] == {"ecd": True, "db": True, "account": True}
    async with get_db_session() as session:
        rows = (
            await session.execute(select(FleetSnapshot).order_by(FleetSnapshot.source))
        ).scalars().all()
        assert [row.source for row in rows] == ["account", "db", "ecd"]
        assert len({row.taken_at for row in rows}) == 1


def test_pool_configuration_defaults_are_safe():
    config = OpenBoxConfig()
    assert config.wuying_desktop_type == "eds.enterprise_office.6c12g"
    assert config.pool_enabled is False
    assert config.pool_auto_purchase is False
    assert config.pool_target_prewarm == 5
    assert config.pool_max_purchases_per_tick == 1
    assert config.pool_max_purchases_per_day == 2


async def test_snapshot_scope_refuses_shared_or_unapproved_bossip_desktops(monkeypatch):
    async def shared():
        return [{"desktop_id": "ecd-4zjxaq5g45dr5qr0i", "tags": {}}]

    monkeypatch.setattr(fleet_module.wuying_ecd, "list_fleet_desktops", shared)
    with pytest.raises(FleetScopeError, match="shared desktop"):
        await fleet_module._ecd_snapshot()

    async def bossip():
        return [{"desktop_id": "ecd-not-approved", "tags": {"purpose": "codex"}}]

    monkeypatch.setattr(fleet_module.wuying_ecd, "list_fleet_desktops", bossip)
    monkeypatch.setattr(
        fleet_module, "get_config", lambda: OpenBoxConfig(pool_adopt_allowlist="ecd-approved")
    )
    with pytest.raises(FleetScopeError, match="non-allowlisted"):
        await fleet_module._ecd_snapshot()
