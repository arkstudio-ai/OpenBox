"""Ingest of media and controls (SPEC §8.4 step 4, §8.5, §8.6): assets, deletions, ownership, gaps, pause."""
import base64
import hashlib

import pytest

from bus import bus
from trajectory import spool
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryGcQueue, TrajectoryMetaSession,
    TrajectoryMetaUser, TrajectoryPayload)
from tests.unit.test_worker_ingest import (AT, SpoolWriter, event, events_of, harness, rows, settings,  # noqa: F401
    trace_db)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(200))
GIF = b"GIF89a" + bytes(40)


def _image(content: bytes, media_type: str = "image/png") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{base64.b64encode(content).decode()}"}}


def _asset_meta(asset_id="asset_1", **fields) -> dict:
    return {"type": "asset.meta", "asset": {"id": asset_id, "user_id": "u1", "workspace_id": "ws_1",
                                            "oss_key": f"assets/u1/{asset_id}/a.png", "mime": "image/png",
                                            "size": len(PNG), "status": "ready", "updated_at": AT, **fields}}


def _prepared(request_id, *parts, **data):
    return event("request.prepared", request_id=request_id, event_id=f"request:{request_id}:prepared",
                 data={"model": "m", "input": {"messages": [{"role": "user", "content": list(parts)}]}, **data})


def _media(row) -> dict:
    return row.data["input"]["messages"][0]["content"][0]["image_url"]["url"]


async def test_media_becomes_asset_references_or_blobs(harness):
    sha = hashlib.sha256(PNG).hexdigest()
    harness.writer.controls(_asset_meta())
    harness.writer.events(_prepared("r1", _image(PNG), media_sources={sha: "asset_1"}),
                          _prepared("r2", _image(GIF, "image/gif")))
    await harness.run()
    _, stored = await events_of("ses_1")
    bound, inline = _media(stored[1]), _media(stored[2])
    assert "media_sources" not in stored[1].data
    assert (bound["source_kind"], bound["source_asset_id"], bound["original_encoding"]) == ("asset", "asset_1", "base64")
    [asset_row] = await rows(TrajectoryPayload, TrajectoryPayload.payload_id == bound["$media"]["payload_id"])
    assert (asset_row.storage_kind, asset_row.storage_key, asset_row.stored_bytes, asset_row.sha256) == (
        "asset", "assets/u1/asset_1/a.png", 0, sha)
    assert inline["source_kind"] == "inline_non_asset" and inline["declared_media_type"] == "image/gif"
    [blob_row] = await rows(TrajectoryPayload, TrajectoryPayload.payload_id == inline["$media"]["payload_id"])
    assert (blob_row.storage_kind, blob_row.encoding, blob_row.source_asset_id) == ("blob", "identity", None)
    assert list(harness.store.objects.values()) == [GIF]


@pytest.mark.parametrize("metadata_first", [False, True])
async def test_compact_media_is_ingested_deduplicated_and_revoked(harness, metadata_first):
    sha = hashlib.sha256(PNG).hexdigest()
    marker = {"$asset_media": {"asset_id": "asset_1", "oss_key": "assets/u1/asset_1/a.png",
                               "sha256": sha, "size_bytes": len(PNG), "media_type": "image/png"}}
    part = {"type": "image_url", "image_url": {"url": marker}}
    if metadata_first:
        harness.writer.controls(_asset_meta())
    # No media_sources helper: the worker must load metadata from the marker.
    harness.writer.events(_prepared("compact1", part), _prepared("compact2", part))
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    assert trajectory.recording_status == "recording"
    assert _media(stored[1]) == _media(stored[2])
    assert _media(stored[1])["$media"]["availability"] == "available"
    [row] = await rows(TrajectoryPayload)
    assert (row.storage_kind, row.storage_key, row.source_asset_id, row.sha256, row.size_bytes) == (
        "asset", "assets/u1/asset_1/a.png", "asset_1", sha, len(PNG))
    assert harness.store.objects == {}
    harness.writer.controls({"type": "asset.deleted", "asset_id": "asset_1", "user_id": "u1",
                             "deleted_at": "2026-09-14T09:00:00.000Z"})
    await harness.run()
    assert (await rows(TrajectoryPayload))[0].availability == "deleted"
    harness.writer.events(_prepared("compact3", part))
    await harness.run()
    assert _media((await events_of("ses_1"))[1][-1])["$media"]["availability"] == "deleted"
    assert len(await rows(TrajectoryPayload)) == 1


