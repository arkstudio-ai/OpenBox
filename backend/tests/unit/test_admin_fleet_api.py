"""Fleet administration remains admin-only and exposes alert lifecycle."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from auth.middleware import get_current_user
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.fleet import FleetAlert, FleetSnapshot
from db.repository.user_repo import PgUserRepo
from main import create_app


async def _request(app, identity, method, path, body=None):
    async def current_user():
        return dict(identity)

    app.dependency_overrides[get_current_user] = current_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, json=body)
    finally:
        app.dependency_overrides.clear()


async def test_admin_reads_snapshot_and_acks_and_mutes_alert():
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"fleet-admin-{suffix}", username=f"fleet-admin-{suffix}",
        password_hash="unused", role="admin",
    )
    now = datetime.now(timezone.utc)
    alert_id = f"flt-{suffix}"
    async with get_db_session() as session:
        session.add(FleetSnapshot(
            id=f"fsp-{suffix}", taken_at=now, source="ecd", ok=True,
            payload={"desktops": []}, error=None,
        ))
        session.add(FleetAlert(
            id=alert_id, rule="ghost", severity="critical",
            resource_type="desktop", resource_id=f"ecd-{suffix}", message="ghost",
            detail={}, first_seen_at=now, last_seen_at=now,
        ))
    app = create_app()
    identity = {"user_id": user["id"], "role": "admin"}

    snapshot = await _request(app, identity, "GET", "/api/admin/fleet/snapshots/latest")
    alerts = await _request(app, identity, "GET", "/api/admin/fleet/alerts")
    ack = await _request(app, identity, "POST", f"/api/admin/fleet/alerts/{alert_id}/ack")
    mute = await _request(
        app, identity, "POST", f"/api/admin/fleet/alerts/{alert_id}/mute",
        {"until": (now + timedelta(hours=1)).isoformat()},
    )

    assert snapshot.status_code == 200
    assert any(row["source"] == "ecd" for row in snapshot.json()["sources"])
    assert alerts.status_code == 200
    assert any(row["id"] == alert_id for row in alerts.json()["items"])
    assert ack.status_code == 200
    assert mute.status_code == 200


async def test_non_admin_cannot_read_fleet():
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"fleet-user-{suffix}", username=f"fleet-user-{suffix}",
        password_hash="unused",
    )
    response = await _request(
        create_app(), {"user_id": user["id"], "role": "user"},
        "GET", "/api/admin/fleet/pool",
    )
    assert response.status_code == 403


async def test_admin_desktop_list_includes_live_ecd_users(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"fleet-list-{suffix}", username=f"fleet-list-{suffix}",
        password_hash="unused", role="admin",
    )
    now = datetime.now(timezone.utc)
    desktop_id = f"ecd-{suffix}"
    async with get_db_session() as session:
        session.add(CloudDesktop(
            id=f"cld-{suffix}", desktop_id=desktop_id,
            workspace_id=user["default_workspace_id"], user_id=user["id"],
            end_user_id="obx-live-user",
            region_id="cn-shanghai", status="running", pool_state="prewarm",
            tunnel_state="ready", created_at=now, updated_at=now,
        ))

    from sandbox import wuying_ecd

    async def entitlements(desktop_ids):
        assert desktop_id in desktop_ids
        return {desktop_id: ["obx-live-user"]}

    async def end_users(end_user_ids):
        assert end_user_ids == ["obx-live-user"]
        return {"obx-live-user": "stale ECD nickname"}

    monkeypatch.setattr(wuying_ecd, "describe_desktop_entitlements", entitlements)
    monkeypatch.setattr(wuying_ecd, "describe_end_users", end_users)
    response = await _request(
        create_app(), {"user_id": user["id"], "role": "admin"},
        "GET", "/api/admin/fleet/desktops",
    )

    assert response.status_code == 200
    row = next(item for item in response.json()["items"] if item["desktop_id"] == desktop_id)
    assert row["ecd_end_user_ids"] == ["obx-live-user"]
    assert row["ecd_end_users"] == [{
        "id": "obx-live-user", "username": user["username"],
    }]


async def test_admin_can_preview_pool_ensure(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"fleet-ensure-{suffix}", username=f"fleet-ensure-{suffix}",
        password_hash="unused", role="admin",
    )
    from sandbox.pool import pool_service

    async def ensure_prewarm(*, dry_run, actor):
        assert dry_run is True
        assert actor == user["id"]
        return {
            "status": "dry_run", "current": 4, "target": 5,
            "gap": 1, "quantity": 1, "unit_price": 200, "currency": "CNY",
        }

    monkeypatch.setattr(pool_service, "ensure_prewarm", ensure_prewarm)
    response = await _request(
        create_app(), {"user_id": user["id"], "role": "admin"},
        "POST", "/api/admin/fleet/pool/ensure?dry_run=true",
    )
    assert response.status_code == 200
    assert response.json()["quantity"] == 1


async def test_admin_renew_forwards_explicit_approval(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"fleet-renew-{suffix}", username=f"fleet-renew-{suffix}",
        password_hash="unused", role="admin",
    )
    from sandbox.pool import pool_service

    async def renew(desktop_id, actor, *, approve):
        assert desktop_id == "ecd-renew"
        assert actor == user["id"]
        assert approve is True
        return {"desktop_id": desktop_id, "pool_state": "prewarm"}

    monkeypatch.setattr(pool_service, "renew", renew)
    response = await _request(
        create_app(), {"user_id": user["id"], "role": "admin"},
        "POST", "/api/admin/fleet/desktops/ecd-renew/renew", {"approve": True},
    )
    assert response.status_code == 200
    assert response.json()["desktop_id"] == "ecd-renew"


async def test_admin_pool_summary_reports_and_resumes_churn_brake(monkeypatch):
    from sandbox import pool as pool_module

    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"fleet-brake-{suffix}", username=f"fleet-brake-{suffix}",
        password_hash="unused", role="admin",
    )
    await pool_module._pause_auto_purchase(12, 10, 5, "system")
    app = create_app()
    identity = {"user_id": user["id"], "role": "admin"}

    summary = await _request(app, identity, "GET", "/api/admin/fleet/pool")
    assert summary.status_code == 200
    body = summary.json()
    assert body["auto_purchase_paused"]["current"] == 12
    assert body["gates"]["pause_above"] == 10

    resume = await _request(app, identity, "POST", "/api/admin/fleet/pool/resume")
    assert resume.status_code == 200
    assert resume.json()["status"] == "resumed"

    after = await _request(app, identity, "GET", "/api/admin/fleet/pool")
    assert after.json()["auto_purchase_paused"] is None
