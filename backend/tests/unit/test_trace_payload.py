"""Content references and payload reads against the trace database (SPEC 7.3, 8.9; wave-1 decisions 1-3)."""
import asyncio
import json

import pytest
from sqlalchemy import event, func, select, update

from tests.unit.test_worker_projection_support import (AT, JSON, add_asset, add_payload, add_trajectory, blobs,  # noqa: F401
    trace_db)
from trajectory.payload import (LruCache, blob_cache, ensure_payload_rows, expand, expand_all, expand_pages, json_blob,
    payload_meta, read_blob, read_payload, reference, reset_blob_cache, set_asset_reader, validate_payload)
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryPayload
from trajectory.types import CorruptContent, canonical

TRAJECTORY = "trj_payload"


@pytest.fixture
async def trajectory(trace_db, blobs):
    await add_trajectory(TRAJECTORY, "session_payload")
    return TRAJECTORY


def _ref(row, kind="value"):
    return {"$ref": {"sha256": row.sha256, "size_bytes": row.size_bytes, "media_type": JSON, "kind": kind,
                     "payload_id": row.payload_id}}


async def _row() -> SessionTrajectory:
    async with trace_session() as db:
        return await db.get(SessionTrajectory, TRAJECTORY)


async def test_visibility_availability_and_corruption_map_to_404_410_and_409(trajectory, blobs):
    image = await add_payload(blobs, TRAJECTORY, b"\x89PNG bytes", first_seq=5, media_type="image/png")
    deleted = await add_payload(blobs, TRAJECTORY, b"gone", first_seq=1, availability="deleted")
    expired = await add_payload(blobs, TRAJECTORY, b"old", first_seq=1, availability="expired")
    missing = await add_payload(blobs, TRAJECTORY, b"missing object", first_seq=1)
    swapped = await add_payload(blobs, TRAJECTORY, b"swapped object", first_seq=1)
    del blobs.objects[missing.storage_key]
    blobs.objects[swapped.storage_key] = b"other bytes"
    trajectory_row = await _row()
    async with trace_session() as db:
        for identity, through in ((image.payload_id, 4), ("pld_unknown", 9)):
            with pytest.raises(LookupError):
                await validate_payload(db, TRAJECTORY, identity, through_seq=through)
        row, content = await read_payload(db, TRAJECTORY, image.payload_id, through_seq=5)
        assert (row.payload_id, content) == (image.payload_id, b"\x89PNG bytes")
        assert await payload_meta(db, trajectory_row, image.payload_id, through_seq=5) == {
            "payload_id": image.payload_id, "availability": "available", "media_type": "image/png",
            "size_bytes": len(b"\x89PNG bytes"), "sha256": image.sha256}
        with pytest.raises(LookupError):
            await payload_meta(db, trajectory_row, image.payload_id, through_seq=4)
        for gone in (deleted, expired):
            with pytest.raises(FileNotFoundError):
                await read_payload(db, TRAJECTORY, gone.payload_id, through_seq=9)
            with pytest.raises(FileNotFoundError):
                await payload_meta(db, trajectory_row, gone.payload_id, through_seq=9)
        with pytest.raises(CorruptContent, match="missing"):
            await read_payload(db, TRAJECTORY, missing.payload_id, through_seq=1)
        with pytest.raises(CorruptContent, match="digest"):
            await read_payload(db, TRAJECTORY, swapped.payload_id, through_seq=1)
    await add_trajectory("trj_other", "session_other")
    async with trace_session() as db:
        with pytest.raises(LookupError):
            await validate_payload(db, "trj_other", image.payload_id, through_seq=9)


async def test_content_expired_trajectories_refuse_every_content_read(trajectory, blobs):
    row = await add_payload(blobs, TRAJECTORY, canonical({"a": 1}), first_seq=1, media_type=JSON)
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == TRAJECTORY).values(content_expired_at=AT))
    async with trace_session() as db:
        trajectory_row = await db.get(SessionTrajectory, TRAJECTORY)
        with pytest.raises(FileNotFoundError, match="expired"):
            await read_payload(db, TRAJECTORY, row.payload_id, through_seq=1)
        with pytest.raises(FileNotFoundError, match="expired"):
            await read_blob(db, trajectory_row, row.sha256, through_seq=1)
        with pytest.raises(FileNotFoundError, match="expired"):
            await payload_meta(db, trajectory_row, row.payload_id, through_seq=1)


