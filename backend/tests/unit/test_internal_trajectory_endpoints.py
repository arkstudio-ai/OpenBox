"""Backend callbacks for the trajectory worker: viewer introspection and audit delivery, guarded like tunnel-keys."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import api.internal as internal
from core.config import OpenBoxConfig
from db.models.audit_log import AuditLog
from db.models.push import MobileSession
from tests.unit.test_worker_app_harness import INTERNAL_TOKEN, admin_env, business_db, internal_backend  # noqa: F401
from trajectory.types import now

AUTH = {"X-Internal-Token": INTERNAL_TOKEN}
VIEWER = "/api/internal/trajectory/viewer"
AUDIT = "/api/internal/trajectory/audit"
AUDIENCE = "/api/internal/trajectory/session-audience"


@pytest.fixture
async def backend(internal_backend, admin_env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_backend.app),
                                 base_url="http://backend") as client:
        yield client


def entry(identity: str, **fields) -> dict:
    return {"id": identity, "user_id": "admin", "workspace_id": None, "action": "admin.trajectory.view",
            "resource_type": "trajectory", "resource_id": "session_a_1", "details": {"through_seq": "3"},
            "ip_address": "10.0.0.7", "user_agent": "agent", "created_at": "2026-09-14T08:00:00.123Z", **fields}


async def test_callbacks_require_the_internal_token_before_any_validation(backend, monkeypatch):
    requests = ((VIEWER, {"user_id": "admin"}), (AUDIT, {"entries": []}),
                (AUDIENCE, {"user_id": "admin", "targets": []}))
    for path, body in requests:
        for headers in ({}, {"X-Internal-Token": "wrong"}, {"X-Internal-Token": INTERNAL_TOKEN + "x"}):
            assert (await backend.post(path, json=body, headers=headers)).status_code == 403
            assert (await backend.post(path, json={"invalid": True}, headers=headers)).status_code == 403
        assert (await backend.post(path, json=body, headers=AUTH)).status_code == 200
        assert (await backend.post(path, json={"invalid": True}, headers=AUTH)).status_code == 422
    monkeypatch.setattr(internal, "get_config", lambda: OpenBoxConfig(internal_api_token=""))
    for path, body in requests:
        assert (await backend.post(path, json=body, headers={"X-Internal-Token": ""})).status_code == 403
    with pytest.raises(HTTPException) as caught:
        internal.internal_token("jeton-non-ascii-é")
    assert caught.value.status_code == 403


async def test_session_audience_callback_validates_original_scope_and_batch_contract(backend):
    target = {"session_id": "session_a_1", "user_id": "a", "workspace_id": "ws_a"}
    body = {"user_id": "admin", "targets": [target]}
    response = await backend.post(AUDIENCE, json=body, headers=AUTH)
    # v2 also certifies recorded source bindings; workers reject v1 replies.
    assert response.json() == {"version": 2, "user_id": "admin", "allowed": ["session_a_1"]}
    for wrong in ({"workspace_id": "ws_b"}, {"user_id": "b"}, {"session_id": "absent"}):
        response = await backend.post(AUDIENCE, json={**body, "targets": [{**target, **wrong}]}, headers=AUTH)
        assert response.status_code == 200 and response.json()["allowed"] == []
    for invalid in ([target, target], [{**target, "session_id": str(i)} for i in range(201)]):
        assert (await backend.post(AUDIENCE, json={**body, "targets": invalid}, headers=AUTH)).status_code == 422


async def test_viewer_facts_follow_account_mobile_session_and_allowlist_rules(backend, business_db, monkeypatch):
    async with business_db.begin() as db:
        db.add(MobileSession(user_id="a", session_id="sid_a", active=True, expires_at=now() + timedelta(days=1),
                             updated_at=now()))
        db.add(MobileSession(user_id="b", session_id="sid_b", active=True, expires_at=now() - timedelta(minutes=1),
                             updated_at=now()))

    async def facts(user_id, client=None, sid=None, **extra):
        response = await backend.post(VIEWER, json={"user_id": user_id, "client": client, "sid": sid, **extra},
                                      headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    assert await facts("admin", "web", jti="j1") == {"user_id": "admin", "role": "admin", "is_active": True,
                                                     "is_deleted": False, "mobile_session_valid": True,
                                                     "admin_enabled": True}
    assert (await facts("a", "mobile", "sid_a"))["mobile_session_valid"] is True
    assert (await facts("a", "mobile", "another"))["mobile_session_valid"] is False
    assert (await facts("a", None))["mobile_session_valid"] is False  # a legacy token once a phone is bound
    assert (await facts("b", "mobile", "sid_b"))["mobile_session_valid"] is False  # expired
    assert (await facts("admin", None))["mobile_session_valid"] is True
    assert (await facts("admin", "desktop-app"))["mobile_session_valid"] is False
    assert {key: value for key, value in (await facts("retired", "web")).items() if key.startswith("is_")} == {
        "is_active": True, "is_deleted": True}
    assert (await facts("suspended", "web"))["is_active"] is False
    # The allowlist is reported as configured; the account facts are what refuse an unknown user.
    assert await facts("ghost", "web") == {"user_id": "ghost", "role": None, "is_active": False, "is_deleted": True,
                                           "mobile_session_valid": True, "admin_enabled": True}
    monkeypatch.setenv("TRAJECTORY_ADMIN_USER_IDS", "default")
    assert (await facts("admin", "web"))["admin_enabled"] is False
    assert (await facts("default", "web"))["admin_enabled"] is True
    monkeypatch.setenv("TRAJECTORY_ADMIN_ENABLED", "false")
    assert (await facts("default", "web"))["admin_enabled"] is False
    for invalid in ({"user_id": ""}, {"user_id": "x" * 65}, {"user_id": "admin", "client": "c" * 33}, {}):
        assert (await backend.post(VIEWER, json=invalid, headers=AUTH)).status_code == 422


async def test_audit_batches_are_written_once_per_entry_id(backend, business_db):
    first = await backend.post(AUDIT, json={"entries": [entry("e1"), entry("e2", action="trajectory.subscribe",
                                                                           resource_id="trj_a1")]}, headers=AUTH)
    assert first.status_code == 200 and first.json() == {"accepted": 2, "written": 2}
    again = await backend.post(AUDIT, json={"entries": [entry("e1"), entry("e2"), entry("e3", user_agent="u" * 900,
                                                                                    ip_address="9" * 60)]},
                               headers=AUTH)
    assert again.json() == {"accepted": 3, "written": 1}
    async with business_db() as db:
        rows = {row.id: row for row in (await db.scalars(select(AuditLog))).all()}
    assert sorted(rows) == ["e1", "e2", "e3"]
    assert (rows["e1"].user_id, rows["e1"].action, rows["e1"].resource_type, rows["e1"].resource_id,
            rows["e1"].details, rows["e1"].ip_address, rows["e1"].user_agent) == (
        "admin", "admin.trajectory.view", "trajectory", "session_a_1", {"through_seq": "3"}, "10.0.0.7", "agent")
    assert rows["e1"].created_at.replace(tzinfo=timezone.utc) == datetime(2026, 9, 14, 8, 0, 0, 123000, tzinfo=timezone.utc)
    assert len(rows["e3"].user_agent) == 512 and len(rows["e3"].ip_address) == 45
    assert (await backend.post(AUDIT, json={"entries": []}, headers=AUTH)).json() == {"accepted": 0, "written": 0}


@pytest.mark.parametrize("body", [
    {"entries": [entry("e1", action="admin.user.delete")]},
    {"entries": [entry("e1", created_at="not a time")]},
    {"entries": [entry("", )]},
    {"entries": [entry(f"e{index}") for index in range(501)]},
    {"entries": "e1"},
])
async def test_audit_batches_outside_the_contract_are_rejected(backend, body):
    assert (await backend.post(AUDIT, json=body, headers=AUTH)).status_code == 422


async def test_naive_timestamps_are_utc_and_refused_rows_do_not_block_the_batch(business_db, monkeypatch):
    batch = internal.TrajectoryAuditBatch.model_validate({"entries": [
        entry("ok1", created_at="2026-09-14T08:00:00"), entry("bad", user_id="ghost"), entry("ok2")]})
    assert batch.entries[0].created_at.tzinfo == timezone.utc
    real = internal._insert_audit

    async def insert(entries):
        if any(item.user_id == "ghost" for item in entries):
            raise IntegrityError("INSERT INTO audit_logs", {}, Exception("foreign key"))
        return await real(entries)

    monkeypatch.setattr(internal, "_insert_audit", insert)
    assert await internal.write_trajectory_audit(batch.entries) == 2
    async with business_db() as db:
        assert sorted((await db.scalars(select(AuditLog.id))).all()) == ["ok1", "ok2"]


USAGE = "/api/internal/trajectory/usage"
ALERTS = "/api/internal/trajectory/alerts"


async def _charge(db, *, session_id, user_id, credits, created_at, status="charged"):
    from decimal import Decimal
    from db.models.billing import UsageEvent
    from core.identifier import ascending
    db.add(UsageEvent(id=ascending("usage"), idempotency_key=f"t:{ascending('k')}", workspace_id=f"ws_{user_id}",
                      user_id=user_id, session_id=session_id, message_id=None, session_title="s", model_id="m",
                      kind="chat", tokens={}, total_tokens=0, credits=Decimal(credits), status=status,
                      pricing={"version": "t"}, created_at=created_at))


async def test_usage_callback_sums_charged_credits_inside_each_window(backend, business_db):
    base = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
    async with business_db() as db:
        await _charge(db, session_id="session_a_1", user_id="a", credits="1.5", created_at=base + timedelta(seconds=10))
        await _charge(db, session_id="session_a_1", user_id="a", credits="0.25", created_at=base + timedelta(seconds=50))
        await _charge(db, session_id="session_a_1", user_id="a", credits="9", created_at=base + timedelta(minutes=10))
        await _charge(db, session_id="session_a_1", user_id="a", credits="7", created_at=base + timedelta(seconds=20), status="shadow")
        await _charge(db, session_id="session_b_1", user_id="b", credits="2", created_at=base + timedelta(seconds=30))
        await db.commit()
    windows = [
        {"key": "turn_1", "session_id": "session_a_1", "from_at": base.isoformat(), "to_at": (base + timedelta(minutes=1)).isoformat()},
        {"key": "turn_later", "session_id": "session_a_1", "from_at": (base + timedelta(minutes=5)).isoformat(), "to_at": (base + timedelta(minutes=15)).isoformat()},
        {"key": "turn_empty", "session_id": "session_a_2", "from_at": base.isoformat(), "to_at": (base + timedelta(minutes=1)).isoformat()},
        {"key": "user_a", "user_id": "a", "from_at": base.isoformat(), "to_at": (base + timedelta(hours=1)).isoformat()},
    ]
    response = await backend.post(USAGE, json={"windows": windows}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == {"credits": {"turn_1": "1.75", "turn_later": "9", "turn_empty": None, "user_a": "10.75"}}
    assert (await backend.post(USAGE, json={"windows": []}, headers=AUTH)).json() == {"credits": {}}
    for bad in ({"key": "x", "from_at": base.isoformat(), "to_at": base.isoformat()},
                {"key": "x", "session_id": "s", "user_id": "u", "from_at": base.isoformat(), "to_at": base.isoformat()},
                {"key": "x", "session_id": "s", "from_at": (base + timedelta(hours=1)).isoformat(), "to_at": base.isoformat()}):
        assert (await backend.post(USAGE, json={"windows": [bad]}, headers=AUTH)).status_code == 422
    assert (await backend.post(USAGE, json={"windows": [windows[0], windows[0]]}, headers=AUTH)).status_code == 422
    assert (await backend.post(USAGE, json={"windows": windows}, headers={"X-Internal-Token": "wrong"})).status_code == 403


async def test_alerts_callback_notifies_every_platform_admin_once_per_turn(backend, business_db):
    from db.models.notification import Notification
    turn = {"turn_id": "turn_f1", "session_id": "session_a_1", "user_id": "a", "workspace_id": "ws_a",
            "username": "张店长", "session_title": "国庆视频", "reason": "login_expired", "started_at": "2026-10-12T08:00:00Z"}
    response = await backend.post(ALERTS, json={"turns": [turn]}, headers=AUTH)
    assert response.status_code == 200, response.text
    # The harness seeds two active platform admins ("admin" and "default"); every one gets the notice.
    assert response.json() == {"alerted": 2, "admins": 2}
    # Idempotent on (admin, turn): a redelivery adds nothing.
    assert (await backend.post(ALERTS, json={"turns": [turn]}, headers=AUTH)).json() == {"alerted": 2, "admins": 2}
    async with business_db() as db:
        rows = list((await db.execute(select(Notification).where(Notification.kind == "trajectory_turn_failed"))).scalars())
    assert sorted(row.user_id for row in rows) == ["admin", "default"]
    row = next(row for row in rows if row.user_id == "admin")
    assert (row.category, row.source_key, row.workspace_id) == ("system", "trajectory:turn_f1:failed", None)
    assert row.title == "有一轮对话失败" and "张店长 · 国庆视频" in row.body and "login_expired" not in row.body
    assert row.link == {"kind": "admin_trajectory_turn", "userId": "a", "sessionId": "session_a_1", "turnId": "turn_f1"}
    assert (await backend.post(ALERTS, json={"turns": []}, headers=AUTH)).json() == {"alerted": 0, "admins": 0}
    assert (await backend.post(ALERTS, json={"turns": [{"turn_id": "x"}]}, headers=AUTH)).status_code == 422
    assert (await backend.post(ALERTS, json={"turns": [turn]}, headers={"X-Internal-Token": "wrong"})).status_code == 403