async def test_asset_deletion_revokes_references_everywhere(harness):
    sha = hashlib.sha256(PNG).hexdigest()
    received = []
    unsubscribe = bus.subscribe("trajectory.available", received.append)
    try:
        # Metadata not synced yet: the bytes are kept, bound to the asset.
        harness.writer.events(_prepared("r1", _image(PNG), media_sources={sha: "asset_1"}))
        await harness.run()
        trajectory, stored = await events_of("ses_1")
        [row] = await rows(TrajectoryPayload)
        assert (row.storage_kind, row.source_asset_id) == ("blob", "asset_1")
        harness.writer.controls({"type": "asset.deleted", "asset_id": "asset_1", "user_id": "u1",
                                 "deleted_at": "2026-09-14T09:00:00.000Z"})
        await harness.run()
    finally:
        unsubscribe()
    [row] = await rows(TrajectoryPayload)
    assert row.availability == "deleted" and row.deleted_at is not None
    assert [(item.kind, item.storage_key) for item in await rows(TrajectoryGcQueue)] == [("key", row.storage_key)]
    _, stored = await events_of("ses_1")
    removed = stored[-1]
    identity = hashlib.sha256(b"ses_1:asset_1:deleted").hexdigest()
    assert (removed.type, removed.event_id) == ("artifact.removed", f"asset:{identity}")
    assert removed.data == {"artifact_id": "asset_1", "availability": "deleted", "reason": "explicitly_deleted"}
    assert received[-1]["data"]["committed_seq"] == str(removed.seq)
    # The same image again, without a binding: never stored again, shown as deleted.
    objects = dict(harness.store.objects)
    harness.writer.events(_prepared("r2", _image(PNG)))
    await harness.run()
    media = _media((await events_of("ses_1"))[1][-1])
    assert media["$media"]["availability"] == "deleted" and harness.store.objects == objects
    assert len(await rows(TrajectoryPayload)) == 1


async def test_artifact_asset_ref_becomes_a_payload_reference(harness):
    harness.writer.events(event("artifact.recorded", event_id="artifact_1", data={
        "artifact_id": "asset_9", "name": "a.png",
        "asset_ref": {"asset_id": "asset_9", "oss_key": "assets/u1/asset_9/a.png", "media_type": "image/png",
                      "size_bytes": 42}}))
    await harness.run()
    row = (await events_of("ses_1"))[1][1]
    assert "asset_ref" not in row.data
    reference = row.data["payload"]
    assert (reference["sha256"], reference["size_bytes"], reference["media_type"], reference["availability"]) == (
        None, 42, "image/png", "available")
    [payload] = await rows(TrajectoryPayload)
    assert (payload.payload_id, payload.storage_kind, payload.storage_key, payload.source_asset_id, payload.first_seq) == (
        reference["payload_id"], "asset", "assets/u1/asset_9/a.png", "asset_9", row.seq)
    assert harness.store.objects == {}


async def test_history_forked_resolves_the_source_trajectory(harness):
    harness.writer.events(event(session="ses_src"), event(session="ses_src"))
    harness.writer.events(
        event("history.forked", session="ses_dst", event_id="fork:ses_dst:destination",
              data={"direction": "incoming", "source_session_id": "ses_src", "source_root_session_id": "ses_src"}),
        event("history.forked", session="ses_other", data={"source_root_session_id": "ses_unknown"}))
    await harness.run()
    source = (await events_of("ses_src"))[0]
    fork = (await events_of("ses_dst"))[1][1]
    assert fork.data == {"direction": "incoming", "source_session_id": "ses_src", "source_trajectory_id": source.id,
                         "source_through_seq": "3"}
    other = (await events_of("ses_other"))[1][1]
    assert (other.data["source_trajectory_id"], other.data["source_through_seq"]) == (None, "0")


