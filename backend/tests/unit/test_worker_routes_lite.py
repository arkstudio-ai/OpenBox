"""Lite view routes on the worker (docs/SESSION_TRAJECTORY_LITE_PLAN.md §6.3): authorization, audit, shapes."""
from datetime import datetime, timezone

import httpx

from tests.unit.test_worker_app_harness import (PREFIX, admin_env, auth_stores, business_db, internal_backend,  # noqa: F401
    token, trace_url, worker)
from trajectory.store.database import trace_session
from trajectory.store.models import TrajectoryRecord, TrajectoryTurnSummary

AT = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


async def _seed_turn(trajectory_id="trj_a1", session_id="session_a_1", user_id="a", turn_id="turn_1", status="failed"):
    async with trace_session() as db:
        db.add(TrajectoryTurnSummary(trajectory_id=trajectory_id, turn_id=turn_id, session_id=session_id, user_id=user_id,
            workspace_id=f"ws_{user_id}", started_at=AT, finished_at=AT, last_activity_at=AT, duration_ms=1200,
            status=status, status_reason="login_expired", waiting_kind=None, user_preview="发视频",
            assistant_preview="登录过期", tool_counts={"desktop_publish": {"total": 1, "failed": 1}}, tool_failures=1,
            request_count=1, input_tokens=10, output_tokens=2, usage_complete=True, first_seq=1, last_seq=3, applied_seq=3))
        await db.execute(TrajectoryRecord.__table__.update().where(TrajectoryRecord.trajectory_id == trajectory_id,
            TrajectoryRecord.record_id == "request:req_a").values(turn_id=turn_id))


def _audited(rows, action):
    return [row for row in rows if row.action == action]


async def test_lite_routes_require_a_platform_admin_and_never_cache(worker):
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://testserver")
    member = httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://testserver",
                               headers={"Authorization": f"Bearer {token('a', 'admin')}"})
    async with anonymous, member:
        for path in ("/users", "/users/a/turns", "/sessions/session_a_1/turns/turn_1", "/lite/labels"):
            response = await anonymous.get(PREFIX + path)
            assert response.status_code == 401 and response.headers["cache-control"] == "no-store", path
            response = await member.get(PREFIX + path)
            assert response.status_code == 403, path
            assert response.json()["detail"] == "Platform administrator access required"


async def test_users_turns_and_turn_detail_are_served_and_audited(worker):
    await _seed_turn()
    users = await worker.client.get(PREFIX + "/users", params={"has_failures": "true", "limit": "5"})
    assert users.status_code == 200, users.text
    assert users.headers["cache-control"] == "no-store"
    body = users.json()
    assert [item["user"]["user_id"] for item in body["items"]] == ["a"] and not body["has_more"]
    assert body["items"][0]["user"]["email"] == "a@example.test"
    assert (body["items"][0]["turns"], body["items"][0]["failed_turns"], body["items"][0]["current_status"]) == (1, 1, "failed")

    turns = await worker.client.get(PREFIX + "/users/a/turns", params=[("status", "failed"), ("status", "waiting")])
    assert turns.status_code == 200, turns.text
    items = turns.json()["items"]
    assert [item["turn_id"] for item in items] == ["turn_1"]
    assert items[0]["session_title"] == "Session session_a_1" and items[0]["status_reason"] == "login_expired"
    assert items[0]["tool_counts"] == {"desktop_publish": {"total": 1, "failed": 1}}
    assert (await worker.client.get(PREFIX + "/users/a/turns", params={"status": "done"})).status_code == 400
    assert (await worker.client.get(PREFIX + "/users", params={"limit": "0"})).status_code == 422

    detail = await worker.client.get(PREFIX + "/sessions/session_a_1/turns/turn_1")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["summary"]["turn_id"] == "turn_1" and payload["through_seq"] == "3"
    assert [step["record_id"] for step in payload["steps"]] == ["request:req_a"]
    assert payload["steps"][0]["step_type"] == "hidden"
    missing = await worker.client.get(PREFIX + "/sessions/session_a_1/turns/nope")
    assert missing.status_code == 404 and missing.json() == {"detail": "Turn not found"}
    assert (await worker.client.get(PREFIX + "/sessions/never/turns/turn_1")).status_code == 404

    labels = await worker.client.get(PREFIX + "/lite/labels")
    assert labels.status_code == 200 and labels.json()["tools"]["desktop_publish"]["label"] == "发布到抖音（云电脑）"

    rows = await worker.delivered_audit()
    assert len(_audited(rows, "admin.trajectory.lite_users")) == 1
    assert _audited(rows, "admin.trajectory.lite_users")[0].details == {"q": None, "has_failures": True, "active_only": False}
    assert len(_audited(rows, "admin.trajectory.lite_turns")) >= 1
    turn_views = _audited(rows, "admin.trajectory.lite_turn")
    assert [row.resource_id for row in turn_views] == ["session_a_1"] and turn_views[0].details == {"turn_id": "turn_1"}


async def test_records_accept_the_additive_turn_filter(worker):
    await _seed_turn()
    plain = await worker.client.get(PREFIX + "/sessions/session_a_1/records")
    filtered = await worker.client.get(PREFIX + "/sessions/session_a_1/records", params={"turn_id": "turn_1"})
    assert plain.status_code == filtered.status_code == 200
    # The read layer stand-in records the call: without the parameter nothing new is passed.
    kwargs = [kwargs for _, kwargs in worker.layer.called("list_records")]
    assert "turn_id" not in kwargs[0] and kwargs[1]["turn_id"] == "turn_1"
