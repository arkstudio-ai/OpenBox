"""Worker admin socket: ticket store, read-only protocol, refusals after accept and direct watermarks."""
import asyncio
import time

import pytest

import auth.ticket as tickets
from auth.jwt import decode_access_token
from bus import bus
from db.models.user import User
from tests.unit.test_worker_app_harness import (PREFIX, admin_env, auth_stores, business_db,  # noqa: F401
    internal_backend, socket, token, trace_url, worker)
from trajectory.store.database import trace_session
from trajectory.store.models import TrajectoryMetaSession
from trajectory.types import now
from trajectory.worker import ws

WATERMARK = {"user_id", "owner_user_id", "session_id", "trajectory_id", "committed_seq"}


async def ticket_for(worker, bearer: str | None = None) -> str:
    response = await worker.client.post(PREFIX + "/ticket", headers={"Authorization": f"Bearer {bearer}"} if bearer else None)
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


async def closed_with(receive) -> int:
    while True:
        message = await receive()
        if message.get("type") == "websocket.close":
            return message["code"]


async def test_tickets_are_scoped_single_use_and_need_a_live_admin(worker):
    response = await worker.client.post(PREFIX + "/ticket")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    ticket = response.json()["ticket"]
    assert await tickets.consume_ticket(ticket) is None  # the chat audience cannot claim it
    claims = await asyncio.gather(*(ws.claim_ticket(ticket) for _ in range(20)))
    assert sum(value is not None for value in claims) == 1
    identity = next(value for value in claims if value)
    assert identity["user_id"] == "admin" and identity["audience"] == "admin_trajectories"
    assert identity["auth_jti"] == decode_access_token(worker.token)["jti"]
    assert identity["auth_expires_at"] > time.time() and identity["client"] == "web"
    forged = await worker.client.post(PREFIX + "/ticket", headers={"Authorization": f"Bearer {token('a', 'admin')}"})
    assert forged.status_code == 403
    anonymous = await worker.client.post(PREFIX + "/ticket", headers={"Authorization": ""})
    assert anonymous.status_code == 401 and anonymous.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("case", ["missing", "unknown", "chat", "used"])
async def test_socket_without_a_valid_admin_ticket_is_closed_4401_after_accept(worker, case):
    ticket = None if case == "missing" else "never-issued"
    if case == "chat":
        ticket = await tickets.create_ticket("admin", "admin")
    if case == "used":
        ticket = await ticket_for(worker)
        assert await ws.claim_ticket(ticket) is not None
    async with worker.socket(ticket) as (_, receive):
        assert (await receive())["type"] == "websocket.accept"
        assert await closed_with(receive) == 4401


@pytest.mark.parametrize("case,code", [("member", 4403), ("disabled", 4403), ("expired", 4401), ("revoked", 4401),
                                       ("backend_down", 1011)])
async def test_refused_viewers_get_close_codes_after_accept(worker, monkeypatch, case, code):
    ticket = await ticket_for(worker)
    if case == "member":
        ticket = await tickets.create_ticket("a", "admin", audience="admin_trajectories")
    elif case == "disabled":
        monkeypatch.setenv("TRAJECTORY_ADMIN_ENABLED", "false")
        from trajectory.auth import clear_viewer_cache
        clear_viewer_cache()
    elif case == "expired":
        ticket = await tickets.create_ticket("admin", "admin", audience="admin_trajectories",
                                             auth_jti="expired", auth_expires_at=int(time.time()) - 1)
    elif case == "revoked":
        await worker.cache.set("jwt_bl:" + decode_access_token(worker.token)["jti"], True, ttl=60)
    else:
        from trajectory.auth import clear_viewer_cache
        clear_viewer_cache()
        worker.backend.down = True
    async with worker.socket(ticket) as (_, receive):
        assert (await receive())["type"] == "websocket.accept"
        assert await closed_with(receive) == code


