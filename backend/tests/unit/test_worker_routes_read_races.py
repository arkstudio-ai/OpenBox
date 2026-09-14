"""The read-race matrix of tests/integration/test_trajectory_read_races.py against the worker app.

Authority and content change while the blob store is still returning bytes;
the worker must answer with the new state and never leak the bytes.
"""
from urllib.parse import quote

import pytest
from sqlalchemy import select

from auth.jwt import decode_access_token
from db.models.user import User
from tests.unit.test_worker_app_harness import (ENCODED_ARTIFACTS, PREFIX, PRIVATE, SESSION, admin_env,  # noqa: F401
    auth_stores, business_db, internal_backend, token, trace_url, worker)
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryMetaAsset, TrajectoryMetaSession, TrajectoryPayload
from trajectory.types import now


async def apply(worker, change):
    if change == "revoked":
        await worker.cache.set("jwt_bl:" + decode_access_token(worker.token)["jti"], True, ttl=60)
    elif change == "role":
        async with worker.business.begin() as db:
            (await db.get(User, "admin")).role = "user"
    else:
        async with trace_session() as db:
            if change == "asset_deleted":
                asset = await db.get(TrajectoryMetaAsset, "race_asset")
                asset.is_deleted, asset.deleted_at = True, now()
            elif change == "payload_deleted":
                # What the worker applies for an asset.deleted control (SPEC §8.5).
                for payload in (await db.scalars(select(TrajectoryPayload).where(
                        TrajectoryPayload.source_asset_id == "race_asset"))).all():
                    payload.availability, payload.deleted_at = "deleted", now()
            else:
                (await db.get(TrajectoryMetaSession, "session_a_1")).is_deleted = True
                trajectory = await db.get(SessionTrajectory, "trj_a1")
                trajectory.deleted_at, trajectory.recording_status = now(), "deleted"


@pytest.mark.parametrize("resource", ["payload", "export"])
@pytest.mark.parametrize("change,status", [
    ("role", 403), ("revoked", 401), ("asset_deleted", 410), ("payload_deleted", 410), ("root_deleted", 404),
])
async def test_download_rechecks_during_blob_read(worker, resource, change, status):
    path = SESSION + ("/payloads/pld_media" if resource == "payload" else "/exports/exp_ready/download")
    assert (await worker.client.get(SESSION)).status_code == 200  # the viewer's facts are now cached

    async def interrupted(key):
        await apply(worker, change)

    worker.blob.get_hook = interrupted
    response = await worker.client.get(path)
    assert response.status_code == status, response.text
    assert PRIVATE not in response.content
    assert response.headers["cache-control"] == "no-store"


async def test_admin_json_and_ticket_are_not_cacheable(worker):
    paths = [PREFIX + "/sessions", SESSION, SESSION + "/events", SESSION + "/records",
             SESSION + "/records/artifact:race_asset", SESSION + "/checkpoint", SESSION + "/search?q=race",
             SESSION + "/events?until_seq=invalid", SESSION + "/records?limit=invalid",
             SESSION + "/payloads/pld_media?meta=1"]
    for path in paths:
        response = await worker.client.get(path)
        assert response.status_code in {200, 400, 422}, response.text
        assert response.headers["cache-control"] == "no-store"
    response = await worker.client.post(PREFIX + "/ticket")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    forged = token("a", "admin")
    response = await worker.client.get(SESSION, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("artifact_id", ENCODED_ARTIFACTS)
async def test_record_detail_accepts_encoded_path_identity(worker, artifact_id):
    header = (await worker.client.get(SESSION)).json()
    record_id = "artifact:" + artifact_id
    response = await worker.client.get(SESSION + "/records/" + quote(record_id, safe=""),
                                       params={"through_seq": header["through_seq"]})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["record"]["record_id"] == record_id
    assert response.json()["record"]["data"]["text"] == "stored fixture"
    assert response.json()["through_seq"] == header["through_seq"]
