"""RetentionService and trajectory.lifecycle on SQLite (SPEC §8.5, §8.11): tombstones, expiry, exports, GC queue."""
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tests.unit.test_worker_archive_support import (FakeMetrics, add_events, add_trajectory, blob_store, event_values,
    gc_entries, notifications, trace_db, trajectory_row, worker_settings, worker_state)
from trajectory.lifecycle import (GC_KEY, GC_PREFIX, PREFIX_SWEEP_DELAY, artifact_removed_event_id, enqueue_gc,
    gc_retry_delay, revoke_asset, tombstone_trajectory, utc)
from trajectory.storage import blob_key, export_key, segment_key, trajectory_prefix
from trajectory.store.database import trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryEvent, TrajectoryEventKey,
    TrajectoryExport, TrajectoryGcQueue, TrajectoryMetaSession, TrajectoryPayload, TrajectoryRecord,
    TrajectoryRecordEvent, TrajectorySegment, TrajectorySessionSummary, TrajectoryWorkerState)
from trajectory.types import now
from trajectory.worker import retention as retention_module
from trajectory.worker.retention import RetentionService

__all__ = ["blob_store", "notifications", "trace_db"]

CONTENT_MODELS = (TrajectoryEvent, TrajectoryRecord, TrajectoryRecordEvent, TrajectoryCheckpoint,
                  TrajectorySessionSummary, TrajectorySegment, TrajectoryEventKey)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def service(blob_store, metrics=None) -> RetentionService:
    return RetentionService(worker_settings(), blob_store=blob_store, metrics=metrics or FakeMetrics())


async def seed_content(trajectory_id: str, blob_store, **trajectory) -> dict:
    """A trajectory with every kind of content row, plus the objects those rows reference."""
    await add_trajectory(trajectory_id, committed=3, archived=2, checkpoint_seq=2, stored_bytes=900, **trajectory)
    await add_events([event_values(trajectory_id, 3)])
    stamp, session_id = now(), f"session_{trajectory_id}"
    keys = {"blob": blob_key(trajectory_id, sha(f"{trajectory_id} blob")), "segment": segment_key(trajectory_id, 1, 2),
            "done": export_key(f"exp_{trajectory_id}_done", sha("done")),
            "running": export_key(f"exp_{trajectory_id}_running", sha("running"))}
    async with trace_session() as db:
        db.add(TrajectorySegment(trajectory_id=trajectory_id, from_seq=1, to_seq=2, storage_key=keys["segment"],
                                 event_count=2, raw_bytes=10, stored_bytes=5, sha256=sha("segment"), created_at=stamp))
        db.add(TrajectoryRecord(trajectory_id=trajectory_id, record_id="turn:1", kind="turn", status="completed",
                                start_seq=1, applied_seq=3, projector_version=1, data={}, summary={}, search_doc="turn"))
        db.add(TrajectoryRecordEvent(trajectory_id=trajectory_id, record_id="turn:1", seq=1))
        db.add(TrajectoryCheckpoint(trajectory_id=trajectory_id, through_seq=2, projector_version=1, state={},
                                    digest=sha("state"), created_at=stamp))
        db.add(TrajectorySessionSummary(trajectory_id=trajectory_id, user_id="user_a", session_id=session_id,
                                        workspace_id="ws_a", last_activity_at=stamp, running_status="idle",
                                        recording_status="recording", applied_seq=3, statistics={"request_count": 2}))
        for suffix, kind, key, source, availability in (
                ("blob", "blob", keys["blob"], None, "available"),
                ("asset", "asset", "assets/user_a/photo.png", "asset_photo", "available"),
                ("gone", "blob", blob_key(trajectory_id, sha("gone")), None, "deleted")):
            db.add(TrajectoryPayload(
                payload_id=f"pld_{trajectory_id}_{suffix}", trajectory_id=trajectory_id,
                dedupe_key=sha(f"{trajectory_id} {suffix}"), sha256=None if kind == "asset" else sha(suffix),
                size_bytes=4, media_type="image/png" if kind == "asset" else "application/json", storage_kind=kind,
                storage_key=key, source_asset_id=source, availability=availability, first_seq=1, created_at=stamp,
                deleted_at=stamp if availability == "deleted" else None))
        db.add(TrajectoryExport(id=f"exp_{trajectory_id}_done", trajectory_id=trajectory_id, viewer_id="admin",
                                through_seq=3, status="completed", storage_key=keys["done"], sha256=sha("done"),
                                size_bytes=4, created_at=stamp, updated_at=stamp))
        db.add(TrajectoryExport(id=f"exp_{trajectory_id}_running", trajectory_id=trajectory_id, viewer_id="admin",
                                through_seq=3, status="running", storage_key=keys["running"], lease_owner="worker_1",
                                lease_until=stamp + timedelta(minutes=1), created_at=stamp, updated_at=stamp))
        db.add(TrajectoryEventKey(event_id=f"evt_{trajectory_id}_3", trajectory_id=trajectory_id, seq=3,
                                  content_hash=sha("3"), recorded_at=stamp))
        db.add(TrajectoryWorkerState(key=f"archive:{trajectory_id}", updated_at=stamp,
                                     value={"storage_key": segment_key(trajectory_id, 3, 3), "from_seq": 3, "to_seq": 3}))
    for key in keys.values():
        await blob_store.put(key, b"data", content_type="application/octet-stream")
    return keys