async def test_read_only_protocol_and_direct_watermarks(worker):
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1", "after_seq": "2"})
        subscribed = await receive()
        assert subscribed == {"type": "subscribed", "data": {"user_id": "a", "owner_user_id": "a",
                              "session_id": "session_a_1", "trajectory_id": "trj_a1", "committed_seq": "3"}}
        # The watermark comes from the trajectory row alone, never from the session header.
        assert worker.layer.called("get_session_header") == []
        assert worker.layer.called("get_trajectory")[-1] == (("session_a_1",), {"optional": True})
        await send({"type": "subscribe", "session_id": "session_a_2"})
        assert await receive() == {"type": "subscribed", "data": {"user_id": "a", "owner_user_id": "a",
                                   "session_id": "session_a_2", "trajectory_id": None, "committed_seq": "0"}}
        for action in ("permission.reply", "question.reply", "session.prompt", "session.cancel"):
            await send({"type": action, "session_id": "session_a_1", "answers": [["yes"]]})
            assert await receive() == {"type": "error", "data": {"code": "READ_ONLY",
                                       "message": "Only subscriptions and ping are accepted"}}
        for invalid in ("not json", "[1, 2]"):
            await send(invalid)
            assert await receive() == {"type": "error", "data": {"code": "INVALID_MESSAGE"}}
        await send({"type": "ping"})
        assert await receive() == {"type": "pong", "data": {}}
        await send({"type": "subscribe", "session_id": "never"})
        assert await receive() == {"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": "never"}}
        await send({"type": "subscribe", "session_id": "x" * 65})
        assert (await receive())["data"]["code"] == "READ_ONLY"

        headers_read = len(worker.layer.called("get_session_header"))
        bus.publish("trajectory.available", {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1",
                                             "trajectory_id": "trj_a1", "committed_seq": 4, "extra": "never sent"})
        hint = await receive()
        assert hint["type"] == "trajectory.available" and set(hint["data"]) == WATERMARK
        assert hint["data"] == {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1",
                                "trajectory_id": "trj_a1", "committed_seq": "4"}
        bus.publish("trajectory.available", {"user_id": "b", "owner_user_id": "b", "session_id": "session_b_1",
                                             "trajectory_id": "trj_b1", "committed_seq": "2"})
        bus.publish("trajectory.available", {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1",
                                             "trajectory_id": "trj_a1", "committed_seq": "5"})
        assert (await receive())["data"]["committed_seq"] == "5"  # the unsubscribed session is not forwarded
        assert len(worker.layer.called("get_session_header")) == headers_read

        await send({"type": "unsubscribe", "session_id": "session_a_1"})
        assert await receive() == {"type": "unsubscribed", "data": {"session_id": "session_a_1"}}
        bus.publish("trajectory.available", {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1",
                                             "trajectory_id": "trj_a1", "committed_seq": "6"})
        await send({"type": "ping"})
        assert await receive() == {"type": "pong", "data": {}}

        await send({"type": "subscribe", "session_id": "session_a_1"})
        assert (await receive())["type"] == "subscribed"
        bus.publish("trajectory.available", {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1",
                                             "trajectory_id": "trj_a1", "committed_seq": "6", "deleted": True})
        assert await receive() == {"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": "session_a_1"}}
        bus.publish("trajectory.available", {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1",
                                             "trajectory_id": "trj_a1", "committed_seq": "7"})
        await send({"type": "ping"})
        assert await receive() == {"type": "pong", "data": {}}
    audit = [(row.resource_id, row.details) for row in await worker.delivered_audit()
             if row.action == "trajectory.subscribe"]
    recorded = ("trj_a1", {"owner_user_id": "a", "session_id": "session_a_1", "through_seq": "3"})
    assert audit == [recorded, ("session_a_2", {"owner_user_id": "a", "session_id": "session_a_2", "through_seq": "0"}),
                     recorded]


async def test_the_viewer_is_checked_once_per_second_however_busy_the_socket_is(worker, monkeypatch):
    checks, original = [], ws.validate_viewer

    async def counted(identity):
        checks.append(time.monotonic())
        await original(identity)

    monkeypatch.setattr(ws, "validate_viewer", counted)
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"

        async def burst():
            for _ in range(20):
                await send({"type": "ping"})
                assert await receive() == {"type": "pong", "data": {}}

        # Forty messages within the second after the connect check (every pong follows it) reuse it.
        await burst()
        assert len(checks) == 1
        await asyncio.sleep(1.2)
        assert len(checks) == 2  # the periodic check, once
        await burst()
        assert len(checks) == 2
        # A revocation still closes the socket within a second.
        revoked = time.monotonic()
        await worker.cache.set(f"jwt_bl:{decode_access_token(worker.token)['jti']}", True, ttl=60)
        assert (await receive(timeout=2))["code"] == 4401
        assert time.monotonic() - revoked <= 1.5