async def test_session_deletion_tombstones_and_drops_late_events(harness):
    received = []
    unsubscribe = bus.subscribe("trajectory.available", received.append)
    try:
        harness.writer.events(event(), event())
        await harness.run()
        trajectory, _ = await events_of("ses_1")
        writer = harness.writer
        writer.file([writer.line("event", event(event_id="before")),
                     writer.line("control", {"type": "session.deleted", "session_id": "ses_1", "user_id": "u1",
                                             "deleted_at": AT}),
                     writer.line("event", event(event_id="after"))])
        result = await harness.run()
    finally:
        unsubscribe()
    assert harness.retention.calls == [(trajectory.id, "session_deleted")]
    assert result["deleted_drops"] == 1 and result["deleted_trajectories"] == {trajectory.id}
    trajectory, stored = await events_of("ses_1")
    assert trajectory.deleted_at is not None and stored == []
    assert received[-1]["data"]["deleted"] is True and received[-1]["data"]["trajectory_id"] == trajectory.id
    [meta_row] = await rows(TrajectoryMetaSession)
    assert meta_row.is_deleted
    harness.writer.events(event())
    assert (await harness.run())["deleted_drops"] == 1
    # A deletion that arrives before any recorded event keeps the session from ever starting.
    harness.writer.controls({"type": "session.deleted", "session_id": "ses_new", "user_id": "u1", "deleted_at": AT})
    harness.writer.events(event(session="ses_new"))
    result = await harness.run()
    assert result["deleted_drops"] == 1 and (await events_of("ses_new"))[0] is None
    assert harness.metrics.counters["deleted_drops"] == 3


async def test_meta_assisted_ownership_checks(harness):
    harness.writer.controls(
        {"type": "session.meta", "session": {"id": "ses_foreign", "user_id": "u2", "updated_at": AT}},
        {"type": "session.meta", "session": {"id": "ses_root", "user_id": "u1", "workspace_id": "ws_meta",
                                             "updated_at": AT}},
        {"type": "session.meta", "session": {"id": "ses_elsewhere", "user_id": "u1", "updated_at": AT}},
        {"type": "session.meta", "session": {"id": "ses_stray", "user_id": "u1", "parent_id": "ses_elsewhere",
                                             "updated_at": AT}},
        {"type": "user.meta", "user": {"id": "u1", "username": "alice", "updated_at": AT}})
    harness.writer.events(
        event(session="ses_foreign"),
        event(session="ses_root", source_session_id="ses_stray"),
        event(session="ses_root", source_session_id="ses_unknown_child", workspace_id=None),
        event(session="ses_root", event_id="kept"))
    result = await harness.run()
    assert result["ownership_drops"] == 2
    assert (await events_of("ses_foreign"))[0] is None
    trajectory, stored = await events_of("ses_root")
    assert [row.source_session_id for row in stored[1:]] == ["ses_unknown_child", "ses_root"]
    assert trajectory.workspace_id == "ws_meta"
    assert [row.username for row in await rows(TrajectoryMetaUser)] == ["alice"]


async def test_gap_controls_append_gap_events_per_run(harness):
    harness.writer.events(event(), event(session="ses_2", user="u2"))
    await harness.run()
    gap = {"type": "gap", "reason": "queue_overflow", "dropped_events": 7, "dropped_bytes": 900,
           "first_dropped_at": AT, "last_dropped_at": "2026-09-14T08:00:05.000Z",
           "sessions": [{"user_id": "u1", "session_id": "ses_1", "run_ids": [f"run_{i}" for i in range(12)],
                         "request_ids": ["req_a", "req_b"]},
                        {"user_id": "u1", "session_id": "ses_without_trajectory", "run_ids": [], "request_ids": []},
                        {"user_id": "u1", "session_id": "ses_2", "run_ids": ["run_x"], "request_ids": []}]}
    harness.writer.controls(gap)
    result = await harness.run()
    n = harness.writer.n
    _, stored = await events_of("ses_1")
    gaps = [row for row in stored if row.type == "recording.gap"]
    producer = harness.writer.producer_id
    assert [row.event_id for row in gaps] == [f"gap:{producer}:{n}:ses_1:run_{i}" for i in range(10)] + [
        f"gap:{producer}:{n}:ses_1"]
    assert gaps[0].context == {"run_id": "run_0"} and gaps[-1].context == {}
    assert gaps[0].data == {"phase": "dropped", "reason": "queue_overflow", "dropped_events": 7, "dropped_bytes": 900,
                            "producer_id": producer, "request_ids": ["req_a", "req_b"]}
    assert result["gaps"] == 11 and harness.metrics.counters["gaps_recorded"] == 11
    assert [row.type for row in (await events_of("ses_2"))[1]] == ["trajectory.started", "input.accepted"]


