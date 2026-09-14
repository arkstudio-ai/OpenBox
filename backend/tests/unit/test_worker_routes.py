"""Worker admin HTTP API: route-for-route parity with the in-process API it replaces (maps/api.md §2)."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

import trajectory.auth as trajectory_auth
from tests.unit.test_worker_app_harness import (MEDIA_SHA, PREFIX, PRIVATE, SESSION, SYSTEM_PROMPT,  # noqa: F401
    SYSTEM_SHA, admin_env, auth_stores, business_db, internal_backend, token, trace_url, worker)
from trajectory.auth import NoStoreRoute
from trajectory.storage import blob_key
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryExport, TrajectoryPayload
from trajectory.types import CorruptContent, OwnershipError
from trajectory.worker import routes

CONTRACT = {
    ("GET", f"{PREFIX}/sessions"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/events"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/records"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/records/{{record_id:path}}"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/checkpoint"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/search"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/payloads/{{payload_id}}"),
    ("POST", f"{PREFIX}/sessions/{{session_id}}/export"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/exports/{{export_id}}"),
    ("GET", f"{PREFIX}/sessions/{{session_id}}/exports/{{export_id}}/download"),
    ("POST", f"{PREFIX}/ticket"),
    # Additive (SPEC §8.12).
    ("GET", f"{PREFIX}/sessions/{{session_id}}/blobs/{{sha256}}"),
}


def _client(worker, headers=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://testserver",
                             headers=headers or {})


def _audited(rows, action):
    return [row for row in rows if row.action == action]


async def test_route_table_is_the_admin_contract_plus_additive_routes():
    from trajectory.worker.app import create_app
    app = create_app()
    http = {(method, route.path) for route in app.routes if isinstance(route, APIRoute)
            and route.path.startswith(PREFIX) for method in route.methods}
    assert http == CONTRACT
    assert all(isinstance(route, NoStoreRoute) for route in app.routes
               if isinstance(route, APIRoute) and route.path.startswith(PREFIX))
    assert [route.path for route in app.routes if isinstance(route, APIWebSocketRoute)] == ["/ws/admin/trajectories"]


async def test_authorization_wins_over_validation_and_every_answer_is_no_store(worker, monkeypatch):
    async with _client(worker) as anonymous:
        for path in ("/sessions?limit=invalid", "/sessions/session_a_1/events?until_seq=invalid",
                     "/sessions/session_a_1/blobs/NOT-A-SHA", "/sessions/session_a_1/records/x?expand=none"):
            response = await anonymous.get(PREFIX + path)
            assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    async with _client(worker, {"Authorization": f"Bearer {token('a', 'admin')}"}) as member:
        for method, path in (("GET", "/sessions?limit=0"), ("GET", "/sessions/session_a_1"),
                             ("GET", "/sessions/session_a_1/blobs/NOT-A-SHA"),
                             ("GET", "/sessions/session_a_1/payloads/pld_media?meta=1"),
                             ("POST", "/sessions/session_a_1/export"), ("POST", "/ticket")):
            response = await member.request(method, PREFIX + path)
            assert response.status_code == 403, response.text
            assert response.json()["detail"] == "Platform administrator access required"
            assert response.headers["cache-control"] == "no-store"
    monkeypatch.setenv("TRAJECTORY_ADMIN_ENABLED", "false")
    trajectory_auth.clear_viewer_cache()
    response = await worker.client.get(PREFIX + "/sessions?limit=invalid")
    assert response.status_code == 404 and response.json()["detail"] == "Trajectory administration is disabled"
    assert response.headers["cache-control"] == "no-store"


async def test_listing_passes_every_filter_and_is_audited_once_per_minute(worker, monkeypatch):
    moment = [1000.0]
    monkeypatch.setattr(trajectory_auth, "_clock", lambda: moment[0])
    params = {"user_id": "a", "user_query": "a@", "q": "Session", "workspace_id": "ws_a", "status": "idle",
              "recording_status": "recording", "activity_from": "2026-09-01T00:00:00Z",
              "activity_to": "2026-09-30T00:00:00+08:00", "include_unrecorded": "true", "limit": "1",
              "sort": "last_activity_asc", "cursor": "offset-0"}
    response = await worker.client.get(PREFIX + "/sessions", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [item["session_id"] for item in body["items"]] == ["session_a_1"] and body["has_more"] is True
    (_, kwargs), = worker.layer.called("list_sessions")
    assert kwargs == {"user_id": "a", "user_query": "a@", "q": "Session", "workspace_id": "ws_a", "status": "idle",
                      "recording_status": "recording",
                      "activity_from": datetime(2026, 9, 1, tzinfo=timezone.utc),
                      "activity_to": datetime(2026, 9, 30, tzinfo=timezone(timedelta(hours=8))),
                      "include_unrecorded": True, "cursor": "offset-0", "limit": 1, "sort": "last_activity_asc"}
    next_page = await worker.client.get(PREFIX + "/sessions", params={**params, "cursor": body["next_cursor"]})
    assert [item["session_id"] for item in next_page.json()["items"]] == ["session_a_2"]
    for bad in ({"sort": "title"}, {"cursor": "forged"}):
        response = await worker.client.get(PREFIX + "/sessions", params=bad)
        assert response.status_code == 400 and response.json()["detail"]["code"] == "trajectory_invalid"
        assert response.headers["cache-control"] == "no-store"
    for bad in ({"limit": "0"}, {"limit": "201"}, {"activity_from": "yesterday"}):
        assert (await worker.client.get(PREFIX + "/sessions", params=bad)).status_code == 422
    listed = _audited(await worker.delivered_audit(), "admin.trajectory.list")
    assert len(listed) == 1
    assert (listed[0].user_id, listed[0].resource_type, listed[0].resource_id) == ("admin", "trajectory", None)
    assert listed[0].details == {"user_id": "a", "workspace_id": "ws_a"}
    assert listed[0].ip_address == "127.0.0.1" and "python-httpx" in listed[0].user_agent
    moment[0] += 61
    await worker.client.get(PREFIX + "/sessions")
    assert len(_audited(await worker.delivered_audit(), "admin.trajectory.list")) == 2


async def test_session_header_adds_refs_capability_and_views_are_audited_per_session(worker, monkeypatch):
    moment = [5000.0]
    monkeypatch.setattr(trajectory_auth, "_clock", lambda: moment[0])
    response = await worker.client.get(SESSION)
    assert response.status_code == 200
    header = response.json()
    assert header["capabilities"] == {"recording": True, "admin_read": True, "export": True, "refs": True}
    assert (header["trajectory_id"], header["committed_seq"], header["through_seq"]) == ("trj_a1", "3", "3")
    assert (await worker.client.get(SESSION, params={"through_seq": "2"})).json()["through_seq"] == "2"
    unrecorded = (await worker.client.get(PREFIX + "/sessions/session_a_2")).json()
    assert unrecorded["trajectory_id"] is None and unrecorded["recording_status"] == "not_recorded"
    assert unrecorded["committed_seq"] == "0" and unrecorded["capabilities"]["refs"] is True
    for through in ("4", "x"):
        response = await worker.client.get(SESSION, params={"through_seq": through})
        assert response.status_code == 400 and response.json()["detail"]["code"] == "trajectory_invalid"
    missing = await worker.client.get(PREFIX + "/sessions/never")
    assert missing.status_code == 404 and missing.json() == {"detail": "Session not found"}
    views = _audited(await worker.delivered_audit(), "admin.trajectory.view")
    assert sorted(row.resource_id for row in views) == ["session_a_1", "session_a_2"]
    assert next(row for row in views if row.resource_id == "session_a_1").details == {"through_seq": "3"}
    moment[0] += 60
    await worker.client.get(SESSION)
    assert len(_audited(await worker.delivered_audit(), "admin.trajectory.view")) == 3


async def test_events_records_checkpoint_and_search_parameters(worker):
    events = await worker.client.get(SESSION + "/events")
    assert events.status_code == 200
    body = events.json()
    assert [event["seq"] for event in body["events"]] == ["1", "2", "3"]
    assert {key: body[key] for key in ("from_seq", "through_seq", "until_seq", "has_more", "committed_seq")} == {
        "from_seq": "1", "through_seq": "3", "until_seq": "3", "has_more": False, "committed_seq": "3"}
    (_, kwargs), = worker.layer.called("read_events")
    assert kwargs == {"after_seq": "0", "until_seq": None, "limit": 500, "include_data": True}
    await worker.client.get(SESSION + "/events", params={"after_seq": "1", "until_seq": "2", "limit": "2000",
                                                         "include_data": "false"})
    assert worker.layer.called("read_events")[-1][1] == {"after_seq": "1", "until_seq": "2", "limit": 2000,
                                                          "include_data": False}
    for bad in ({"limit": "2001"}, {"limit": "0"}, {"include_data": "maybe"}):
        assert (await worker.client.get(SESSION + "/events", params=bad)).status_code == 422
    assert (await worker.client.get(SESSION + "/events", params={"after_seq": "3", "until_seq": "2"})).status_code == 400
    assert (await worker.client.get(PREFIX + "/sessions/session_a_2/events")).json() == {
        "detail": "Session has not started recording"}

    records = await worker.client.get(SESSION + "/records", params={"kind": "artifact", "status": "completed",
                                                                    "agent_id": "", "limit": "500"})
    assert records.status_code == 200 and {item["kind"] for item in records.json()["items"]} == {"artifact"}
    assert all("data" not in item and "blocks" not in item for item in records.json()["items"])
    assert worker.layer.called("list_records")[-1][1] == {"through_seq": None, "before": None, "limit": 500,
                                                           "kind": "artifact", "status": "completed", "agent_id": ""}
    assert (await worker.client.get(SESSION + "/records", params={"limit": "501"})).status_code == 422
    assert (await worker.client.get(SESSION + "/records", params={"before": "x"})).status_code == 400

    checkpoint = (await worker.client.get(SESSION + "/checkpoint")).json()
    assert checkpoint["through_seq"] == "3" and checkpoint["checkpoint"]["through_seq"] == "2"
    assert (await worker.client.get(SESSION + "/checkpoint", params={"at_seq": "1"})).json() == {
        "checkpoint": None, "through_seq": "1"}
    assert (await worker.client.get(SESSION + "/checkpoint", params={"at_seq": "9"})).status_code == 400

    hits = await worker.client.get(SESSION + "/search", params={"q": "race", "through_seq": "3", "limit": "200"})
    assert [hit["record_id"] for hit in hits.json()["items"]] == ["artifact:race_asset"]
    assert worker.layer.called("search")[-1][1] == {"q": "race", "through_seq": "3", "cursor": None, "limit": 200}
    for bad in ({}, {"q": ""}, {"q": "x" * 501}, {"q": "race", "limit": "201"}):
        assert (await worker.client.get(SESSION + "/search", params=bad)).status_code == 422


async def test_record_detail_expands_refs_by_default_and_keeps_them_on_request(worker):
    full = await worker.client.get(SESSION + "/records/request:req_a")
    assert full.status_code == 200 and full.headers["cache-control"] == "no-store"
    assert full.json()["record"]["data"]["input"]["system"] == SYSTEM_PROMPT
    assert [event["seq"] for event in full.json()["record"]["events"]] == ["2", "3"]
    refs = (await worker.client.get(SESSION + "/records/request:req_a", params={"expand": "refs"})).json()
    assert refs["record"]["data"]["input"]["system"]["$ref"]["sha256"] == SYSTEM_SHA
    assert [kwargs["expand"] for _, kwargs in worker.layer.called("get_record")] == ["full", "refs"]
    assert (await worker.client.get(SESSION + "/records/request:req_a", params={"expand": "none"})).status_code == 422
    early = await worker.client.get(SESSION + "/records/artifact:race_asset", params={"through_seq": "2"})
    assert early.status_code == 404 and early.json() == {"detail": "Record is not available at this position"}


async def test_payload_download_is_audited_and_revalidated_after_the_read(worker):
    assert (await worker.client.get(SESSION)).status_code == 200  # the viewer's facts are cached now
    viewer_calls = worker.backend.viewer_calls()
    response = await worker.client.get(SESSION + "/payloads/pld_media")
    assert response.status_code == 200 and response.content == PRIVATE
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == 'attachment; filename="pld_media"'
    # The dependency used the cached facts; revalidation after the read asked the backend again.
    assert worker.backend.viewer_calls() == viewer_calls + 1
    payloads = _audited(await worker.delivered_audit(), "admin.trajectory.payload")
    assert [(row.resource_id, row.details) for row in payloads] == [
        ("session_a_1", {"payload_id": "pld_media", "through_seq": "3"})]
    await worker.client.get(SESSION + "/payloads/pld_media")
    assert len(_audited(await worker.delivered_audit(), "admin.trajectory.payload")) == 2
    asset = await worker.client.get(SESSION + "/payloads/pld_asset")
    assert asset.status_code == 200 and asset.content == PRIVATE  # asset references have no hash to compare
    assert (await worker.client.get(SESSION + "/payloads/pld_media", params={"through_seq": "2"})).status_code == 404
    assert (await worker.client.get(PREFIX + "/sessions/session_b_1/payloads/pld_media")).status_code == 404
    worker.blob.objects[blob_key("trj_a1", MEDIA_SHA)] = b"tampered"
    corrupt = await worker.client.get(SESSION + "/payloads/pld_media")
    assert corrupt.status_code == 409 and corrupt.json()["detail"]["code"] == "trajectory_corrupt"
    assert corrupt.headers["cache-control"] == "no-store" and PRIVATE not in corrupt.content


async def test_payload_meta_reports_availability_without_bytes_or_audit(worker):
    response = await worker.client.get(SESSION + "/payloads/pld_media", params={"meta": "1"})
    assert response.status_code == 200
    assert response.json() == {"payload_id": "pld_media", "availability": "available", "media_type": "image/png",
                               "size_bytes": len(PRIVATE), "sha256": MEDIA_SHA}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert worker.blob.gets == 0
    early = await worker.client.get(SESSION + "/payloads/pld_media", params={"meta": "1", "through_seq": "2"})
    assert early.status_code == 404
    async with trace_session() as db:
        (await db.get(TrajectoryPayload, "pld_media")).availability = "deleted"
    deleted = await worker.client.get(SESSION + "/payloads/pld_media", params={"meta": "true"})
    assert deleted.status_code == 410 and deleted.json()["detail"]["code"] == "trajectory_content_deleted"
    assert not _audited(await worker.delivered_audit(), "admin.trajectory.payload")


async def test_blob_endpoint_returns_the_ref_value_at_the_watermark(worker):
    response = await worker.client.get(SESSION + f"/blobs/{SYSTEM_SHA}", params={"through_seq": "2"})
    assert response.status_code == 200, response.text
    assert response.json() == SYSTEM_PROMPT
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    viewer_calls = worker.backend.viewer_calls()
    assert (await worker.client.get(SESSION + f"/blobs/{SYSTEM_SHA}")).status_code == 200
    assert worker.backend.viewer_calls() == viewer_calls + 1  # revalidated after the read
    early = await worker.client.get(SESSION + f"/blobs/{SYSTEM_SHA}", params={"through_seq": "1"})
    assert early.status_code == 404 and early.headers["cache-control"] == "no-store"
    assert (await worker.client.get(SESSION + "/blobs/" + "e" * 64)).status_code == 404
    for malformed in (SYSTEM_SHA.upper(), SYSTEM_SHA[:-1], SYSTEM_SHA + "0"):
        response = await worker.client.get(SESSION + "/blobs/" + malformed)
        assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    worker.blob.objects[blob_key("trj_a1", SYSTEM_SHA)] = b"not zstd"
    from trajectory.payload import reset_blob_cache
    reset_blob_cache()  # the value served above was verified when it entered the cache
    assert (await worker.client.get(SESSION + f"/blobs/{SYSTEM_SHA}")).status_code == 409
    async with trace_session() as db:
        (await db.get(TrajectoryPayload, "pld_system")).availability = "expired"
    gone = await worker.client.get(SESSION + f"/blobs/{SYSTEM_SHA}")
    assert gone.status_code == 410 and gone.headers["cache-control"] == "no-store"


async def test_blob_content_that_does_not_match_its_address_is_never_served(worker):
    from trajectory.storage import encode_blob
    from trajectory.types import canonical
    tampered, _ = encode_blob(canonical({"role": "system", "content": "tampered prompt " * 100}), "application/json")
    worker.blob.objects[blob_key("trj_a1", SYSTEM_SHA)] = tampered
    response = await worker.client.get(SESSION + f"/blobs/{SYSTEM_SHA}")
    assert response.status_code == 409 and response.json()["detail"] == {
        "code": "trajectory_corrupt", "message": "Trajectory content digest mismatch"}
    assert b"tampered" not in response.content and response.headers["cache-control"] == "no-store"


async def test_export_create_status_and_download_contract(worker, monkeypatch):
    missing_body = await worker.client.post(SESSION + "/export")
    assert missing_body.status_code == 422 and missing_body.headers["cache-control"] == "no-store"
    assert (await worker.client.post(SESSION + "/export", json={"through_seq": "9"})).status_code == 400
    created = await worker.client.post(SESSION + "/export", json={"through_seq": "2"})
    assert created.status_code == 202 and created.headers["cache-control"] == "no-store"
    body = created.json()
    assert body == {"export_id": body["export_id"], "status": "pending", "through_seq": "2", "error": None,
                    "download_url": None}
    async with trace_session() as db:
        row = await db.get(TrajectoryExport, body["export_id"])
        assert (row.trajectory_id, row.viewer_id, row.through_seq, row.status) == ("trj_a1", "admin", 2, "pending")
    status = await worker.client.get(SESSION + f"/exports/{body['export_id']}")
    assert status.status_code == 200 and status.json() == body
    not_ready = await worker.client.get(SESSION + f"/exports/{body['export_id']}/download")
    assert not_ready.status_code == 409 and not_ready.json() == {"detail": "Export is not ready"}
    assert (await worker.client.get(PREFIX + f"/sessions/session_b_1/exports/{body['export_id']}")).json() == {
        "detail": "Export does not belong to this trajectory"}

    ready = (await worker.client.get(SESSION + "/exports/exp_ready")).json()
    assert ready["status"] == "completed" and ready["download_url"] == SESSION + "/exports/exp_ready/download"
    download = await worker.client.get(ready["download_url"])
    assert download.status_code == 200 and PRIVATE in download.content
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"] == 'attachment; filename="exp_ready.zip"'
    actions = sorted((row.action, row.resource_id, row.details) for row in await worker.delivered_audit()
                     if row.action in {"admin.trajectory.export", "admin.trajectory.download"})
    assert actions == [("admin.trajectory.download", "session_a_1", {"export_id": "exp_ready"}),
                       ("admin.trajectory.export", "session_a_1", {"through_seq": "2", "export_id": body["export_id"]})]

    # Without a watermark the export is taken at the committed head.
    assert (await worker.client.post(SESSION + "/export", json={})).json()["through_seq"] == "3"

    async with trace_session() as db:
        (await db.get(TrajectoryExport, "exp_ready")).sha256 = "0" * 64
    tampered = await worker.client.get(ready["download_url"])
    assert tampered.status_code == 409 and PRIVATE not in tampered.content


async def test_exports_are_invalidated_by_later_content_deletion_or_expiry(worker):
    async with trace_session() as db:
        (await db.get(SessionTrajectory, "trj_a1")).content_expired_at = datetime.now(timezone.utc)
    expired = await worker.client.get(SESSION + "/exports/exp_ready/download")
    assert expired.status_code == 410 and PRIVATE not in expired.content
    async with trace_session() as db:
        (await db.get(SessionTrajectory, "trj_a1")).content_expired_at = None
        payload = await db.get(TrajectoryPayload, "pld_asset")
        payload.availability, payload.deleted_at = "deleted", datetime.now(timezone.utc)
    deleted = await worker.client.get(SESSION + "/exports/exp_ready/download")
    assert deleted.status_code == 410
    assert deleted.json()["detail"]["message"] == "Export invalidated by explicit content deletion; create a new export"


@pytest.mark.parametrize("error,status,detail", [
    (FileNotFoundError("gone"), 410, {"code": "trajectory_content_deleted", "message": "gone"}),
    (LookupError("missing"), 404, "missing"),
    (KeyError("key"), 404, "'key'"),
    (CorruptContent("bad digest"), 409, {"code": "trajectory_corrupt", "message": "bad digest"}),
    (OwnershipError("not yours"), 400, {"code": "trajectory_ownership", "message": "not yours"}),
])
async def test_read_errors_map_to_the_contract_statuses(worker, monkeypatch, error, status, detail):
    async def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr("trajectory.repository.list_records", failing)
    response = await worker.client.get(SESSION + "/records")
    assert response.status_code == status and response.json() == {"detail": detail}
    assert response.headers["cache-control"] == "no-store"