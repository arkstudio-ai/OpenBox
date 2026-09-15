"""ContentPlanner phases: bind, lookup keys, assign, and which references end up nested in blobs."""
import base64
import hashlib
import json

from trajectory.storage import decode_blob
from trajectory.types import canonical
from trajectory.worker.content import (BLOB_UNAVAILABLE, ContentPlanner, ExistingPayload, TrajectoryContent,
    dedupe_key, extract_media, media_digests, reference_keys)

TID = "trj_planner"


def test_reused_objects_can_be_stored_again_for_a_queued_gc_entry():
    system = "S" * 3000
    planner = ContentPlanner(inline_bytes=65536, blob_key=_key)
    plan, _ = _bind(planner, {"input": {"system": system}})
    [ref] = plan.refs
    row = ExistingPayload("pld_system", ref.dedupe_key, ref.sha256, "available", "blob", "zstd", 99, None, 1)
    lookup = TrajectoryContent.from_rows([row])
    planner.assign(plan, index=3, trajectory_id=TID, lookup=lookup)
    key = _key(TID, ref.sha256)
    # Reused without an upload, but listed so the ingest can look for queued GC entries.
    assert ref.payload_id == "pld_system" and planner.uploads == {} and planner.object_keys() == {key}
    assert planner.store_again(key) and planner.store_again("trajectories/trj_planner/blobs/unknown") is False
    upload = planner.uploads[key]
    assert upload.if_absent is False and upload.events == {3}
    assert decode_blob(upload.data, "zstd") == canonical(system)
    planner.release_reused()
    assert planner.object_keys() == {key} and planner.reused[key].content == b""
    # A queued key the blob store kept failing to store again is not referenced.
    for queued, expected in ((frozenset({key}), BLOB_UNAVAILABLE), (frozenset(), "pld_system")):
        planner = ContentPlanner(inline_bytes=65536, blob_key=_key)
        plan, _ = _bind(planner, {"input": {"system": system}})
        planner.assign(plan, index=0, trajectory_id=TID, lookup=lookup, unavailable={key}, queued=queued)
        stored = plan.data["input"]["system"]
        assert (stored if expected is BLOB_UNAVAILABLE else stored["$ref"]["payload_id"]) == expected


def _key(trajectory_id, sha):
    return f"trajectories/{trajectory_id}/blobs/{sha}"


def _bind(planner, data, *, event_type="request.prepared", lookup=None):
    media = extract_media(data)
    return planner.bind(trajectory_id=TID, event_type=event_type, data=data, media=media, helpers={},
                        lookup=lookup or TrajectoryContent(), assets={}, owner_user_id="u1", workspace_id="w1"), media


def test_bind_exposes_the_keys_to_look_up_before_ids_are_assigned():
    image = base64.b64encode(b"\x89PNG" + bytes(64)).decode()
    message = {"role": "user", "content": [{"type": "text", "text": "t" * 600},
                                           {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}]}
    planner = ContentPlanner(inline_bytes=65536, blob_key=_key)
    plan, media = _bind(planner, {"input": {"messages": [message]}})
    [media_ref, message_ref] = plan.refs
    assert media_ref.payload_id is None and message_ref.payload_id is None
    # The message blob embeds the bound media envelope, so its digest is final after bind.
    assert message_ref.style == "ref" and b'"$media"' in message_ref.content
    assert plan.data["input"]["messages"][0] == {"$ref": message_ref.envelope}
    keys, digests = reference_keys([plan])
    assert keys == {media_ref.dedupe_key, message_ref.dedupe_key}
    assert digests == {media_ref.sha256, message_ref.sha256} and media_digests(media) == {media_ref.sha256}
    # An existing row for the message is reused by assign without an upload.
    existing = ExistingPayload("pld_known", message_ref.dedupe_key, message_ref.sha256, "available", "blob",
                               "zstd", 99, None)
    lookup = TrajectoryContent.from_rows([existing])
    planner.assign(plan, index=0, trajectory_id=TID, lookup=lookup)
    assert message_ref.payload_id == "pld_known" and message_ref.existing
    assert set(planner.uploads) == {_key(TID, media_ref.sha256)}
    assert not media_ref.nested and not message_ref.nested