@pytest.mark.parametrize("truncated", [True, False, None])
async def test_capped_gap_control_preserves_uncertain_coverage_after_worker_restart(harness, truncated):
    count = spool.GAP_MAX_SESSIONS + 5
    harness.writer.events(*(event(session=f"ses_{index}", user=f"u{index}") for index in range(count)))
    other = SpoolWriter(harness.settings.spool_dir, producer_id="20260914080000-other-1-bbbbbbbb")
    other.events(event(session="ses_other", user="other"))
    await harness.run()
    await harness.service.flush_state()
    harness.service = harness._service()
    control = {"type": "gap", "reason": "queue_overflow", "dropped_events": count, "dropped_bytes": 10000,
               "first_dropped_at": AT, "last_dropped_at": AT,
               "sessions": [{"user_id": f"u{index}", "session_id": f"ses_{index}", "run_ids": [],
                             "request_ids": []} for index in range(spool.GAP_MAX_SESSIONS)]}
    if truncated is not None:
        control["sessions_truncated"] = truncated
    harness.writer.controls(control)
    await harness.run()
    gaps = [row for row in await rows(TrajectoryEvent) if row.type == "recording.gap"]
    expected = spool.GAP_MAX_SESSIONS if truncated is False else count
    assert {row.session_id for row in gaps} == {f"ses_{index}" for index in range(expected)}
    assert len(gaps) == expected
    for gap in gaps:
        index = int(gap.session_id.removeprefix("ses_"))
        if index >= spool.GAP_MAX_SESSIONS:
            assert gap.context == {}
            assert gap.data["sessions_truncated"] is True and gap.data["session_scope"] == "producer"
            assert "dropped_events" not in gap.data


async def test_recording_state_pauses_and_resumes(harness):
    harness.writer.events(event())
    await harness.run()
    state = {"type": "recording.state", "user_id": "u1", "session_id": "ses_1", "reason": "recording_disabled",
             "at": "2026-09-14T08:01:00.000Z"}
    harness.writer.controls({**state, "state": "paused"}, {**state, "state": "paused"},
                            {**state, "state": "resumed", "at": "2026-09-14T08:02:00.000Z"},
                            {**state, "session_id": "ses_unknown", "state": "paused"})
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    paused, resumed = stored[2], stored[3]
    assert len(stored) == 4
    assert paused.data == {"phase": "paused", "reason": "recording_disabled", "last_recorded_seq": "2"}
    assert resumed.data == {"phase": "resumed", "reason": "recording_reenabled", "previous_committed_seq": "3"}
    assert (trajectory.recording_status, trajectory.recording_epoch) == ("gap", 1)
    assert paused.event_id == f"gap:{harness.writer.producer_id}:2:ses_1"
    [row] = await rows(SessionTrajectory)
    assert row.id == trajectory.id


async def test_duplicate_recording_state_controls_from_several_processes_apply_once(harness):
    """Producers deduplicate resumes per process only (trajectory/producers.py): two processes resuming the
    same period both emit ``resumed``, and a pause can be reported twice. The control carries no epoch, so
    the trajectory's own state decides: a resume ends only a pause, a pause only a live recording."""
    harness.writer.events(event())
    await harness.run()
    other = SpoolWriter(harness.settings.spool_dir, "20260914080005-other-7-dddddddd")
    state = {"type": "recording.state", "user_id": "u1", "session_id": "ses_1", "reason": "recording_disabled",
             "at": "2026-09-14T08:01:00.000Z"}
    resumed = {**state, "state": "resumed", "reason": "recording_reenabled", "at": "2026-09-14T08:02:00.000Z"}
    # Oldest file first across producers: pause, duplicate pause, resume, duplicate resume.
    harness.writer.controls({**state, "state": "paused"}, age=50)
    other.controls({**state, "state": "paused"}, age=40)
    harness.writer.controls(resumed, age=30)
    other.controls(resumed, age=20)
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    gaps = [(row.data["phase"], row.event_id) for row in stored if row.type == "recording.gap"]
    assert gaps == [("paused", f"gap:{harness.writer.producer_id}:2:ses_1"),
                    ("resumed", f"gap:{harness.writer.producer_id}:3:ses_1")]
    assert (trajectory.recording_status, trajectory.recording_epoch) == ("gap", 1)
    # The next period pauses and resumes once more, whatever the duplicates.
    harness.writer.controls({**state, "state": "paused", "at": "2026-09-14T08:03:00.000Z"}, resumed, resumed,
                            {**state, "state": "resumed", "at": "2026-09-14T08:04:00.000Z"})
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    assert [row.data["phase"] for row in stored if row.type == "recording.gap"] == [
        "paused", "resumed", "paused", "resumed"]
    assert (trajectory.recording_status, trajectory.recording_epoch) == ("gap", 2)
    # A resume without a pause (a trajectory that never paused) changes nothing.
    harness.writer.events(event(session="ses_2"))
    harness.writer.controls({**resumed, "session_id": "ses_2"})
    await harness.run()
    trajectory, stored = await events_of("ses_2")
    assert [row.type for row in stored] == ["trajectory.started", "input.accepted"]
    assert (trajectory.recording_status, trajectory.recording_epoch) == ("recording", 0)


