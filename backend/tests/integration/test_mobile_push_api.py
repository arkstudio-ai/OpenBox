"""Actual HTTP auth/refresh/device transitions against an isolated database."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from auth.jwt import decode_access_token
from auth.mobile import begin_login, mobile_transaction, now, validate_claims, watch_session
from auth.ticket import consume_ticket
from db.base import Base, close_engine, get_db_session, init_engine
from db.models.push import MobilePresence, PushDelivery, PushDevice
from db.models.workspace import Workspace, WorkspaceMember
from notifications.providers import SendResult
from notifications.runtime import PushWorker, claim_delivery, settle_delivery
from notifications.store import enqueue_notification


class FakeProviders:
    enabled = {"apns", "jpush"}

    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append(args)
        return SendResult(True, message_id="provider-test-id")

    async def close(self):
        pass


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    import db.models  # noqa
    from cache.memory_cache import MemoryCache
    from core.config import OpenBoxConfig
    from auth import setup_auth
    from main import create_app

    await close_engine()
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'push.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = OpenBoxConfig(jwt_secret="mobile-test-secret-never-production")
    monkeypatch.setattr("core.config.get_config", lambda: config)
    cache = MemoryCache()
    setup_auth(config, cache)
    application = create_app()
    fake = FakeProviders()
    application.state.push_providers = fake
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as web:
        credentials = {"username": "push-" + uuid4().hex[:12], "password": "password123"}
        response = await web.post("/api/auth/register", json=credentials)
        assert response.status_code == 200, response.text
        web.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        user = response.json()["user"]["id"]
        # Transport diagnostics exercise the now-admin-only test route.
        from db.models.user import User
        async with get_db_session() as db:
            row = await db.get(User, user)
            row.role = "admin"
        await login(web, credentials)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={
            "X-Client-Type": "mobile", "X-Installation-Id": "installation-iphone-0001",
        }) as ios, httpx.AsyncClient(transport=transport, base_url="http://test", headers={
            "X-Client-Type": "mobile", "X-Installation-Id": "installation-android-0002",
        }) as android:
            yield web, ios, android, credentials, user, fake
    await cache.close()
    await close_engine()


async def login(client, credentials):
    response = await client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    data = response.json()
    client.headers["Authorization"] = "Bearer " + data["access_token"]
    return data


async def bind(client, platform):
    response = await client.post("/api/push/devices", json={
        "platform": platform, "provider": "apns" if platform == "ios" else "jpush",
        "token": "ab" * 32 if platform == "ios" else "jpushregistration0001",
        "apnsEnvironment": "sandbox" if platform == "ios" else "production",
    })
    assert response.status_code == 200, response.text
    return response.json()


async def make_due_in_background(user):
    """Transport tests simulate time passing after a genuine background report."""
    async with mobile_transaction() as db:
        presence = await db.get(MobilePresence, user)
        presence.state, presence.reported_at = "paused", now() - timedelta(seconds=15)
        await db.execute(update(PushDelivery).where(PushDelivery.user_id == user,
            PushDelivery.status == "pending").values(available_at=now() - timedelta(seconds=1)))


@pytest.mark.parametrize("first,second", [("ios", "android"), ("android", "ios")])
async def test_new_phone_revokes_http_refresh_ticket_and_binding_before_permission(setup, first, second):
    web, ios, android, credentials, user, fake = setup
    clients = {"ios": ios, "android": android}
    a, b = clients[first], clients[second]
    old = await login(a, credentials)
    await bind(a, first)
    queued = await a.post("/api/push/test")
    assert queued.status_code == 202
    ticket = (await a.post("/api/auth/ticket")).json()["ticket"]

    new = await login(b, credentials)  # No push permission or token on B yet.
    assert new["mobile_session_id"] != old["mobile_session_id"]
    assert (await a.get("/api/auth/me")).status_code == 401
    assert (await a.post("/api/auth/refresh")).status_code == 401
    assert (await a.post("/api/auth/ticket")).status_code == 401
    assert await consume_ticket(ticket) is None
    assert (await b.get("/api/auth/me")).status_code == 200
    assert (await web.get("/api/auth/me")).status_code == 200
    assert (await b.get("/api/push/status")).json()["registered"] is False
    assert await claim_delivery(fake.enabled) is None
    assert (await b.get(f"/api/push/messages/{queued.json()['id']}")).json()["status"] == "cancelled"

    device = await bind(b, second)
    assert device["deliveryEnabled"] is True
    # The displaced device must not be able to reclaim the endpoint.
    assert (await a.post("/api/push/devices", json={"platform": first, "provider": "apns" if first == "ios" else "jpush", "token": "aa" * 32})).status_code == 401


async def test_refresh_keeps_lease_and_repeated_registration_is_idempotent(setup):
    _, ios, _, credentials, _, _ = setup
    initial = await login(ios, credentials)
    device = await bind(ios, "ios")
    refreshed = await ios.post("/api/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["mobile_session_id"] == initial["mobile_session_id"]
    ios.headers["Authorization"] = "Bearer " + refreshed.json()["access_token"]
    assert (await bind(ios, "ios"))["bindingId"] == device["bindingId"]


async def test_test_api_flows_through_worker_and_is_rate_limited(setup):
    web, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    response = await ios.post("/api/push/test")
    assert response.status_code == 202
    assert (await ios.post("/api/push/test")).status_code == 429
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1
    assert fake.sent[0][0] == "apns"
    assert fake.sent[0][2] == "sandbox"
    payload = fake.sent[0][3]
    assert payload["source"] == "openbox" and payload["type"] == "system_test"
    result = await ios.get(f"/api/push/messages/{response.json()['id']}")
    assert result.json()["status"] == "accepted"
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1
    assert (await web.post("/api/push/test")).status_code == 401


async def test_late_provider_completion_cannot_revive_old_binding(setup):
    _, ios, android, credentials, user, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    response = await ios.post("/api/push/test")
    await make_due_in_background(user)
    claim = await claim_delivery(fake.enabled)
    assert claim
    await login(android, credentials)
    new = await bind(android, "android")
    await settle_delivery(claim, SendResult(True, message_id="late"))
    status = (await android.get(f"/api/push/messages/{response.json()['id']}")).json()
    assert status["status"] == "cancelled"
    assert (await android.get("/api/push/status")).json()["bindingId"] == new["bindingId"]


async def test_logout_revokes_access_refresh_and_pending_push(setup):
    _, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    await ios.post("/api/push/test")
    cookie = ios.cookies.get("refresh_token")
    assert (await ios.post("/api/auth/logout")).status_code == 200
    assert (await ios.get("/api/auth/me")).status_code == 401
    ios.cookies.set("refresh_token", cookie)
    assert (await ios.post("/api/auth/refresh")).status_code == 401
    assert await claim_delivery(fake.enabled) is None


async def test_same_installation_switches_accounts_without_leaking_old_messages(setup):
    web, ios, android, credentials, user, fake = setup
    old = await login(ios, credentials)
    await bind(ios, "ios")
    queued = await ios.post("/api/push/test")
    other = {"username": "other-" + uuid4().hex[:12], "password": "password123"}
    assert (await web.post("/api/auth/register", json=other)).status_code == 200
    await login(ios, other)
    await bind(ios, "ios")
    assert await claim_delivery(fake.enabled) is None
    assert (await ios.get(f"/api/push/messages/{queued.json()['id']}")).status_code == 404
    with pytest.raises(HTTPException):
        await validate_claims(decode_access_token(old["access_token"]))


async def test_old_disable_and_stale_lease_result_do_not_change_new_endpoint(setup):
    _, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    first = await bind(ios, "ios")
    await ios.post("/api/push/test")
    assert (await ios.delete(f"/api/push/devices/{first['bindingId']}")).status_code == 200
    second = await bind(ios, "ios")
    assert first["bindingId"] != second["bindingId"]
    await ios.delete(f"/api/push/devices/{first['bindingId']}")
    assert (await ios.get("/api/push/status")).json()["notificationsEnabled"] is True
    assert await claim_delivery(fake.enabled) is None


async def test_business_enqueue_is_transactional_and_deduplicated(setup):
    _, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    async with mobile_transaction() as db:
        a = await enqueue_notification(db, user_id=user, event_key="completed:1", kind="task_completed", title="Done", body="Result")
        b = await enqueue_notification(db, user_id=user, event_key="completed:1", kind="task_completed", title="Changed", body="Duplicate")
        assert a.id == b.id
    with pytest.raises(RuntimeError):
        async with mobile_transaction() as db:
            await enqueue_notification(db, user_id=user, event_key="rolled-back", kind="task_completed", title="No", body="No")
            raise RuntimeError("rollback")
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1


async def test_socket_lease_observer_ends_when_other_phone_signs_in(setup):
    _, ios, android, credentials, user, _ = setup
    old = await login(ios, credentials)
    identity = {"user_id": user, "client": "mobile", "mobile_session_id": old["mobile_session_id"]}
    watcher = asyncio.create_task(watch_session(identity, interval=0.01))
    await login(android, credentials)
    with pytest.raises(HTTPException):
        await asyncio.wait_for(watcher, 1)


async def test_unconfigured_channel_is_not_reported_as_ready(setup):
    _, ios, _, credentials, _, fake = setup
    fake.enabled = set()
    await login(ios, credentials)
    assert (await bind(ios, "ios"))["deliveryEnabled"] is False
    assert (await ios.post("/api/push/test")).status_code == 409


@pytest.mark.parametrize("revocation", ["member", "workspace"])
async def test_queued_push_rechecks_workspace_access_before_sending(setup, revocation):
    _, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    async with mobile_transaction() as db:
        member = await db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == user))
        workspace_id = member.workspace_id
        await enqueue_notification(db, user_id=user, workspace_id=workspace_id,
            event_key="workspace-result", kind="task_completed", title="Private", body="Private")
        if revocation == "member":
            member.status = "removed"
        else:
            (await db.get(Workspace, workspace_id)).is_deleted = True
    assert await claim_delivery(fake.enabled) is None


async def test_expired_worker_lease_is_recoverable_and_old_attempt_cannot_settle(setup):
    from datetime import timedelta
    _, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    await ios.post("/api/push/test")
    await make_due_in_background(user)
    first = await claim_delivery(fake.enabled)
    async with mobile_transaction() as db:
        (await db.get(PushDelivery, first.id)).lease_until = now() - timedelta(seconds=1)
    second = await claim_delivery(fake.enabled)
    assert second.id == first.id and second.lease_id != first.lease_id
    await settle_delivery(first, SendResult(False, invalid_device=True, error="old-invalid-token"))
    async with get_db_session() as db:
        assert (await db.get(PushDevice, user)).enabled
        assert (await db.get(PushDelivery, first.id)).lease_id == second.lease_id
    await settle_delivery(second, SendResult(True, message_id="new-attempt"))
    async with get_db_session() as db:
        assert (await db.get(PushDelivery, first.id)).status == "accepted"


async def test_off_status_cannot_claim_delivery_ready(setup):
    _, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    device = await bind(ios, "ios")
    await ios.delete(f"/api/push/devices/{device['bindingId']}")
    status = (await ios.get("/api/push/status")).json()
    assert status["providerConfigured"] is True
    assert status["deliveryEnabled"] is False