async def test_a_deletion_hint_is_not_replaced_by_a_hint_delivered_after_it(worker):
    """Redis fan-out does not keep publish order: an older watermark may arrive after the deletion."""
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1"})
        assert (await receive())["type"] == "subscribed"
        hint = {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1", "trajectory_id": "trj_a1"}
        # Both arrive before the socket's publisher runs, so they coalesce.
        bus.publish("trajectory.available", {**hint, "committed_seq": "4", "deleted": True})
        bus.publish("trajectory.available", {**hint, "committed_seq": "3"})
        assert await receive() == {"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": "session_a_1"}}
        bus.publish("trajectory.available", {**hint, "committed_seq": "5"})
        await send({"type": "ping"})
        assert await receive() == {"type": "pong", "data": {}}  # the dropped subscription forwards nothing more


async def test_subscription_limit_is_sixteen_sessions(worker):
    async with trace_session() as db:
        for index in range(17):
            db.add(TrajectoryMetaSession(id=f"extra_{index}", user_id="a", workspace_id="ws_a", updated_at=now(),
                                         synced_at=now()))
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        for index in range(16):
            await send({"type": "subscribe", "session_id": f"extra_{index}"})
            assert (await receive())["type"] == "subscribed"
        await send({"type": "subscribe", "session_id": "extra_0"})
        assert (await receive())["type"] == "subscribed"  # resubscribing does not count twice
        await send({"type": "subscribe", "session_id": "extra_16"})
        assert await receive() == {"type": "error", "data": {"code": "SUBSCRIPTION_LIMIT"}}


async def test_live_role_change_closes_an_open_socket_with_4403(worker, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_AUTH_CACHE_SECONDS", "1")
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1"})
        assert (await receive())["type"] == "subscribed"
        async with worker.business.begin() as db:
            (await db.get(User, "admin")).role = "user"
        assert (await receive(timeout=5))["code"] == 4403
    assert (await worker.client.get(PREFIX + "/sessions")).status_code == 403


async def test_idle_subscription_stops_after_token_revocation(worker):
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (_, receive):
        assert (await receive())["type"] == "websocket.accept"
        await worker.cache.set(f"jwt_bl:{decode_access_token(worker.token)['jti']}", True, ttl=60)
        assert (await receive())["code"] == 4401
    assert (await worker.client.get(PREFIX + "/sessions")).status_code == 401


async def test_unreachable_authority_closes_with_a_retryable_code(worker, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_AUTH_CACHE_SECONDS", "1")
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (_, receive):
        assert (await receive())["type"] == "websocket.accept"
        worker.backend.down = True
        assert (await receive(timeout=5))["code"] == 1011


def test_close_codes_and_watermark_shape():
    from fastapi import HTTPException
    assert [ws.close_code(HTTPException(status)) for status in (401, 403, 404, 503)] == [4401, 4403, 4403, 1011]
    assert ws.watermark({"user_id": "a", "owner_user_id": "a", "session_id": "s", "trajectory_id": "t",
                         "committed_seq": 12, "deleted": False}) == {
        "user_id": "a", "owner_user_id": "a", "session_id": "s", "trajectory_id": "t", "committed_seq": "12"}


@pytest.mark.parametrize("case,code", [
    ("member_and_expired", 4403),
    ("disabled_and_revoked", 4403),
    ("member_with_a_replaced_mobile_session", 4403),
    ("expired_with_a_replaced_mobile_session", 4401),
])
async def test_the_old_check_order_decides_the_close_code_when_several_checks_fail(worker, monkeypatch, case, code):
    """Account, role and allowlist first, then token expiry and revocation, then the mobile session."""
    user_id, fields = "admin", {"audience": ws.AUDIENCE}
    expired = int(time.time()) - 1
    if case == "member_and_expired":
        user_id, fields["auth_expires_at"] = "a", expired
    elif case == "disabled_and_revoked":
        monkeypatch.setenv("TRAJECTORY_ADMIN_ENABLED", "false")
        await worker.cache.set("jwt_bl:revoked-jti", True, ttl=60)
        fields["auth_jti"] = "revoked-jti"
    elif case == "member_with_a_replaced_mobile_session":
        user_id = "a"
        fields.update(client="mobile", mobile_session_id="replaced")
    else:
        fields.update(client="mobile", mobile_session_id="replaced", auth_expires_at=expired)
    from trajectory.auth import clear_viewer_cache
    clear_viewer_cache()
    ticket = await tickets.create_ticket(user_id, "admin", **fields)
    async with worker.socket(ticket) as (_, receive):
        assert (await receive())["type"] == "websocket.accept"
        assert await closed_with(receive) == code