async def test_recording_state_epochs_apply_each_transition_once(harness):
    """Contract 6: a resume carries the epoch R of the period it opens and a pause R - 1. A control applies only
    when its epoch is greater than the last applied one, whichever producer or file it comes from; the epoch (a
    millisecond timestamp) is kept as the trajectory's recording epoch."""
    harness.writer.events(event())
    await harness.run()
    other = SpoolWriter(harness.settings.spool_dir, "20260914080005-other-7-dddddddd")
    first, second = 1_789_000_000_000, 1_789_000_060_000
    state = {"type": "recording.state", "user_id": "u1", "session_id": "ses_1", "at": "2026-09-14T08:01:00.000Z"}
    pause = {**state, "state": "paused", "reason": "recording_disabled"}
    resume = {**state, "state": "resumed", "reason": "recording_reenabled"}
    # Oldest file first: the pause reported twice, its resume twice, then that pause once more (a crashed
    # producer's abandoned file ingested after the resume).
    harness.writer.controls({**pause, "epoch": first - 1}, age=50)
    other.controls({**pause, "epoch": first - 1}, age=45)
    harness.writer.controls({**resume, "epoch": first}, age=40)
    other.controls({**resume, "epoch": first}, age=35)
    other.controls({**pause, "epoch": first - 1}, age=30)
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    gaps = [(row.data["phase"], row.event_id) for row in stored if row.type == "recording.gap"]
    assert gaps == [("paused", f"gap:{harness.writer.producer_id}:2:ses_1"),
                    ("resumed", f"gap:{harness.writer.producer_id}:3:ses_1")]
    assert (trajectory.recording_status, trajectory.recording_epoch) == ("gap", first)
    # The next period, with a late resume of the previous one in between.
    harness.writer.controls({**pause, "epoch": second - 1}, {**resume, "epoch": first}, {**resume, "epoch": second})
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    assert [row.data["phase"] for row in stored if row.type == "recording.gap"] == [
        "paused", "resumed", "paused", "resumed"]
    assert (trajectory.recording_status, trajectory.recording_epoch) == ("gap", second)
    # Without an epoch the status rule still applies: this resume ends no pause.
    harness.writer.controls(resume, {**pause, "epoch": "not a number"})
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    assert [row.data["phase"] for row in stored if row.type == "recording.gap"][4:] == ["paused"]
    assert (trajectory.recording_status, trajectory.recording_epoch) == ("paused", second)


async def test_a_session_deletion_publishes_its_deleted_notification_once(trace_db, settings):
    from tests.unit.test_worker_ingest import FakeMetrics
    from trajectory.storage import MemoryBlobStore
    from trajectory.worker.ingest import IngestService
    from trajectory.worker.retention import RetentionService

    store, metrics = MemoryBlobStore(), FakeMetrics()
    retention = RetentionService(settings, blob_store=store, metrics=metrics)
    service = IngestService(settings, blob_store=store, metrics=metrics, retention=retention)
    writer = SpoolWriter(settings.spool_dir)
    received = []
    unsubscribe = bus.subscribe("trajectory.available", received.append)
    try:
        writer.events(event(), event())
        await service.run_once()
        trajectory, _ = await events_of("ses_1")
        writer.controls({"type": "session.deleted", "session_id": "ses_1", "user_id": "u1", "deleted_at": AT})
        result = await service.run_once()
    finally:
        unsubscribe()
    assert [item["data"] for item in received if item["data"].get("deleted")] == [
        {"user_id": "u1", "owner_user_id": "u1", "session_id": "ses_1", "trajectory_id": trajectory.id,
         "committed_seq": "3", "deleted": True}]
    assert result["deleted_trajectories"] == {trajectory.id}
    assert (await events_of("ses_1"))[0].deleted_at is not None
    # Counted once, after the commit, for the retention service's daily report.
    assert retention.report.counters["tombstones_processed"] == 1