async def row_counts(trajectory_id: str) -> dict[str, int]:
    async with trace_session() as db:
        return {model.__tablename__: await db.scalar(select(func.count()).select_from(model).where(
            model.trajectory_id == trajectory_id)) for model in CONTENT_MODELS}


async def availability(trajectory_id: str) -> dict[str, str]:
    async with trace_session() as db:
        return dict((await db.execute(select(TrajectoryPayload.payload_id, TrajectoryPayload.availability).where(
            TrajectoryPayload.trajectory_id == trajectory_id))).all())


async def test_a_tombstone_purges_content_keeps_tombstones_and_notifies_after_commit(trace_db, blob_store,
                                                                                    notifications):
    keys = await seed_content("trj_a", blob_store)
    await seed_content("trj_b", blob_store)
    retention = service(blob_store)

    async with trace_session() as db:
        await retention.tombstone(db, await db.get(SessionTrajectory, "trj_a"), reason="session_deleted")
        assert notifications == []
    assert notifications == [{"trajectory_id": "trj_a", "user_id": "user_a", "session_id": "session_trj_a",
                              "committed_seq": 3, "deleted": True}]
    assert set((await row_counts("trj_a")).values()) == {0}
    assert 0 not in (await row_counts("trj_b")).values()
    trajectory = await trajectory_row("trj_a")
    assert (trajectory.recording_status, trajectory.stored_bytes) == ("deleted", 0)
    assert trajectory.deleted_at is not None and trajectory.content_expired_at is None
    assert set((await availability("trj_a")).values()) == {"deleted"}
    async with trace_session() as db:
        exports = (await db.execute(select(TrajectoryExport.status, TrajectoryExport.storage_key,
                                           TrajectoryExport.lease_owner, TrajectoryExport.lease_until)
                                    .where(TrajectoryExport.trajectory_id == "trj_a"))).all()
        payloads_deleted_at = (await db.scalars(select(TrajectoryPayload.deleted_at).where(
            TrajectoryPayload.trajectory_id == "trj_a"))).all()
        prefix_due = sorted(utc(value) for value in (await db.scalars(select(TrajectoryGcQueue.next_attempt_at).where(
            TrajectoryGcQueue.kind == GC_PREFIX))).all())
    assert {tuple(row) for row in exports} == {("deleted", None, None, None)}
    assert all(payloads_deleted_at)
    assert await worker_state("archive:trj_a") is None
    prefix = trajectory_prefix("trj_a")
    assert sorted(await gc_entries()) == sorted([
        ("key", keys["done"], "session_deleted"), ("key", keys["running"], "session_deleted"),
        ("prefix", prefix, "session_deleted"), ("prefix", prefix, "session_deleted")])
    assert prefix_due[1] - prefix_due[0] == PREFIX_SWEEP_DELAY

    assert await retention.process_gc_queue() == 3
    assert [key for key in blob_store.objects if "trj_a" in key] == []
    assert len([key for key in blob_store.objects if "trj_b" in key]) == 4
    assert [entry[0] for entry in await gc_entries()] == ["prefix"]
    async with trace_session() as db:
        assert await tombstone_trajectory(db, await db.get(SessionTrajectory, "trj_a"), reason="again") is False
    assert len(notifications) == 1


