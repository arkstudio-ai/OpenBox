"""Deleting an asset also revokes inline/derived media retained in model inputs."""
import base64
import hashlib
import io
import json
import zipfile

import pytest
from sqlalchemy import select

from agent.trajectory import RequestCapture
from db.models.file_asset import FileAsset
from db.models.trajectory import SessionTrajectory, TrajectoryEvent, TrajectoryPayload
from tests.integration.test_trajectory_boundaries import app_client
from tests.integration.test_trajectory_storage import tracedb
from tool.tool import ToolContext
from trajectory import TraceContext
from trajectory.artifacts import _retained_asset, capture_asset_in_tx, revoke_asset_in_tx
from trajectory.payload import expand
from trajectory.types import now


@pytest.mark.parametrize("large_request", [False, True])
async def test_inline_and_derived_media_have_no_independent_copy_after_source_deletion(
        app_client, tracedb, monkeypatch, large_request):
    _, client, _, _ = app_client
    factory, _ = tracedb
    if large_request:
        monkeypatch.setenv("TRAJECTORY_INLINE_BYTES", "1000")
    source = b"\x89PNG\r\n\x1a\noriginal-retained-image"
    frame = b"\x89PNG\r\n\x1a\nderived-thumbnail-from-the-source"
    source_b64, frame_b64 = (base64.b64encode(data).decode() for data in (source, frame))
    trace = TraceContext("a", "session_a_1", workspace_id="ws_a", run_id="media_run", turn_id="media_turn")
    ctx = ToolContext(user_id="a", session_id="session_a_1", workspace_id="ws_a", trace_context=trace)
    async with factory.begin() as db:
        asset = FileAsset(id="media_source", user_id="a", workspace_id="ws_a", session_id="session_a_1",
                          name="source.png", mime="image/png", size=len(source), oss_key="assets/a/source.png",
                          status="ready", is_deleted=False, created_at=now())
        db.add(asset)
        original_reference = await capture_asset_in_tx(db, trace, asset, content=source)
    # The original image resolves by SHA; transformed media carries explicit
    # source identity from its producer, because its digest differs.
    ctx._trajectory_media_sources = {hashlib.sha256(frame).hexdigest(): "media_source"}
    body = {"model": "fixture", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "inspect " + ("large context " * 500 if large_request else "image")},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{source_b64}"}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": frame_b64}},
    ]}]}
    before = json.dumps(body)
    capture = await RequestCapture.start(ctx, purpose="chat", model_id="fixture", payload=body,
                                         capture_level="adapter_input")
    await capture.finish("completed")
    assert json.dumps(body) == before  # Recording never rewrites the real SDK body.
    async with factory() as db:
        trajectory = await db.scalar(select(SessionTrajectory))
        head = trajectory.committed_seq
        original = await _retained_asset(db, trace, "media_source")
        assert original.payload_id == original_reference["payload_id"]
        assert original.sha256 == hashlib.sha256(source).hexdigest()  # Never substitutes a derived frame.
        row = await db.scalar(select(TrajectoryEvent).where(TrajectoryEvent.type == "request.prepared"))
        assert ("$payload" in row.data) is large_request
        retained = await expand(db, trajectory.id, row.data, through_seq=head)
        encoded = json.dumps(retained)
        assert source_b64 not in encoded and frame_b64 not in encoded
        media = retained["input"]["messages"][0]["content"]
        assert media[1]["image_url"]["url"]["$media"]["payload_id"] == original_reference["payload_id"]
        frame_payload = media[2]["source"]["$media"]["payload_id"]
        assert (await db.get(TrajectoryPayload, frame_payload)).source_asset_id == "media_source"
    prefix = "/api/admin/trajectories/sessions/session_a_1"
    old_export = (await client.post(prefix + "/export", json={"through_seq": str(head)})).json()["export_id"]
    async with factory.begin() as db:
        asset = await db.get(FileAsset, "media_source")
        asset.is_deleted, asset.deleted_at = True, now()
        await revoke_asset_in_tx(db, asset.id)
    for path in ("", "/events", "/records", f"/records/request:{capture.context.request_id}"):
        response = await client.get(prefix + path, params={"through_seq": str(head), "until_seq": str(head)})
        assert response.status_code == 200, response.text
        assert source_b64 not in response.text and frame_b64 not in response.text
    for payload_id in (original_reference["payload_id"], frame_payload):
        assert (await client.get(prefix + f"/payloads/{payload_id}",
                                 params={"through_seq": str(head)})).status_code == 410
    assert (await client.get(prefix + f"/exports/{old_export}/download")).status_code == 410
    new_export = (await client.post(prefix + "/export", json={"through_seq": str(head)})).json()["export_id"]
    downloaded = await client.get(prefix + f"/exports/{new_export}/download")
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert json.loads(archive.read("manifest.json"))["complete"] is False
        for name in archive.namelist():
            data = archive.read(name)
            assert source not in data and frame not in data
            assert source_b64.encode() not in data and frame_b64.encode() not in data

    # A delayed caller holding the old data URL cannot save an unbound copy.
    again = await RequestCapture.start(ctx, purpose="chat", model_id="fixture", payload=body,
                                       capture_level="adapter_input")
    async with factory() as db:
        row = await db.get(TrajectoryEvent, f"request:{again.context.request_id}:prepared")
        trajectory = await db.scalar(select(SessionTrajectory))
        retained = await expand(db, trajectory.id, row.data, through_seq=trajectory.committed_seq)
        assert "source_attachment_deleted" in json.dumps(retained)
        available_media = (await db.scalars(select(TrajectoryPayload).where(
            TrajectoryPayload.media_type == "image/png", TrajectoryPayload.availability == "available"))).all()
        assert available_media == []
