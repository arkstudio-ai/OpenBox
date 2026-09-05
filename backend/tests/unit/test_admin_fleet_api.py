"""Fleet administration remains admin-only and exposes alert lifecycle."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from auth.middleware import get_current_user
from db.base import get_db_session
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