async def test_a_rolled_back_tombstone_changes_and_publishes_nothing(trace_db, blob_store, notifications):
    await seed_content("trj_a", blob_store)
    with pytest.raises(RuntimeError):
        async with trace_session() as db:
            await tombstone_trajectory(db, await db.get(SessionTrajectory, "trj_a"), reason="session_deleted")
            raise RuntimeError("the ingest batch failed")
    assert notifications == [] and (await trajectory_row("trj_a")).deleted_at is None
    assert 0 not in (await row_counts("trj_a")).values()
    assert await gc_entries() == []


async def test_content_expiry_keeps_the_summary_statistics_and_keys(trace_db, blob_store, notifications):
    old = now() - timedelta(days=200)
    await seed_content("trj_old", blob_store, last_activity_at=old)
    await seed_content("trj_recent", blob_store, last_activity_at=now() - timedelta(days=10))
    await seed_content("trj_deleted", blob_store, last_activity_at=old, deleted_at=old, recording_status="deleted")
    retention = service(blob_store)

    result = await retention.run_once()
    assert result == {"tombstoned": 0, "expired": 1, "exports_expired": 0, "gc_processed": 3}
    trajectory = await trajectory_row("trj_old")
    assert (trajectory.recording_status, trajectory.deleted_at, trajectory.stored_bytes) == ("expired", None, 0)
    assert trajectory.content_expired_at is not None
    assert await row_counts("trj_old") == {
        "trajectory_events": 0, "trajectory_records": 0, "trajectory_record_events": 0, "trajectory_checkpoints": 0,
        "trajectory_session_summaries": 1, "trajectory_segments": 0, "trajectory_event_keys": 1}
    async with trace_session() as db:
        summary = await db.get(TrajectorySessionSummary, "trj_old")
    assert (summary.recording_status, summary.statistics) == ("expired", {"request_count": 2})
    assert await availability("trj_old") == {"pld_trj_old_blob": "expired", "pld_trj_old_asset": "expired",
                                             "pld_trj_old_gone": "deleted"}
    assert [key for key in blob_store.objects if "trj_old" in key] == []
    assert (await trajectory_row("trj_recent")).content_expired_at is None
    assert (await row_counts("trj_recent"))["trajectory_events"] == 1
    assert (await trajectory_row("trj_deleted")).content_expired_at is None
    assert notifications == []
    assert (await retention.run_once())["expired"] == 0


async def test_expiry_rechecks_the_last_activity_under_the_row_lock(trace_db, blob_store):
    await seed_content("trj_a", blob_store)
    retention = service(blob_store)
    await retention.expire_content("trj_a", inactive_before=now() - timedelta(days=180))
    assert (await trajectory_row("trj_a")).content_expired_at is None
    await retention.expire_content("trj_a")
    assert (await trajectory_row("trj_a")).recording_status == "expired"


async def test_old_exports_are_deleted_and_their_objects_queued(trace_db, blob_store):
    await add_trajectory("trj_a", committed=3)
    old, recent = now() - timedelta(days=31), now() - timedelta(days=29)
    exports = {"exp_old_done": ("completed", export_key("exp_old_done", sha("a")), old),
               "exp_old_failed": ("failed", None, old), "exp_old_deleted": ("deleted", None, old),
               "exp_recent": ("completed", export_key("exp_recent", sha("b")), recent)}
    async with trace_session() as db:
        for export_id, (status, key, created_at) in exports.items():
            db.add(TrajectoryExport(id=export_id, trajectory_id="trj_a", viewer_id="admin", through_seq=3,
                                    status=status, storage_key=key, created_at=created_at, updated_at=created_at))
    assert await service(blob_store).expire_exports() == 2
    async with trace_session() as db:
        rows = dict((row.id, (row.status, row.storage_key)) for row in (await db.scalars(select(TrajectoryExport))).all())
    assert rows == {"exp_old_done": ("deleted", None), "exp_old_failed": ("deleted", None),
                    "exp_old_deleted": ("deleted", None),
                    "exp_recent": ("completed", export_key("exp_recent", sha("b")))}
    assert await gc_entries() == [("key", export_key("exp_old_done", sha("a")), "export_expired")]