def test_large_values_and_whole_data_mark_inner_references_nested():
    planner = ContentPlanner(inline_bytes=4096, blob_key=_key)
    # Thirty message holders (about 200 bytes each) still exceed the limit together.
    messages = [{"role": "user", "content": "m" * 600} | {"n": index} for index in range(30)]
    plan, _ = _bind(planner, {"input": {"messages": messages}, "note": "small"})
    assert len(plan.refs) == 30
    planner.assign(plan, index=0, trajectory_id=TID, lookup=TrajectoryContent())
    value_refs = [ref for ref in plan.refs if ref.kind == "value"]
    assert value_refs and plan.data["note"] == "small"
    assert all(ref.nested for ref in plan.refs if ref.kind == "message")
    assert not any(ref.nested for ref in value_refs)

    planner = ContentPlanner(inline_bytes=2048, blob_key=_key)
    plan, _ = _bind(planner, {"input": {"messages": messages[:3]}, **{f"k{i}": "v" * 90 for i in range(40)}})
    planner.assign(plan, index=0, trajectory_id=TID, lookup=TrajectoryContent())
    whole = plan.refs[-1]
    assert whole.style == "payload" and not whole.nested and plan.data == {"$payload": whole.envelope}
    assert all(ref.nested for ref in plan.refs[:-1])


def test_small_events_skip_size_checks_with_a_size_hint():
    planner = ContentPlanner(inline_bytes=4096, blob_key=_key)
    plan, _ = _bind(planner, {"text": "x" * 5000}, event_type="tool.finished")
    planner.assign(plan, index=0, trajectory_id=TID, lookup=TrajectoryContent(), size_hint=100)
    assert plan.refs == [] and plan.data["text"] == "x" * 5000
    plan, _ = _bind(planner, {"text": "x" * 5000}, event_type="tool.finished")
    planner.assign(plan, index=1, trajectory_id=TID, lookup=TrajectoryContent(), size_hint=5100)
    assert plan.data["text"]["$ref"]["kind"] == "value"
    sha = hashlib.sha256(b'"' + b"x" * 5000 + b'"').hexdigest()
    assert plan.refs[0].sha256 == sha and plan.refs[0].dedupe_key == dedupe_key(sha, "application/json", None, "blob")


def test_only_failed_objects_become_markers_and_reused_payload_ids_keep_blob_keys():
    system, tools = "S" * 3000, [{"name": "t", "description": "D" * 3000}]
    fields = {f"k{index}": "v" * 90 for index in range(60)}

    def attempt(unavailable=frozenset(), payload_ids=None):
        planner = ContentPlanner(inline_bytes=4096, blob_key=_key, payload_ids=payload_ids)
        plan, _ = _bind(planner, {"input": {"system": system, "tools": tools}, **fields})
        planner.assign(plan, index=0, trajectory_id=TID, lookup=TrajectoryContent(), unavailable=unavailable)
        return planner, plan

    first, plan = attempt()
    system_ref, tools_ref, whole = plan.refs
    assert whole.style == "payload" and system_ref.nested and tools_ref.nested
    # The whole-data blob embeds the new payload ids: with fresh ids its key changes from plan to plan...
    assert attempt()[1].refs[-1].storage_key != whole.storage_key
    # ...and with the ids of the earlier attempt it does not.
    ids = first.new_payload_ids()
    again, same = attempt(payload_ids=ids)
    assert [ref.payload_id for ref in same.refs] == [ref.payload_id for ref in plan.refs]
    assert set(again.uploads) == set(first.uploads) == {system_ref.storage_key, tools_ref.storage_key, whole.storage_key}
    # Once the tools object kept failing, only its reference becomes a marker.
    degraded, marked = attempt(unavailable={tools_ref.storage_key}, payload_ids=ids)
    kept, failed, rewritten = marked.refs
    assert failed.failed and not kept.failed and not rewritten.failed and kept.payload_id == system_ref.payload_id
    assert set(degraded.uploads) == {system_ref.storage_key, rewritten.storage_key}
    stored = json.loads(decode_blob(degraded.uploads[rewritten.storage_key].data, rewritten.encoding))
    assert stored["input"]["tools"] == BLOB_UNAVAILABLE
    assert stored["input"]["system"]["$ref"]["payload_id"] == system_ref.payload_id
