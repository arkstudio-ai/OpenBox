"""Exports built by the worker on SQLite (SPEC §6.3, §8.12; manifest format of maps/projection.md §7)."""
import asyncio
import hashlib
import io
import json
import os
import zipfile
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from tests.unit.test_worker_archive_support import (AT, FakeMetrics, add_events, add_trajectory, blob_store, event_values,
    gc_entries, trace_db, worker_settings)
from trajectory import export
from trajectory.export import (ExportService, build_export, create_export, export_status, read_export, resume_exports,
    stop_exports)
from trajectory.lifecycle import expire_trajectory_content, tombstone_trajectory, utc
from trajectory.projector import replay, statistics
from trajectory.storage import MemoryBlobStore, blob_key, decode_blob, encode_blob, export_key, get_blob_store
from trajectory.store.database import close_trace_engine, trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryExport, TrajectoryMetaAsset, TrajectoryPayload
from trajectory.types import CorruptContent, canonical, iso, now
from trajectory.worker.archive import ArchiveService
from trajectory.worker.retention import RetentionService

__all__ = ["blob_store", "trace_db"]

ASSET_BYTES = b"\x89PNG\r\n\x1a\nexported-asset-bytes"
SYSTEM = {"role": "system", "content": "You are OpenBox. " * 100}
FINISHED = {"status": "completed", "usage": {"input_tokens": 3, "output_tokens": 5}, "output": "O" * 70000}


@pytest.fixture
def payload_reader(monkeypatch):
    """Payload reads as SPEC §8.9 defines them, standing in for trajectory.payload.read_payload (w2-projection)."""
    assets = {"assets/user_a/photo.png": ASSET_BYTES}

    async def read_payload(db, trajectory_id, payload_id, *, through_seq):
        row = await db.scalar(select(TrajectoryPayload).where(
            TrajectoryPayload.trajectory_id == trajectory_id, TrajectoryPayload.payload_id == payload_id,
            TrajectoryPayload.first_seq <= through_seq))
        if row is None:
            raise LookupError("Trajectory payload is not available at this position")
        if row.availability != "available":
            raise FileNotFoundError("Trajectory content has been deleted")
        if row.storage_kind == "asset":
            asset = await db.get(TrajectoryMetaAsset, row.source_asset_id)
            if asset is None or asset.is_deleted:
                raise FileNotFoundError("Source attachment has been deleted")
            return row, assets[row.storage_key]
        content = decode_blob(await get_blob_store().get(row.storage_key), row.encoding)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent("Trajectory content digest mismatch")
        return row, content

    monkeypatch.setattr(export, "read_payload", read_payload)
    return assets


@pytest.fixture
def admins(monkeypatch):
    """Viewer ids that pass the build's admin checks; discarding one revokes that viewer."""
    allowed = {"admin"}

    async def assert_admin(user_id):
        if user_id not in allowed:
            raise HTTPException(status_code=403, detail="Platform administrator access required")
        return {"user_id": user_id, "role": "admin"}

    monkeypatch.setattr(export, "assert_admin", assert_admin)
    return allowed


async def store_payload(db, blob_store, trajectory_id: str, payload_id: str, content: bytes, *, media_type: str,
                        first_seq: int, availability: str = "available") -> dict:
    sha = hashlib.sha256(content).hexdigest()
    stored, encoding = encode_blob(content, media_type)
    key = blob_key(trajectory_id, sha)
    await blob_store.put(key, stored, content_type=media_type)
    db.add(TrajectoryPayload(payload_id=payload_id, trajectory_id=trajectory_id,
                             dedupe_key=hashlib.sha256(f"{sha}:{media_type}::blob".encode()).hexdigest(), sha256=sha,
                             size_bytes=len(content), stored_bytes=len(stored), media_type=media_type,
                             encoding=encoding, storage_kind="blob", storage_key=key, availability=availability,
                             first_seq=first_seq, created_at=now()))
    return {"payload_id": payload_id, "sha256": sha, "size_bytes": len(content), "media_type": media_type,
            "availability": availability}


