"""ContentPlanner phases: bind, lookup keys, assign, and which references end up nested in blobs."""
import base64
import hashlib

from trajectory.worker.content import (ContentPlanner, ExistingPayload, TrajectoryContent, dedupe_key, extract_media,
    media_digests, reference_keys)

TID = "trj_planner"


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