async def test_asset_references_read_the_business_object_while_the_asset_replica_lives(trajectory, blobs):
    await add_asset("asset_live", oss_key="assets/live.png")
    await add_asset("asset_gone", is_deleted=True, deleted_at=AT)
    objects, reads = {"assets/live.png": b"img"}, []

    async def reader(key):
        reads.append(key)
        if key not in objects:
            raise FileNotFoundError(key)
        return objects[key]
    set_asset_reader(reader)
    live = await add_payload(blobs, TRAJECTORY, b"img", first_seq=1, media_type="image/png", storage_kind="asset",
                             source_asset_id="asset_live", storage_key="assets/live.png")
    gone = await add_payload(blobs, TRAJECTORY, b"img", first_seq=1, media_type="image/png", storage_kind="asset",
                             source_asset_id="asset_gone", storage_key="assets/gone.png")
    unsynced = await add_payload(blobs, TRAJECTORY, b"img", first_seq=1, media_type="image/png", storage_kind="asset",
                                 source_asset_id="asset_unsynced", storage_key="assets/unsynced.png")
    frame_gone = await add_payload(blobs, TRAJECTORY, b"frame 1", first_seq=1, media_type="image/jpeg", source_asset_id="asset_gone")
    frame_unsynced = await add_payload(blobs, TRAJECTORY, b"frame 2", first_seq=1, media_type="image/jpeg",
                                       source_asset_id="asset_unsynced")
    async with trace_session() as db:
        assert (await read_payload(db, TRAJECTORY, live.payload_id, through_seq=1))[1] == b"img" and reads == ["assets/live.png"]
        # An asset reference has no bytes of its own: it needs a live replica row.
        for row in (gone, unsynced, frame_gone):
            with pytest.raises(FileNotFoundError, match="attachment"):
                await read_payload(db, TRAJECTORY, row.payload_id, through_seq=1)
        # Copied frames stay readable until their source is known to be deleted.
        assert (await read_payload(db, TRAJECTORY, frame_unsynced.payload_id, through_seq=1))[1] == b"frame 2"
        objects["assets/live.png"] = b"changed"
        with pytest.raises(CorruptContent, match="digest"):
            await read_payload(db, TRAJECTORY, live.payload_id, through_seq=1)
        await db.execute(update(TrajectoryPayload).where(TrajectoryPayload.payload_id == live.payload_id).values(sha256=None))
    async with trace_session() as db:
        # Without a known hash there is nothing to verify.
        assert (await read_payload(db, TRAJECTORY, live.payload_id, through_seq=1))[1] == b"changed"
        objects.clear()
        with pytest.raises(FileNotFoundError, match="unavailable"):
            await read_payload(db, TRAJECTORY, live.payload_id, through_seq=1)


async def test_read_blob_returns_the_json_of_a_visible_value(trajectory, blobs):
    value = {"role": "system", "content": "Be exact"}
    row = await add_payload(blobs, TRAJECTORY, canonical(value), first_seq=3, media_type=JSON)
    image = await add_payload(blobs, TRAJECTORY, b"\x00binary", first_seq=1, media_type="image/png")
    deleted = await add_payload(blobs, TRAJECTORY, canonical("removed"), first_seq=1, media_type=JSON, availability="deleted")
    corrupt = await add_payload(blobs, TRAJECTORY, canonical("corrupt"), first_seq=1, media_type=JSON)
    blobs.objects[corrupt.storage_key] = b"\"tampered\""
    await add_trajectory("trj_other", "session_other")
    async with trace_session() as db:
        trajectory_row = await db.get(SessionTrajectory, TRAJECTORY)
        other = await db.get(SessionTrajectory, "trj_other")
        assert json.loads(await read_blob(db, trajectory_row, row.sha256, through_seq=3)) == value
        for sha, owner, through in ((row.sha256, trajectory_row, 2), ("f" * 64, trajectory_row, 9), ("../etc/passwd", trajectory_row, 9),
                                    (image.sha256, trajectory_row, 9), (row.sha256, other, 9)):
            with pytest.raises(LookupError):
                await read_blob(db, owner, sha, through_seq=through)
        with pytest.raises(FileNotFoundError):
            await read_blob(db, trajectory_row, deleted.sha256, through_seq=9)
        with pytest.raises(CorruptContent):
            await read_blob(db, trajectory_row, corrupt.sha256, through_seq=9)