async def seed_export_trajectory(blob_store, trajectory_id: str = "trj_a", *, deleted_payload: bool = True,
                                 gap: bool = True) -> list[dict]:
    """One request and one tool call in eight events; events 1..5 are archived into a segment, 6..8 stay hot."""
    await add_trajectory(trajectory_id, committed=8, recording_status="gap" if gap else "recording")
    async with trace_session() as db:
        system = await store_payload(db, blob_store, trajectory_id, "pld_system", canonical(SYSTEM),
                                     media_type="application/json", first_seq=3)
        finished = await store_payload(db, blob_store, trajectory_id, "pld_finished", canonical(FINISHED),
                                       media_type="application/json", first_seq=4)
        if deleted_payload:
            await store_payload(db, blob_store, trajectory_id, "pld_gone", b"removed content", media_type="text/plain",
                                first_seq=2, availability="deleted")
        await store_payload(db, blob_store, trajectory_id, "pld_later", b"not visible yet", media_type="text/plain",
                            first_seq=9)
        db.add(TrajectoryPayload(payload_id="pld_photo", trajectory_id=trajectory_id,
                                 dedupe_key=hashlib.sha256(b"photo").hexdigest(), size_bytes=len(ASSET_BYTES),
                                 media_type="image/png", storage_kind="asset", storage_key="assets/user_a/photo.png",
                                 source_asset_id="asset_photo", first_seq=6, created_at=now()))
        if await db.get(TrajectoryMetaAsset, "asset_photo") is None:
            db.add(TrajectoryMetaAsset(id="asset_photo", user_id="user_a", oss_key="assets/user_a/photo.png",
                                       mime="image/png", size=len(ASSET_BYTES), status="ready", updated_at=now(),
                                       synced_at=now()))
    photo = {"payload_id": "pld_photo", "sha256": None, "size_bytes": len(ASSET_BYTES), "media_type": "image/png",
             "availability": "available"}
    system_ref = {key: system[key] for key in ("sha256", "size_bytes", "media_type", "payload_id")}
    rows = [
        event_values(trajectory_id, 1, type="trajectory.started", event_id=f"evt_start_{trajectory_id}",
                     data={"existing_session": False, "schema_version": 2}),
        event_values(trajectory_id, 2, type="request.started", request_id="req_1", data={"model": "gpt-test"}),
        event_values(trajectory_id, 3, type="request.prepared", request_id="req_1",
                     data={"input": {"messages": [{"$ref": {**system_ref, "kind": "message"}}]}},
                     hints={"preview": {"input": "You are OpenBox."}}),
        event_values(trajectory_id, 4, type="request.finished", request_id="req_1", data={"$payload": finished}),
        event_values(trajectory_id, 5, type="recording.gap" if gap else "input.accepted",
                     data={"phase": "dropped", "reason": "queue_overflow", "dropped_events": 2} if gap else {"text": "hi"}),
        event_values(trajectory_id, 6, type="artifact.recorded", data={"artifact_id": "asset_photo", "payload": photo}),
        event_values(trajectory_id, 7, type="tool.requested", call_id="call_1",
                     data={"tool": "bash", "arguments": {"cmd": "ls"}}),
        event_values(trajectory_id, 8, type="tool.finished", call_id="call_1", data={"status": "completed", "output": "done"}),
    ]
    await add_events(rows)
    archive = ArchiveService(worker_settings(segment_events=5), blob_store=blob_store, metrics=FakeMetrics())
    assert await archive.archive_trajectory(trajectory_id) == 5
    return rows


def expected_event(row: dict) -> dict:
    """The events API shape of a stored row, with the data as stored."""
    return {"event_id": row["event_id"], "trajectory_id": row["trajectory_id"], "user_id": row["user_id"],
            "session_id": row["session_id"], **row["context"], "source_session_id": row["source_session_id"],
            "seq": str(row["seq"]), "type": row["type"], "version": row["version"],
            "occurred_at": iso(row["occurred_at"]), "recorded_at": iso(row["recorded_at"]), "data": row["data"]}