async def test_gc_deletes_keys_and_whole_trajectory_prefixes(trace_db, blob_store):
    kept = blob_key("trj_ab", sha("2"))
    for key in (blob_key("trj_a", sha("1")), segment_key("trj_a", 1, 2), export_key("exp_1", sha("z")), kept):
        await blob_store.put(key, b"x", content_type="application/octet-stream")
    async with trace_session() as db:
        await enqueue_gc(db, GC_PREFIX, trajectory_prefix("trj_a"), "session_deleted")
        await enqueue_gc(db, GC_KEY, export_key("exp_1", sha("z")), "export_expired")
    metrics = FakeMetrics()
    assert await service(blob_store, metrics).process_gc_queue() == 2
    # A prefix names one trajectory directory: trj_a/ never matches trj_ab/.
    assert list(blob_store.objects) == [kept]
    assert (metrics.counters, metrics.gauges) == ({"gc_deleted": 3}, {"gc_queue_depth": 0})


async def test_gc_entries_must_name_an_object_or_one_trajectory_directory(trace_db):
    async with trace_session() as db:
        for kind, key in ((GC_PREFIX, "trajectories/"), (GC_PREFIX, "trajectories/trj_a"),
                          (GC_PREFIX, "trajectories/trj_a/blobs/"), (GC_PREFIX, "other/trj_a/"),
                          (GC_KEY, "../outside"), ("bucket", trajectory_prefix("trj_a"))):
            with pytest.raises(ValueError):
                await enqueue_gc(db, kind, key, "test")


def test_gc_retry_delay_doubles_from_30_seconds_up_to_6_hours():
    assert [gc_retry_delay(attempts).total_seconds() for attempts in range(1, 13)] == [
        30, 60, 120, 240, 480, 960, 1920, 3840, 7680, 15360, 21600, 21600]
    assert gc_retry_delay(10_000) == timedelta(hours=6)


async def test_gc_failures_back_off_without_starving_due_entries(trace_db, blob_store, monkeypatch):
    start = now()
    clock = {"now": start}
    monkeypatch.setattr(retention_module, "now", lambda: clock["now"])
    failing = [blob_key("trj_bad", sha(str(index))) for index in range(100)]
    healthy = [blob_key("trj_good", sha(str(index))) for index in range(50)]
    for key in failing + healthy:
        await blob_store.put(key, b"x", content_type="application/octet-stream")
    async with trace_session() as db:
        for key in failing:
            await enqueue_gc(db, GC_KEY, key, "test", at=start - timedelta(seconds=1))
        for key in healthy:
            await enqueue_gc(db, GC_KEY, key, "test", at=start)

    def refuse(key):
        if "trj_bad" in key:
            raise ConnectionError("store unavailable")

    blob_store.faults["delete"] = refuse
    metrics = FakeMetrics()
    retention = service(blob_store, metrics)

    assert await retention.process_gc_queue() == 0  # the 100 oldest entries fail...
    assert await retention.process_gc_queue() == 50  # ...and no longer block the entries behind them
    assert [key for key in healthy if key in blob_store.objects] == []
    async with trace_session() as db:
        rows = (await db.scalars(select(TrajectoryGcQueue).order_by(TrajectoryGcQueue.id))).all()
    assert {(row.attempts, utc(row.next_attempt_at)) for row in rows} == {(1, start + timedelta(seconds=30))}
    assert len(rows) == 100 and rows[0].last_error == "ConnectionError: store unavailable"
    assert metrics.counters == {"gc_failures": 100, "gc_deleted": 50}
    assert metrics.gauges == {"gc_queue_depth": 100}

    clock["now"] = start + timedelta(seconds=29)
    assert await retention.process_gc_queue() == 0 and metrics.counters["gc_failures"] == 100
    clock["now"] = start + timedelta(seconds=30)
    assert await retention.process_gc_queue() == 0
    async with trace_session() as db:
        rows = (await db.scalars(select(TrajectoryGcQueue))).all()
    assert {(row.attempts, utc(row.next_attempt_at)) for row in rows} == {(2, start + timedelta(seconds=90))}

    blob_store.clear_faults()
    clock["now"] = start + timedelta(seconds=90)
    assert await retention.process_gc_queue() == 100
    assert await gc_entries() == [] and metrics.gauges == {"gc_queue_depth": 0}
    assert blob_store.objects == {}