async def test_expand_resolves_refs_payloads_and_current_availability(trajectory, blobs):
    inner = await add_payload(blobs, TRAJECTORY, canonical("inner text"), first_seq=1, media_type=JSON)
    outer = await add_payload(blobs, TRAJECTORY, canonical({"text": _ref(inner), "items": [1, 2]}), first_seq=1, media_type=JSON)
    image = await add_payload(blobs, TRAJECTORY, b"img", first_seq=1, media_type="image/png")
    gone = await add_payload(blobs, TRAJECTORY, b"gone", first_seq=1, media_type="image/png", availability="deleted")
    schema = {"type": "object", "properties": {"x": {"$ref": "#/definitions/x"}}}
    media = {"$media": {**reference(image), "availability": "available"}, "source_kind": "inline_non_asset",
             "original_encoding": "base64", "declared_media_type": "image/png"}
    artifact = {"artifact_id": "a1", "payload": {**reference(gone), "availability": "available"}}
    data = {"input": {"messages": [_ref(outer, "message")], "tools": schema}, "media": media, "artifact": artifact}
    async with trace_session() as db:
        full = await expand(db, TRAJECTORY, data, through_seq=1)
        assert full["input"] == {"messages": [{"text": "inner text", "items": [1, 2]}], "tools": schema}
        assert full["media"]["$media"]["availability"] == "available" and full["media"]["source_kind"] == "inline_non_asset"
        assert full["artifact"]["payload"]["availability"] == "deleted" and full["artifact"]["availability"] == "deleted"
        assert full["artifact"]["payload"]["reason"] == "explicitly_deleted"
        # expand=refs: only $ref values stay references, untouched.
        kept = await expand(db, TRAJECTORY, data, through_seq=1, refs=False)
        assert kept["input"] == data["input"] and kept["artifact"] == full["artifact"]
        assert await expand(db, TRAJECTORY, {"value": _ref(outer)}, through_seq=0) == {
            "value": {"$ref": {**_ref(outer)["$ref"], "availability": "not_recorded"}}}
    assert data["artifact"]["payload"]["availability"] == "available"


async def test_whole_data_payloads_load_as_today(trajectory, blobs):
    inner = await add_payload(blobs, TRAJECTORY, canonical("inner text"), first_seq=1, media_type=JSON)
    stored = await add_payload(blobs, TRAJECTORY, canonical({"big": _ref(inner)}), first_seq=2, media_type=JSON)
    listed = await add_payload(blobs, TRAJECTORY, canonical([1, 2]), first_seq=2, media_type=JSON)
    removed = await add_payload(blobs, TRAJECTORY, canonical({"x": 1}), first_seq=2, media_type=JSON, availability="deleted")

    def envelope(row):
        return {"$payload": {**reference(row), "availability": "available"}}
    async with trace_session() as db:
        assert await expand(db, TRAJECTORY, envelope(stored), through_seq=2) == {"big": "inner text"}
        assert await expand(db, TRAJECTORY, envelope(stored), through_seq=2, refs=False) == {"big": _ref(inner)}
        assert await expand(db, TRAJECTORY, envelope(removed), through_seq=2) == {
            "$payload": {**reference(removed), "availability": "deleted", "reason": "explicitly_deleted"}}
        with pytest.raises(LookupError):
            await expand(db, TRAJECTORY, envelope(stored), through_seq=1)
        with pytest.raises(CorruptContent, match="not an object"):
            await expand(db, TRAJECTORY, envelope(listed), through_seq=2)


