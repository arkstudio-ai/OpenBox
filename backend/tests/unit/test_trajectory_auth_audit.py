"""Trajectory audit: outbox writes with the 60 s list/view dedupe, and batched delivery to the backend."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from starlette.requests import Request

import trajectory.auth as trajectory_auth
from db.models.audit_log import AuditLog
from tests.unit.test_worker_app_harness import (admin_env, auth_stores, business_db, internal_backend,  # noqa: F401
    trace_engine, trace_url)
from trajectory.auth import AuditDelivery, AuditRejected, LocalBackend, record_audit
from trajectory.store.database import close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import TrajectoryAuditOutbox
from trajectory.types import now


async def outbox() -> list[TrajectoryAuditOutbox]:
    async with trace_session() as db:
        return list((await db.scalars(select(TrajectoryAuditOutbox).order_by(TrajectoryAuditOutbox.id))).all())


async def audit_logs(factory) -> list[AuditLog]:
    async with factory() as db:
        return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


def viewer_request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "client": ("10.0.0.7", 5000),
                    "headers": [(b"user-agent", b"fixture-agent/1.0")]})


class RecordingBackend:
    def __init__(self, *, delay: float = 0.0, refuse: str | None = None, down: bool = False):
        self.delay, self.refuse, self.down = delay, refuse, down
        self.sent: list[str] = []

    async def audit(self, entries):
        await asyncio.sleep(self.delay)
        if self.down:
            raise ConnectionError("backend down")
        if self.refuse and any(entry["action"] == self.refuse for entry in entries):
            raise AuditRejected("HTTP 422")
        self.sent.extend(entry["id"] for entry in entries)


async def test_audit_facts_are_queued_with_request_metadata(trace_engine, auth_stores):
    before = now()
    assert await record_audit("admin", "admin.trajectory.payload", target_id="session_a_1",
                              details={"payload_id": "pld_1", "through_seq": "3"}, request=viewer_request())
    (row,) = await outbox()
    payload = row.payload
    assert re.fullmatch(r"[0-9a-f]{32}", payload.pop("id"))
    created = datetime.fromisoformat(payload.pop("created_at").replace("Z", "+00:00"))
    assert before - timedelta(seconds=1) <= created <= now()
    assert payload == {"user_id": "admin", "workspace_id": None, "action": "admin.trajectory.payload",
                       "resource_type": "trajectory", "resource_id": "session_a_1",
                       "details": {"payload_id": "pld_1", "through_seq": "3"}, "ip_address": "10.0.0.7",
                       "user_agent": "fixture-agent/1.0"}
    assert row.attempts == 0


async def test_list_and_view_are_recorded_once_per_viewer_session_and_minute(trace_engine, auth_stores, monkeypatch):
    moment = [0.0]
    monkeypatch.setattr(trajectory_auth, "_clock", lambda: moment[0])
    calls = [("admin", "admin.trajectory.list", None), ("admin", "admin.trajectory.list", None),
             ("admin", "admin.trajectory.view", "s1"), ("admin", "admin.trajectory.view", "s1"),
             ("admin", "admin.trajectory.view", "s2"), ("other", "admin.trajectory.view", "s1"),
             ("admin", "admin.trajectory.payload", "s1"), ("admin", "admin.trajectory.payload", "s1"),
             ("admin", "admin.trajectory.export", "s1"), ("admin", "admin.trajectory.download", "s1"),
             ("admin", "trajectory.subscribe", "trj_1"), ("admin", "trajectory.subscribe", "trj_1")]
    written = [await record_audit(user_id, action, target_id=target) for user_id, action, target in calls]
    assert written == [True, False, True, False, True, True, True, True, True, True, True, True]
    moment[0] = 59.9
    assert not await record_audit("admin", "admin.trajectory.list")
    moment[0] = 60.0
    assert await record_audit("admin", "admin.trajectory.list")
    assert len(await outbox()) == 11


async def test_a_failed_audit_write_never_raises_or_suppresses_the_next_attempt(trace_url, auth_stores):
    assert await record_audit("admin", "admin.trajectory.view", target_id="s1") is False
    init_trace_engine(trace_url)
    try:
        assert await record_audit("admin", "admin.trajectory.view", target_id="s1") is True
        assert len(await outbox()) == 1
    finally:
        await close_trace_engine()


async def test_delivery_writes_business_audit_logs_and_empties_the_outbox(trace_engine, internal_backend, business_db,
                                                                         auth_stores):
    for action, target in (("admin.trajectory.list", None), ("admin.trajectory.view", "session_a_1"),
                           ("trajectory.subscribe", "trj_a1")):
        await record_audit("admin", action, target_id=target, details={"through_seq": "3"}, request=viewer_request())
    queued = {row.payload["id"]: row.payload for row in await outbox()}
    client = internal_backend.client()
    try:
        assert await AuditDelivery(client).deliver_once() == 3
        assert await AuditDelivery(client).deliver_once() == 0
    finally:
        await client.close()
    assert await outbox() == []
    rows = await audit_logs(business_db)
    assert {row.id for row in rows} == set(queued)
    for row in rows:
        fact = queued[row.id]
        assert (row.user_id, row.workspace_id, row.action, row.resource_type, row.resource_id, row.details,
                row.ip_address, row.user_agent) == (
            fact["user_id"], None, fact["action"], "trajectory", fact["resource_id"], {"through_seq": "3"},
            "10.0.0.7", "fixture-agent/1.0")
        created = datetime.fromisoformat(fact["created_at"].replace("Z", "+00:00"))
        assert row.created_at.replace(tzinfo=timezone.utc) == created


async def test_unreachable_backend_backs_off_and_redelivery_is_idempotent(trace_engine, internal_backend, business_db,
                                                                         auth_stores):
    await record_audit("admin", "admin.trajectory.export", target_id="session_a_1", details={"export_id": "exp_1"})
    client = internal_backend.client()
    try:
        internal_backend.down = True
        started = now()
        assert await AuditDelivery(client).deliver_once() == 0
        (row,) = await outbox()
        assert row.attempts == 1
        retry_at = row.next_attempt_at.replace(tzinfo=timezone.utc)
        assert started + timedelta(seconds=4) <= retry_at <= now() + timedelta(seconds=6)
        internal_backend.down = False
        assert await AuditDelivery(client).deliver_once() == 0  # not due yet
        async with trace_session() as db:
            await db.execute(update(TrajectoryAuditOutbox).values(next_attempt_at=now() - timedelta(seconds=1)))
        payload = row.payload
        await client.audit([payload])  # an earlier attempt reached the backend after all
        assert await AuditDelivery(client).deliver_once() == 1
    finally:
        await client.close()
    assert await outbox() == []
    assert [row.id for row in await audit_logs(business_db)] == [payload["id"]]


async def test_a_refused_entry_does_not_hold_back_its_batch(trace_engine, internal_backend, business_db, auth_stores):
    await record_audit("admin", "admin.trajectory.payload", target_id="s1")
    async with trace_session() as db:
        db.add(TrajectoryAuditOutbox(payload={"id": "f" * 32, "user_id": "admin", "action": "admin.session.delete",
                                              "created_at": "2026-09-14T00:00:00.000Z"},
                                     attempts=0, next_attempt_at=now(), created_at=now()))
    await record_audit("admin", "admin.trajectory.download", target_id="s1")
    client = internal_backend.client()
    try:
        assert await AuditDelivery(client).deliver_once() == 2
    finally:
        await client.close()
    (left,) = await outbox()
    assert left.payload["action"] == "admin.session.delete" and left.attempts == 1
    assert left.next_attempt_at.replace(tzinfo=timezone.utc) > now()
    assert sorted(row.action for row in await audit_logs(business_db)) == [
        "admin.trajectory.download", "admin.trajectory.payload"]


async def test_concurrent_deliverers_never_send_a_row_twice(trace_engine, auth_stores):
    for index in range(150):
        await record_audit("admin", "admin.trajectory.payload", target_id=f"s{index}")
    backend = RecordingBackend(delay=0.05)
    delivered = await asyncio.gather(AuditDelivery(backend).deliver_once(), AuditDelivery(backend).deliver_once())
    while await AuditDelivery(backend).deliver_once():
        pass
    assert sum(delivered) <= 150 and len(backend.sent) == len(set(backend.sent)) == 150
    assert await outbox() == []


async def test_the_delivery_loop_runs_until_stopped(trace_engine, auth_stores):
    backend = RecordingBackend()
    delivery = AuditDelivery(backend, interval=0.02)
    task = delivery.start()
    assert delivery.start() is task
    await record_audit("admin", "admin.trajectory.payload", target_id="s1")
    for _ in range(200):
        if backend.sent:
            break
        await asyncio.sleep(0.01)
    assert len(backend.sent) == 1
    await delivery.stop()
    assert task.cancelled()
    await delivery.stop()


async def test_failed_rows_back_off_exponentially_to_an_hour(trace_engine, auth_stores):
    assert [AuditDelivery.backoff(attempts) for attempts in (0, 1, 2, 3, 10, 11, 10_000)] == [
        5.0, 5.0, 10.0, 20.0, 2560.0, 3600.0, 3600.0]
    await record_audit("admin", "admin.trajectory.payload", target_id="s1")
    backend = RecordingBackend(down=True)
    for attempt in range(1, 4):
        async with trace_session() as db:
            await db.execute(update(TrajectoryAuditOutbox).values(next_attempt_at=now() - timedelta(seconds=1)))
        before = now()
        assert await AuditDelivery(backend).deliver_once() == 0
        (row,) = await outbox()
        assert row.attempts == attempt
        delay = (row.next_attempt_at.replace(tzinfo=timezone.utc) - before).total_seconds()
        assert AuditDelivery.backoff(attempt) - 1 <= delay <= AuditDelivery.backoff(attempt) + 1


async def test_in_process_backend_writes_and_validates_audit_entries(business_db):
    entry = {"id": "a" * 32, "user_id": "admin", "workspace_id": None, "action": "admin.trajectory.view",
             "resource_type": "trajectory", "resource_id": "session_a_1", "details": {"through_seq": "1"},
             "ip_address": None, "user_agent": None, "created_at": "2026-09-14T08:00:00.000Z"}
    await LocalBackend().audit([entry])
    await LocalBackend().audit([entry])
    assert [row.id for row in await audit_logs(business_db)] == ["a" * 32]
    with pytest.raises(AuditRejected):
        await LocalBackend().audit([{**entry, "id": "b" * 32, "action": "admin.users.delete"}])
