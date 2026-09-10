"""Lifecycle, timeouts and races over real HTTP + SQLite; no provider network."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from auth.mobile import mobile_transaction, now
from db.models.push import MobilePresence, PushDelivery, PushMessage
from notifications.runtime import PushWorker, claim_delivery, still_sendable
from notifications.store import enqueue_notification
from tests.integration.test_mobile_push_api import setup, login, bind  # noqa: F401


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        value = now()

        def advance(self, seconds):
            self.value += timedelta(seconds=seconds)

    clock = Clock()
    for module in ("auth.mobile", "notifications.presence", "notifications.runtime",
                   "notifications.events", "notifications.store", "api.push"):
        monkeypatch.setattr(module + ".now", lambda: clock.value)
    return clock


@pytest.fixture
async def phone(setup, clock):
    _, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    await bind(ios, "ios")
    return ios, user, fake


async def report(client, state, sequence):
    response = await client.put("/api/push/presence", json={"state": state, "sequence": sequence})
    assert response.status_code == 200, response.text
    return response.json()


async def enqueue(user, key="event"):
    async with mobile_transaction() as db:
        message = await enqueue_notification(db, user_id=user, event_key=key, kind="task_completed",
                                             title="Task completed", body="A result is ready")
        return message.id


async def delivery(message_id):
    async with mobile_transaction() as db:
        return await db.scalar(select(PushDelivery).where(PushDelivery.message_id == message_id))


@pytest.mark.parametrize("state", ["resumed", "inactive"])
async def test_all_visible_states_suppress_even_after_brief_connection_loss(phone, clock, state):
    client, user, fake = phone
    data = await report(client, state, 1)
    assert data["policy"] == "suppress"
    # No socket event is consulted; missing two heartbeats is still foreground.
    clock.advance(30)
    message = await enqueue(user)
    clock.advance(4)
    await PushWorker(fake).tick()
    assert fake.sent == []
    row = await delivery(message)
    assert (row.status, row.error) == ("cancelled", "app_foreground")


@pytest.mark.parametrize("state", ["hidden", "paused"])
async def test_background_is_eligible_only_after_settle_window(phone, clock, state):
    client, user, fake = phone
    assert (await report(client, state, 1))["pushAllowed"] is False
    message = await enqueue(user)
    clock.advance(2)
    await PushWorker(fake).tick()
    assert fake.sent == []
    clock.advance(2)
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1
    assert (await delivery(message)).status == "accepted"


async def test_unknown_startup_and_reconnecting_are_not_immediate_offline(phone, clock):
    client, user, fake = phone
    assert (await client.get("/api/push/status")).json()["presence"]["appState"] == "unknown"
    message = await enqueue(user)
    clock.advance(4)
    await PushWorker(fake).tick()
    assert (await delivery(message)).error == "presence_unknown"
    # A background update wakes a deferred event, instead of waiting 90 seconds.
    await report(client, "paused", 1)
    clock.advance(4)
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1


async def test_long_missing_heartbeat_is_inferred_offline_but_brief_loss_is_deferred(phone, clock):
    client, user, fake = phone
    await report(client, "resumed", 1)
    clock.advance(50)
    message = await enqueue(user)
    clock.advance(4)
    await PushWorker(fake).tick()
    assert (await delivery(message)).error == "presence_reconnecting"
    assert (await client.get("/api/push/status")).json()["presence"]["appState"] == "reconnecting"
    clock.advance(37)
    assert (await client.get("/api/push/status")).json()["presence"]["appState"] == "offline_inferred"
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1


async def test_foreground_return_cancels_queued_and_claimed_deliveries(phone, clock):
    client, user, fake = phone
    await report(client, "paused", 1)
    a, b = await enqueue(user, "a"), await enqueue(user, "b")
    clock.advance(4)
    claim = await claim_delivery(fake.enabled)
    assert claim
    await report(client, "resumed", 2)
    assert not await still_sendable(claim)
    await PushWorker(fake).tick()
    assert fake.sent == []
    assert all([(await delivery(id)).status == "cancelled" for id in (a, b)])


async def test_reordered_background_and_duplicate_heartbeat_cannot_override_foreground(phone, clock):
    client, user, fake = phone
    await report(client, "paused", 20)
    await report(client, "resumed", 21)
    assert (await report(client, "paused", 20))["applied"] is False
    clock.advance(46)
    assert (await report(client, "resumed", 21))["applied"] is False
    # Replaying a heartbeat must not renew freshness indefinitely.
    status = (await client.get("/api/push/status")).json()["presence"]
    assert status["appState"] == "reconnecting" and status["sequence"] == 21


async def test_displaced_phone_cannot_report_presence_for_the_new_login(setup, clock):
    web, ios, android, credentials, user, _ = setup
    await login(ios, credentials)
    await report(ios, "paused", 300)
    await login(android, credentials)
    await report(android, "resumed", 1)
    assert (await ios.put("/api/push/presence", json={"state": "paused", "sequence": 999})).status_code == 401
    assert (await web.put("/api/push/presence", json={"state": "paused", "sequence": 999})).status_code == 401
    status = (await android.get("/api/push/status")).json()["presence"]
    assert status["appState"] == "foreground" and status["sequence"] == 1


@pytest.mark.parametrize("body", [
    {"state": "offline", "sequence": 1}, {"state": "resumed", "sequence": 0},
    {"state": "resumed", "sequence": True}, {"state": "paused", "sequence": 1, "user_id": "other"},
])
async def test_presence_rejects_invented_states_and_untrusted_identity(phone, body):
    client, _, _ = phone
    assert (await client.put("/api/push/presence", json=body)).status_code == 422


@pytest.mark.parametrize("background", [True, False])
async def test_remote_test_has_setup_delay_and_obeys_the_same_visibility_policy(phone, clock, background):
    client, _, fake = phone
    await report(client, "resumed", 1)
    response = await client.post("/api/push/test")
    assert response.status_code == 202
    clock.advance(5)
    await report(client, "paused" if background else "resumed", 2)
    await PushWorker(fake).tick()
    assert fake.sent == []
    clock.advance(6)
    await PushWorker(fake).tick()
    assert len(fake.sent) == (1 if background else 0)
    assert (await delivery(response.json()["id"])).status == ("accepted" if background else "cancelled")