async def test_many_values_resolve_with_a_constant_number_of_statements(trajectory, blobs, trace_db):
    rows = [await add_payload(blobs, TRAJECTORY, canonical(f"value {index}"), first_seq=1, media_type=JSON) for index in range(3)]
    values = [{"a": _ref(rows[index % 3]), "b": [{"payload_id": rows[1].payload_id, "sha256": rows[1].sha256}]}
              for index in range(50)]
    statements = []

    def listener(conn, cursor, statement, *args):
        statements.append(statement)
    event.listen(trace_db.sync_engine, "before_cursor_execute", listener)
    try:
        async with trace_session() as db:
            single = await expand_all(db, TRAJECTORY, values[:1], through_seq=1)
            first, statements[:] = len(statements), []
            many = await expand_all(db, TRAJECTORY, values, through_seq=1)
            # One query for the $ref rows and at most one for availability, however many values.
            assert len(statements) <= first <= 2
    finally:
        event.remove(trace_db.sync_engine, "before_cursor_execute", listener)
    assert single[0]["a"] == "value 0" and [value["a"] for value in many[:3]] == ["value 0", "value 1", "value 2"]


def test_lru_cache_keeps_its_byte_budget():
    cache = LruCache(10)
    cache.put("a", b"1", 4)
    cache.put("b", b"2", 4)
    assert cache.get("a") == b"1"
    cache.put("c", b"3", 4)
    assert (cache.get("b"), cache.get("a"), cache.size, len(cache)) == (None, b"1", 8, 2)
    cache.put("huge", b"4", 11)
    assert cache.get("huge") is None and cache.size == 8
    cache.put("a", b"5", 6)
    assert (cache.get("a"), cache.get("c"), cache.size) == (b"5", b"3", 10)
    cache.put("a", b"6", 11)
    assert (cache.get("a"), cache.size) == (None, 4)


async def test_blob_fetches_are_cached_and_at_most_sixteen_are_in_flight(trajectory, blobs, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_BLOB_CACHE_BYTES", "64")
    reset_blob_cache()
    rows = [await add_payload(blobs, TRAJECTORY, canonical(f"v{index:02d}"), first_seq=1, media_type=JSON) for index in range(40)]
    in_flight = peak = 0

    async def slow(_key):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
    blobs.get_hook = slow
    async with trace_session() as db:
        values = await expand_all(db, TRAJECTORY, [_ref(row) for row in rows], through_seq=1)
        assert values == [f"v{index:02d}" for index in range(40)]
        assert 1 < peak <= 16 and blob_cache().size <= 64
        gets = blobs.gets
        assert await expand(db, TRAJECTORY, _ref(rows[-1]), through_seq=1) == "v39"
        assert blobs.gets == gets


async def test_checkpoint_pages_must_be_available_objects_of_records(trajectory, blobs):
    good = await add_payload(blobs, TRAJECTORY, canonical({"records": {}}), first_seq=5, media_type=JSON)
    shapeless = await add_payload(blobs, TRAJECTORY, canonical({"rows": []}), first_seq=5, media_type=JSON)
    removed = await add_payload(blobs, TRAJECTORY, canonical({"records": {"x": {}}}), first_seq=5, media_type=JSON,
                                availability="deleted")
    async with trace_session() as db:
        assert await expand_pages(db, TRAJECTORY, [{"$payload": reference(good)}], through_seq=5) == [{"records": {}}]
        for references, through in (([{"$payload": reference(good)}], 4), ([{"$payload": reference(removed)}], 5),
                                    ([{"$payload": reference(shapeless)}], 5)):
            with pytest.raises(CorruptContent):
                await expand_pages(db, TRAJECTORY, references, through_seq=through)


async def test_payload_rows_are_deduplicated_per_trajectory_and_keep_given_ids(trajectory, blobs):
    first, second = json_blob(TRAJECTORY, {"a": 1}), json_blob(TRAJECTORY, {"b": 2})
    second["payload_id"] = "pld_" + "1" * 32
    async with trace_session() as db:
        rows, inserted = await ensure_payload_rows(db, TRAJECTORY, [first, second, dict(first)], first_seq=7)
        assert inserted == first["stored_bytes"] + second["stored_bytes"]
        assert rows[second["dedupe_key"]].payload_id == "pld_" + "1" * 32 and rows[first["dedupe_key"]].first_seq == 7
        again, inserted_again = await ensure_payload_rows(db, TRAJECTORY, [first], first_seq=9)
        assert inserted_again == 0 and again[first["dedupe_key"]].payload_id == rows[first["dedupe_key"]].payload_id
        assert again[first["dedupe_key"]].first_seq == 7
        assert await db.scalar(select(func.count()).select_from(TrajectoryPayload)) == 2