async def test_asset_deletion_revokes_bound_payloads_and_collects_unshared_blobs(trace_db):
    await add_trajectory("trj_a")
    await add_trajectory("trj_b")
    await add_trajectory("trj_deleted", deleted_at=now(), recording_status="deleted")
    await add_trajectory("trj_expired", content_expired_at=now(), recording_status="expired")
    shared, own, other = blob_key("trj_a", sha("shared")), blob_key("trj_a", sha("own")), blob_key("trj_b", sha("other"))
    payloads = [
        ("pld_bound_shared", "trj_a", "blob", shared, "asset_1", "available"),
        ("pld_unbound_shared", "trj_a", "blob", shared, None, "available"),
        ("pld_bound_own", "trj_a", "blob", own, "asset_1", "available"),
        ("pld_asset_ref", "trj_a", "asset", "assets/user_a/photo.png", "asset_1", "available"),
        ("pld_other", "trj_b", "blob", other, "asset_1", "available"),
        ("pld_unrelated", "trj_b", "blob", blob_key("trj_b", sha("x")), "asset_2", "available"),
        ("pld_tombstoned", "trj_deleted", "blob", blob_key("trj_deleted", sha("t")), "asset_1", "deleted"),
        ("pld_expired", "trj_expired", "blob", blob_key("trj_expired", sha("e")), "asset_1", "expired"),
    ]
    async with trace_session() as db:
        for payload_id, trajectory_id, kind, key, source, state in payloads:
            db.add(TrajectoryPayload(payload_id=payload_id, trajectory_id=trajectory_id, dedupe_key=sha(payload_id),
                                     sha256=None if kind == "asset" else key.rsplit("/", 1)[1], size_bytes=3,
                                     media_type="image/png", storage_kind=kind, storage_key=key, source_asset_id=source,
                                     availability=state, first_seq=1, created_at=now()))

    async with trace_session() as db:
        revocations = [(item.trajectory.id, item.event_id, item.data) for item in await revoke_asset(db, "asset_1")]
    removed = {"artifact_id": "asset_1", "availability": "deleted", "reason": "explicitly_deleted"}
    assert revocations == [("trj_a", artifact_removed_event_id("session_trj_a", "asset_1"), removed),
                           ("trj_b", artifact_removed_event_id("session_trj_b", "asset_1"), removed)]
    assert artifact_removed_event_id("session_trj_a", "asset_1") == "asset:" + sha("session_trj_a:asset_1:deleted")
    async with trace_session() as db:
        rows = {row.payload_id: row for row in (await db.scalars(select(TrajectoryPayload))).all()}
    assert {payload_id: row.availability for payload_id, row in rows.items()} == {
        "pld_bound_shared": "deleted", "pld_unbound_shared": "available", "pld_bound_own": "deleted",
        "pld_asset_ref": "deleted", "pld_other": "deleted", "pld_unrelated": "available",
        "pld_tombstoned": "deleted", "pld_expired": "deleted"}
    assert all(rows[name].deleted_at for name in ("pld_bound_shared", "pld_bound_own", "pld_asset_ref", "pld_other"))
    # The shared blob still backs an available payload; asset references own no bytes.
    assert await gc_entries() == [("key", own, "asset_deleted"), ("key", other, "asset_deleted")]

    async with trace_session() as db:
        assert len(await revoke_asset(db, "asset_1")) == 2
        assert await revoke_asset(db, "asset_unknown") == []
    assert len(await gc_entries()) == 2


async def test_the_sweep_tombstones_trajectories_whose_session_was_deleted(trace_db, blob_store, notifications):
    await seed_content("trj_a", blob_store)
    await seed_content("trj_b", blob_store)
    async with trace_session() as db:
        for session_id, deleted in (("session_trj_a", True), ("session_trj_b", False)):
            db.add(TrajectoryMetaSession(id=session_id, user_id="user_a", is_deleted=deleted, updated_at=now(),
                                         synced_at=now()))
    retention = service(blob_store)
    assert await retention.tombstone_deleted_sessions() == 1
    assert (await trajectory_row("trj_a")).recording_status == "deleted"
    assert (await trajectory_row("trj_b")).deleted_at is None
    assert [item["trajectory_id"] for item in notifications] == ["trj_a"]
    assert await retention.tombstone_deleted_sessions() == 0
