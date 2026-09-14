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
    for path, body in ((VIEWER, {"user_id": "admin"}), (AUDIT, {"entries": []})):
        for headers in ({}, {"X-Internal-Token": "wrong"}, {"X-Internal-Token": INTERNAL_TOKEN + "x"}):
            assert (await backend.post(path, json=body, headers=headers)).status_code == 403
            assert (await backend.post(path, json={"invalid": True}, headers=headers)).status_code == 403
        assert (await backend.post(path, json=body, headers=AUTH)).status_code == 200
        assert (await backend.post(path, json={"invalid": True}, headers=AUTH)).status_code == 422
    monkeypatch.setattr(internal, "get_config", lambda: OpenBoxConfig(internal_api_token=""))
    for path, body in ((VIEWER, {"user_id": "admin"}), (AUDIT, {"entries": []})):
        assert (await backend.post(path, json=body, headers={"X-Internal-Token": ""})).status_code == 403
    with pytest.raises(HTTPException) as caught:
        internal.internal_token("jeton-non-ascii-é")
    assert caught.value.status_code == 403


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