async def new_export(trajectory_id: str, through_seq: int, viewer_id: str = "admin") -> str:
    async with trace_session() as db:
        return (await create_export(db, await db.get(SessionTrajectory, trajectory_id), viewer_id, through_seq)).id


async def export_row(export_id: str) -> TrajectoryExport:
    async with trace_session() as db:
        return await db.get(TrajectoryExport, export_id)


def worker(blob_store, metrics=None, owner_id="worker-a", **settings) -> ExportService:
    return ExportService(worker_settings(**settings), blob_store=blob_store, metrics=metrics or FakeMetrics(),
                         owner_id=owner_id)


def unzip(content: bytes) -> tuple[dict, dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    return json.loads(entries["manifest.json"]), entries


async def test_an_export_holds_segments_hot_rows_payload_blobs_and_asset_bytes(trace_db, blob_store, payload_reader,
                                                                              admins):
    rows = await seed_export_trajectory(blob_store)
    async with trace_session() as db:
        row = await create_export(db, await db.get(SessionTrajectory, "trj_a"), "admin", 8)
        assert export_status(row, "session_trj_a") == {"export_id": row.id, "status": "pending", "through_seq": "8",
                                                       "error": None, "download_url": None}
    metrics = FakeMetrics()
    assert await worker(blob_store, metrics).run_once() == 1

    async with trace_session() as db:
        saved, content = await read_export(db, await db.get(SessionTrajectory, "trj_a"), row.id)
    sha = hashlib.sha256(content).hexdigest()
    assert export_status(saved, "session_trj_a") == {
        "export_id": row.id, "status": "completed", "through_seq": "8", "error": None,
        "download_url": f"/api/admin/trajectories/sessions/session_trj_a/exports/{row.id}/download"}
    assert (saved.storage_key, saved.sha256, saved.size_bytes) == (export_key(row.id, sha), sha, len(content))
    assert (saved.lease_owner, saved.lease_until) == (None, None)
    assert blob_store.content_types[saved.storage_key] == "application/zip"
    assert metrics.counters == {"exports_built": 1}

    manifest, entries = unzip(content)
    assert list(entries) == ["events.jsonl", "statistics.json", "payloads/pld_system", "payloads/pld_finished",
                             "payloads/pld_photo", "manifest.json"]
    for item in manifest["files"]:
        data = entries[item["path"]]
        assert (hashlib.sha256(data).hexdigest(), len(data)) == (item["sha256"], item["size_bytes"])
    events = [json.loads(line) for line in entries["events.jsonl"].splitlines()]
    assert events == [expected_event(row) for row in rows]
    expanded = [{**event, "data": FINISHED} if event["seq"] == "4" else event for event in events]
    stats = json.loads(entries["statistics.json"])
    assert stats == json.loads(canonical(statistics(replay(expanded))))
    assert (stats["request_count"], stats["tool_count"], stats["input_tokens"], stats["output_tokens"]) == (1, 1, 3, 5)
    assert (entries["payloads/pld_system"], entries["payloads/pld_finished"], entries["payloads/pld_photo"]) == (
        canonical(SYSTEM), canonical(FINISHED), ASSET_BYTES)
    assert [item for item in manifest["files"] if item["path"] == "payloads/pld_photo"] == [
        {"path": "payloads/pld_photo", "payload_id": "pld_photo", "media_type": "image/png",
         "sha256": hashlib.sha256(ASSET_BYTES).hexdigest(), "size_bytes": len(ASSET_BYTES)}]
    assert set(manifest) == {"format", "version", "projector_version", "trajectory_id", "through_seq", "created_at",
                             "files", "missing_payloads", "user_id", "session_id", "coverage_start",
                             "recording_status", "gaps", "complete", "unsupported_events"}
    assert {key: manifest[key] for key in manifest if key not in {"created_at", "files", "gaps"}} == {
        "format": "openbox.session-trajectory", "version": 1, "projector_version": 1, "trajectory_id": "trj_a",
        "through_seq": "8", "user_id": "user_a", "session_id": "session_trj_a", "coverage_start": iso(AT),
        "recording_status": "gap", "complete": False, "unsupported_events": [],
        "missing_payloads": [{"payload_id": "pld_gone", "availability": "deleted", "reason": "FileNotFoundError"}]}
    assert manifest["gaps"] == [{"record_id": f"gap:{rows[4]['event_id']}", "seq": "5", "data": rows[4]["data"]}]


async def test_an_export_at_an_earlier_watermark_is_complete(trace_db, blob_store, payload_reader, admins):
    rows = await seed_export_trajectory(blob_store, deleted_payload=False, gap=False)
    export_id = await new_export("trj_a", 4)
    assert await worker(blob_store).run_once() == 1
    async with trace_session() as db:
        _, content = await read_export(db, await db.get(SessionTrajectory, "trj_a"), export_id)
    manifest, entries = unzip(content)
    assert [json.loads(line) for line in entries["events.jsonl"].splitlines()] == [expected_event(row) for row in rows[:4]]
    assert sorted(name for name in entries if name.startswith("payloads/")) == ["payloads/pld_finished", "payloads/pld_system"]
    assert (manifest["through_seq"], manifest["complete"], manifest["gaps"], manifest["missing_payloads"]) == ("4", True, [], [])


async def test_unsupported_events_make_an_export_incomplete(trace_db, blob_store, payload_reader, admins):
    await add_trajectory("trj_a", committed=2)
    await add_events([event_values("trj_a", 1), event_values("trj_a", 2, version=2)])
    export_id = await new_export("trj_a", 2)
    assert await worker(blob_store).run_once() == 1
    manifest, _ = unzip(blob_store.objects[(await export_row(export_id)).storage_key])
    assert manifest["unsupported_events"] == [{"seq": "2", "type": "input.accepted", "version": 2}]
    assert manifest["complete"] is False


async def test_events_archived_while_the_export_reads_come_from_their_new_segment(trace_db, blob_store, payload_reader,
                                                                                admins, monkeypatch):
    await add_trajectory("trj_a", committed=12)
    rows = [event_values("trj_a", seq) for seq in range(1, 13)]
    await add_events(rows)
    export_id = await new_export("trj_a", 12)
    monkeypatch.setattr(export, "EVENT_PAGE", 4)
    expanded = ExportService._expanded

    async def archive_meanwhile(self, trajectory_id, event, through):
        if event["seq"] == "4":
            archive = ArchiveService(worker_settings(segment_events=8), blob_store=blob_store, metrics=FakeMetrics())
            assert await archive.archive_trajectory("trj_a") == 8
        return await expanded(self, trajectory_id, event, through)

    monkeypatch.setattr(ExportService, "_expanded", archive_meanwhile)
    assert await worker(blob_store).run_once() == 1
    saved = await export_row(export_id)
    assert saved.status == "completed", saved.error
    _, entries = unzip(blob_store.objects[saved.storage_key])
    assert [json.loads(line) for line in entries["events.jsonl"].splitlines()] == [expected_event(row) for row in rows]


async def test_two_workers_never_build_the_same_export(trace_db, blob_store, payload_reader, admins):
    await add_trajectory("trj_a", committed=2)
    await add_events([event_values("trj_a", 1), event_values("trj_a", 2)])
    export_id = await new_export("trj_a", 2)
    entered, release, uploads = asyncio.Event(), asyncio.Event(), []

    async def slow_upload(key):
        if "/_exports/" in f"/{key}":
            uploads.append(key)
            entered.set()
            await release.wait()

    blob_store.faults["put"] = slow_upload
    metrics = FakeMetrics()
    first, second = worker(blob_store, metrics, "worker-a"), worker(blob_store, metrics, "worker-b")
    building = asyncio.create_task(first.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    assert await second.run_once() == 0
    row = await export_row(export_id)
    assert (row.status, row.lease_owner) == ("running", "worker-a")
    release.set()
    assert await asyncio.wait_for(building, 5) == 1
    assert (len(uploads), metrics.counters, (await export_row(export_id)).status) == (1, {"exports_built": 1}, "completed")
    assert await second.run_once() == 0


async def test_unfinished_exports_resume_after_lease_expiry_and_on_the_owners_restart(trace_db, blob_store,
                                                                                    payload_reader, admins):
    await add_trajectory("trj_a", committed=2)
    await add_events([event_values("trj_a", 1), event_values("trj_a", 2)])
    partial = export_key("exp_expired", hashlib.sha256(b"partial").hexdigest())
    await blob_store.put(partial, b"partial", content_type="application/zip")
    stamp = now()
    rows = {"exp_pending": ("pending", None, None, None), "exp_expired": ("running", "worker-dead", stamp - timedelta(seconds=1), partial),
            "exp_mine": ("running", "worker-a", stamp + timedelta(minutes=5), None),
            "exp_theirs": ("running", "worker-b", stamp + timedelta(minutes=5), None)}
    async with trace_session() as db:
        for export_id, (status, owner, lease_until, key) in rows.items():
            db.add(TrajectoryExport(id=export_id, trajectory_id="trj_a", viewer_id="admin", through_seq=2, status=status,
                                    lease_owner=owner, lease_until=lease_until, storage_key=key, created_at=stamp,
                                    updated_at=stamp))
    restarted = worker(blob_store, owner_id="worker-a")
    assert await restarted.run_once() == 3
    statuses = {export_id: ((row := await export_row(export_id)).status, row.lease_owner) for export_id in rows}
    assert statuses == {"exp_pending": ("completed", None), "exp_expired": ("completed", None),
                        "exp_mine": ("completed", None), "exp_theirs": ("running", "worker-b")}
    assert ("key", partial, "export_superseded") in await gc_entries()
    assert await restarted.run_once() == 0


async def test_a_build_stops_when_its_lease_is_taken_over(trace_db, blob_store, payload_reader, admins):
    await add_trajectory("trj_a", committed=1)
    await add_events([event_values("trj_a", 1)])
    export_id = await new_export("trj_a", 1)
    entered = asyncio.Event()

    async def stuck_upload(key):
        if key.startswith("trajectories/_exports/"):
            entered.set()
            await asyncio.Event().wait()

    blob_store.faults["put"] = stuck_upload
    metrics = FakeMetrics()
    service = ExportService(worker_settings(), blob_store=blob_store, metrics=metrics, owner_id="worker-a",
                            lease_seconds=0.3)
    building = asyncio.create_task(service.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    async with trace_session() as db:
        await db.execute(update(TrajectoryExport).where(TrajectoryExport.id == export_id).values(
            lease_owner="worker-b", lease_until=now() + timedelta(hours=1)))
    assert await asyncio.wait_for(building, 5) == 1
    row = await export_row(export_id)
    assert (row.status, row.lease_owner, metrics.counters) == ("running", "worker-b", {})


async def test_the_heartbeat_keeps_a_long_build_leased(trace_db, blob_store, payload_reader, admins):
    await add_trajectory("trj_a", committed=1)
    await add_events([event_values("trj_a", 1)])
    export_id = await new_export("trj_a", 1)
    entered = asyncio.Event()

    async def slow_upload(key):
        if key.startswith("trajectories/_exports/"):
            entered.set()
            await asyncio.sleep(1.0)

    blob_store.faults["put"] = slow_upload
    service = ExportService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics(), owner_id="worker-a",
                            lease_seconds=0.3)
    building = asyncio.create_task(service.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.sleep(0.5)
    assert await worker(blob_store, owner_id="worker-b").run_once() == 0
    assert await asyncio.wait_for(building, 5) == 1
    assert (await export_row(export_id)).status == "completed"


async def test_deletion_wins_over_an_export_being_built(trace_db, blob_store, payload_reader, admins):
    await add_trajectory("trj_a", committed=2)
    await add_events([event_values("trj_a", 1), event_values("trj_a", 2)])
    export_id = await new_export("trj_a", 2)

    async def delete_during_upload(key):
        if key.startswith("trajectories/_exports/"):
            async with trace_session() as db:
                await tombstone_trajectory(db, await db.get(SessionTrajectory, "trj_a"), reason="session_deleted")

    blob_store.faults["put"] = delete_during_upload
    metrics = FakeMetrics()
    assert await worker(blob_store, metrics).run_once() == 1
    row = await export_row(export_id)
    assert (row.status, row.storage_key, metrics.counters) == ("deleted", None, {})
    uploaded = [key for key in blob_store.objects if key.startswith("trajectories/_exports/")]
    assert len(uploaded) == 1
    assert {("key", uploaded[0], "session_deleted"), ("key", uploaded[0], "export_deleted")} <= set(await gc_entries())
    blob_store.clear_faults()
    await RetentionService(worker_settings(), blob_store=blob_store, metrics=FakeMetrics()).process_gc_queue()
    assert [key for key in blob_store.objects if "_exports" in key] == []


async def test_the_size_cap_fails_oversized_exports_and_skips_payloads_that_do_not_fit(trace_db, blob_store,
                                                                                     payload_reader, admins):
    await add_trajectory("trj_big", committed=20)
    await add_events([event_values("trj_big", seq, data={"text": os.urandom(4000).hex()}) for seq in range(1, 21)])
    oversized = await new_export("trj_big", 20)
    assert await worker(blob_store, export_max_bytes=20_000).run_once() == 1
    row = await export_row(oversized)
    assert (row.status, row.error, row.storage_key) == ("failed", "ExportTooLarge", None)
    assert [key for key in blob_store.objects if "_exports" in key] == []

    await add_trajectory("trj_small", committed=1)
    await add_events([event_values("trj_small", 1)])
    async with trace_session() as db:
        await store_payload(db, blob_store, "trj_small", "pld_large", os.urandom(100_000),
                            media_type="application/octet-stream", first_seq=1)
    fits = await new_export("trj_small", 1)
    assert await worker(blob_store, export_max_bytes=60_000).run_once() == 1
    row = await export_row(fits)
    manifest, entries = unzip(blob_store.objects[row.storage_key])
    assert row.status == "completed" and "payloads/pld_large" not in entries
    assert manifest["missing_payloads"] == [{"payload_id": "pld_large", "availability": "available",
                                             "reason": "ExportTooLarge"}]
    assert manifest["complete"] is False


class CorruptExportStore(MemoryBlobStore):
    async def put(self, key, data, *, content_type, if_absent=True):
        if key.startswith("trajectories/_exports/"):
            data = bytes(data[:-1]) + bytes([data[-1] ^ 1])
        await super().put(key, data, content_type=content_type, if_absent=if_absent)


async def test_an_archive_that_does_not_read_back_intact_fails_the_export(trace_db, payload_reader, admins):
    store = CorruptExportStore()
    await add_trajectory("trj_a", committed=1)
    await add_events([event_values("trj_a", 1)])
    export_id = await new_export("trj_a", 1)
    assert await worker(store).run_once() == 1
    row = await export_row(export_id)
    assert (row.status, row.error, row.storage_key, row.lease_owner) == ("failed", "CorruptContent", None, None)
    (entry,) = await gc_entries()
    assert entry[0] == "key" and entry[1].startswith("trajectories/_exports/") and entry[2] == "export_failed"


async def test_a_missing_committed_tail_or_a_revoked_admin_fails_the_export(trace_db, blob_store, payload_reader, admins,
                                                                         monkeypatch):
    await add_trajectory("trj_tail", committed=3)
    await add_events([event_values("trj_tail", 1), event_values("trj_tail", 2)])
    tail = await new_export("trj_tail", 3)
    assert await worker(blob_store).run_once() == 1
    assert ((row := await export_row(tail)).status, row.error) == ("failed", "CorruptContent")

    await add_trajectory("trj_a", committed=1)
    await add_events([event_values("trj_a", 1)])
    revoked = await new_export("trj_a", 1)
    replay_statistics = export.statistics

    def revoke_during_the_build(state):
        admins.discard("admin")
        return replay_statistics(state)

    monkeypatch.setattr(export, "statistics", revoke_during_the_build)
    assert await worker(blob_store).run_once() == 1
    assert ((row := await export_row(revoked)).status, row.error) == ("failed", "HTTPException")
    assert [key for key in blob_store.objects if "_exports" in key] == []


async def test_downloads_are_refused_when_not_ready_invalidated_expired_or_corrupt(trace_db, blob_store,
                                                                                   payload_reader, admins):
    await seed_export_trajectory(blob_store, deleted_payload=False, gap=False)
    await add_trajectory("trj_b", committed=0)
    ready = await new_export("trj_a", 8)
    assert await worker(blob_store).run_once() == 1
    pending = await new_export("trj_a", 8)

    async def attempt(export_id, trajectory_id="trj_a"):
        async with trace_session() as db:
            return await read_export(db, await db.get(SessionTrajectory, trajectory_id), export_id)

    assert (await attempt(ready))[0].status == "completed"
    with pytest.raises(LookupError):
        await attempt(ready, "trj_b")
    with pytest.raises(HTTPException) as raised:
        await attempt(pending)
    assert (raised.value.status_code, raised.value.detail) == (409, "Export is not ready")

    key = (await export_row(ready)).storage_key
    original = blob_store.objects[key]
    blob_store.objects[key] = original + b"tampered"
    with pytest.raises(CorruptContent):
        await attempt(ready)
    del blob_store.objects[key]
    with pytest.raises(FileNotFoundError):
        await attempt(ready)
    blob_store.objects[key] = original

    async with trace_session() as db:
        (await db.get(TrajectoryMetaAsset, "asset_photo")).is_deleted = True
    with pytest.raises(FileNotFoundError, match="invalidated"):
        await attempt(ready)
    async with trace_session() as db:
        (await db.get(TrajectoryMetaAsset, "asset_photo")).is_deleted = False
        (await db.get(TrajectoryPayload, "pld_system")).deleted_at = now() + timedelta(seconds=1)
    with pytest.raises(FileNotFoundError, match="invalidated"):
        await attempt(ready)
    async with trace_session() as db:
        (await db.get(TrajectoryPayload, "pld_system")).deleted_at = None
    assert (await attempt(ready))[1] == original

    async with trace_session() as db:
        await expire_trajectory_content(db, await db.get(SessionTrajectory, "trj_a"))
    with pytest.raises(FileNotFoundError, match="expired"):
        await attempt(ready)


async def test_backend_process_hooks_build_stop_and_resume_exports(trace_db, blob_store, payload_reader, admins):
    await add_trajectory("trj_a", committed=1)
    await add_events([event_values("trj_a", 1)])
    assert await build_export(await new_export("trj_a", 1)) == "completed"

    stopped = await new_export("trj_a", 1)
    entered = asyncio.Event()

    async def stuck_upload(key):
        if key.startswith("trajectories/_exports/"):
            entered.set()
            await asyncio.Event().wait()

    blob_store.faults["put"] = stuck_upload
    building = asyncio.create_task(build_export(stopped))
    await asyncio.wait_for(entered.wait(), 5)
    await stop_exports()
    assert building.cancelled() and not export._tasks
    row = await export_row(stopped)
    assert row.status == "running" and utc(row.lease_until) <= now()

    blob_store.clear_faults()
    await resume_exports()
    for _ in range(100):
        if (await export_row(stopped)).status == "completed":
            break
        await asyncio.sleep(0.05)
    assert (await export_row(stopped)).status == "completed"


async def test_resuming_exports_without_a_trace_database_does_nothing():
    await close_trace_engine()
    await resume_exports()
    assert not export._tasks
