"""Content preparation (SPEC §8.4): NUL replacement, hash, hints, media, content addressing, fallbacks."""
import base64
import hashlib
import json

import pytest

from trajectory.storage import decode_blob
from trajectory.types import canonical, digest
from trajectory.worker.content import (BLOB_UNAVAILABLE, AssetView, ContentPlanner, ExistingPayload,
    TrajectoryContent, dedupe_key, extract_media, final_hints, hash_event, has_media_markers, media_sources,
    normalize_media_type, previews, prepare_data, strip_helpers)

TID = "trj_content"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _key(trajectory_id, sha):
    return f"trajectories/{trajectory_id}/blobs/{sha}"


def _planner(inline_bytes=65536):
    return ContentPlanner(inline_bytes=inline_bytes, blob_key=_key)


def _plan(planner, data, *, event_type="tool.finished", helpers=None, lookup=None, assets=None, index=0,
          unavailable=frozenset(), workspace_id="w1"):
    media = extract_media(data)
    return planner.plan(index=index, trajectory_id=TID, event_type=event_type, data=data, media=media,
                        helpers=helpers or {}, lookup=lookup or TrajectoryContent(), assets=assets or {},
                        owner_user_id="u1", workspace_id=workspace_id, unavailable=unavailable)


def _existing(payload_id, sha, *, media_type="application/json", source=None, kind="blob", availability="available",
              encoding="identity", stored=10):
    return ExistingPayload(payload_id, dedupe_key(sha, media_type, source, kind), sha, availability, kind, encoding,
                           stored, source)


@pytest.mark.parametrize("event", [
    {"type": "input.accepted", "version": 1, "event_id": "e1", "occurred_at": "2026-09-14T08:00:00.000Z",
     "user_id": "u", "session_id": "s", "source_session_id": "s", "data": {"text": "你好", "n": [1, 2.5, None]}},
    {"type": "tool.finished", "version": 1, "event_id": "e2", "user_id": "u", "session_id": "s",
     "generation": 3, "call_id": "c", "data": {"z": {"b": 1, "a": " "}, "emoji": "😀"}},
])
def test_hash_matches_the_recorder_definition(event):
    expected = digest({key: value for key, value in event.items() if key not in {"event_id", "occurred_at"}})
    assert hash_event(event) == expected
    assert hash_event({**event, "event_id": "other", "occurred_at": "2027-01-01T00:00:00.000Z"}) == expected


def test_data_preparation_replaces_nul_and_keeps_secrets_verbatim():
    data = {"api_key": "sk-1234567890abcdef", "text": "Bearer abc.def", "nul\x00key": "a\x00b"}
    raw = json.dumps(data).encode()
    assert b"\\u0000" in raw
    result = prepare_data(data, raw)
    assert result["api_key"] == "sk-1234567890abcdef"
    assert result["text"] == "Bearer abc.def"
    assert result["nul�key"] == "a�b"
    # Without a \\u0000 escape in the line, the NUL pass is skipped.
    assert prepare_data({"text": "plain"}, b'{"text":"plain"}') == {"text": "plain"}


def test_previews_helpers_and_hints():
    data = {"text": "x" * 300, "output": {"k": "v"}, "status": "ok", "media_sources": {"a" * 64: "asset_1"},
            "asset_ref": {"asset_id": "asset_1"}, "source_root_session_id": "root"}
    helpers = strip_helpers(data)
    assert set(helpers) == {"media_sources", "asset_ref", "source_root_session_id"}
    assert set(data) == {"text", "output", "status"}
    candidates = previews(data)
    assert candidates == {"text": "x" * 240, "output": '{"k":"v"}'}
    assert final_hints(candidates, data) is None
    data["text"] = {"$ref": {"sha256": "0" * 64}}
    assert final_hints(candidates, data) == {"preview": {"text": "x" * 240}}
    assert final_hints(candidates, {"$payload": {}}) == {"preview": candidates}
    assert media_sources({"a" * 64: "asset", "bad": "x", "b" * 64: 5}) == {"a" * 64: "asset"}
    assert normalize_media_type(" Image/PNG ") == "image/png"
    assert normalize_media_type(None) == "application/octet-stream"


def test_inline_media_forms_are_extracted_and_share_holders():
    encoded = base64.b64encode(PNG).decode()
    data = {"input": {"messages": [
        {"content": [{"type": "image_url", "image_url": {"url": f"data:image/png;charset=x;base64,{encoded}"}},
                     {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]},
        {"content": [{"type": "base64", "media_type": "image/png", "data": encoded}]},
        {"input_audio": {"data": base64.b64encode(b"RIFF").decode(), "format": "mp3"}},
        {"content": "data:image/png;base64,***"},
    ]}}
    assert has_media_markers(json.dumps(data).encode())
    items = extract_media(data)
    by_type = {(item.media_type, item.content is not None) for item in items}
    assert by_type == {("image/png", True), ("audio/mp3", True), ("image/png", False)}
    messages = data["input"]["messages"]
    first = messages[0]["content"][0]["image_url"]["url"]
    assert first is messages[0]["content"][1]["image_url"]["url"] is messages[1]["content"][0]
    assert not has_media_markers(b'{"text":"plain"}')


def test_unbound_media_becomes_a_blob_and_invalid_base64_a_marker():
    encoded = base64.b64encode(PNG).decode()
    planner = _planner()
    data = {"image": f"data:image/png;base64,{encoded}", "broken": "data:image/gif;base64,@@@"}
    plan = _plan(planner, data)
    sha = hashlib.sha256(PNG).hexdigest()
    assert plan.data["broken"] == {"$media": {"availability": "not_recorded", "reason": "invalid_base64",
                                              "media_type": "image/gif"}}
    media = plan.data["image"]
    assert media["source_kind"] == "inline_non_asset" and media["original_encoding"] == "base64"
    assert media["$media"]["sha256"] == sha and media["$media"]["payload_id"].startswith("pld_")
    [ref] = plan.refs
    assert (ref.storage_kind, ref.encoding, ref.storage_key) == ("blob", "identity", _key(TID, sha))
    assert planner.uploads[_key(TID, sha)].data == PNG


def test_media_bound_to_a_known_asset_stores_no_bytes():
    encoded = base64.b64encode(PNG).decode()
    sha = hashlib.sha256(PNG).hexdigest()
    asset = AssetView("asset_1", "u1", "w1", "assets/u1/asset_1/a.png", "image/png", len(PNG), False)
    planner = _planner()
    plan = _plan(planner, {"image": f"data:image/png;base64,{encoded}"}, helpers={"media_sources": {sha: "asset_1"}},
                 assets={"asset_1": asset})
    media = plan.data["image"]
    assert media["source_kind"] == "asset" and media["source_asset_id"] == "asset_1"
    [ref] = plan.refs
    assert (ref.storage_kind, ref.storage_key, ref.source_asset_id) == ("asset", asset.oss_key, "asset_1")
    assert planner.uploads == {}
    # Metadata not synced yet: bytes are kept, still bound to the asset.
    planner = _planner()
    plan = _plan(planner, {"image": f"data:image/png;base64,{encoded}"}, helpers={"media_sources": {sha: "asset_1"}})
    [ref] = plan.refs
    assert (ref.storage_kind, ref.source_asset_id) == ("blob", "asset_1") and len(planner.uploads) == 1


def test_media_sources_deleted_ambiguous_and_single_candidates():
    encoded = base64.b64encode(PNG).decode()
    sha = hashlib.sha256(PNG).hexdigest()
    deleted = AssetView("asset_1", "u1", "w1", "k", "image/png", 1, True)
    plan = _plan(_planner(), {"image": f"data:image/png;base64,{encoded}"}, helpers={"media_sources": {sha: "asset_1"}},
                 assets={"asset_1": deleted})
    assert plan.data["image"]["$media"] == {"availability": "deleted", "reason": "source_attachment_deleted",
                                            "sha256": sha, "media_type": "image/png"}
    assert plan.refs == []
    lookup = TrajectoryContent.from_rows([_existing("pld_a", sha, media_type="image/png", source="asset_a"),
                                          _existing("pld_b", sha, media_type="image/png", source="asset_b")])
    plan = _plan(_planner(), {"image": f"data:image/png;base64,{encoded}"}, lookup=lookup)
    assert plan.data["image"]["source_kind"] == "ambiguous_asset" and plan.refs == []
    lookup = TrajectoryContent.from_rows([_existing("pld_a", sha, media_type="image/png", source="asset_a",
                                                    availability="deleted")])
    plan = _plan(_planner(), {"image": f"data:image/png;base64,{encoded}"}, lookup=lookup,
                 assets={"asset_a": AssetView("asset_a", "u1", "w1", "k", "image/png", 1, True)})
    assert plan.data["image"]["$media"]["availability"] == "deleted"
    # A foreign asset outside the workspace is not a valid binding.
    foreign = AssetView("asset_x", "u2", "w2", "assets/u2/x", "image/png", 1, False)
    plan = _plan(_planner(), {"image": f"data:image/png;base64,{encoded}"},
                 helpers={"media_sources": {sha: "asset_x"}}, assets={"asset_x": foreign})
    assert plan.data["image"]["source_kind"] == "inline_non_asset"


def test_existing_and_deleted_rows_are_reused_without_uploads():
    encoded = base64.b64encode(PNG).decode()
    sha = hashlib.sha256(PNG).hexdigest()
    lookup = TrajectoryContent.from_rows([_existing("pld_old", sha, media_type="image/png", stored=77)])
    planner = _planner()
    plan = _plan(planner, {"image": f"data:image/png;base64,{encoded}"}, lookup=lookup)
    assert plan.data["image"]["$media"]["payload_id"] == "pld_old"
    assert plan.refs[0].existing and planner.uploads == {}
    # Same bytes previously deleted (under another media type): never stored again.
    lookup = TrajectoryContent.from_rows([_existing("pld_gone", sha, media_type="image/jpeg", availability="deleted")])
    planner = _planner()
    plan = _plan(planner, {"image": f"data:image/png;base64,{encoded}"}, lookup=lookup)
    reference = plan.data["image"]["$media"]
    assert (reference["payload_id"], reference["availability"], reference["reason"]) == (
        "pld_gone", "deleted", "explicitly_deleted")
    assert plan.refs[0].blocked and planner.uploads == {}


def test_request_input_is_content_addressed_once_per_unique_value():
    system = "s" * 1100
    message = {"role": "user", "content": "m" * 600}
    data = {"input": {"system": system, "instructions": "short", "tools": [{"name": "t", "d": "x" * 1100}],
                      "messages": [message, dict(message), {"role": "user", "content": "tiny"}]},
            "model": "m"}
    planner = _planner()
    plan = _plan(planner, data, event_type="request.prepared")
    value = plan.data["input"]
    assert value["system"]["$ref"]["kind"] == "system" and value["instructions"] == "short"
    assert value["tools"]["$ref"]["kind"] == "tools"
    first, second, third = value["messages"]
    assert first["$ref"]["kind"] == "message" and third == {"role": "user", "content": "tiny"}
    assert first["$ref"]["payload_id"] == second["$ref"]["payload_id"]
    assert first["$ref"]["size_bytes"] == len(canonical(message))
    assert set(first["$ref"]) == {"sha256", "size_bytes", "media_type", "kind", "payload_id"}
    assert len(planner.uploads) == 3
    upload = planner.uploads[_key(TID, first["$ref"]["sha256"])]
    assert json.loads(upload.data) == message and upload.content_type == "application/json"
    assert final_hints(plan.previews, plan.data) == {"preview": {"input": plan.previews["input"]}}
    # Another event of the same batch reuses the ids and needs no new objects.
    again = _plan(planner, {"input": {"system": system}}, event_type="request.prepared", index=1)
    assert again.data["input"]["system"]["$ref"]["payload_id"] == value["system"]["$ref"]["payload_id"]
    assert len(planner.uploads) == 3 and planner.uploads[_key(TID, value["system"]["$ref"]["sha256"])].events == {0, 1}


def test_large_values_and_whole_data_fallback():
    planner = _planner(inline_bytes=2048)
    data = {"status": "ok", "result": {"output": "o" * 5000, "code": 0}, "items": ["i" * 100] * 30}
    plan = _plan(planner, data)
    assert plan.data["status"] == "ok"
    assert plan.data["result"]["output"]["$ref"]["kind"] == "value" and plan.data["result"]["code"] == 0
    assert isinstance(plan.data["items"], dict) and plan.data["items"]["$ref"]["kind"] == "value"
    many = {f"k{i}": "v" * 90 for i in range(40)}
    plan = _plan(_planner(inline_bytes=2048), many)
    envelope = plan.data["$payload"]
    assert envelope["media_type"] == "application/json" and envelope["availability"] == "available"
    [ref] = plan.refs
    assert ref.style == "payload" and ref.encoding == "zstd"
    assert final_hints({"text": "x"}, plan.data) == {"preview": {"text": "x"}}


def test_whole_data_blob_contains_final_nested_references():
    planner = _planner(inline_bytes=4096)
    data = {"output": "o" * 5000, **{f"k{i}": "v" * 100 for i in range(40)}}
    plan = _plan(planner, data)
    whole = plan.data["$payload"]
    upload = planner.uploads[_key(TID, whole["sha256"])]
    stored = json.loads(decode_blob(upload.data, "zstd"))
    assert stored["output"]["$ref"]["payload_id"] is not None
    assert hashlib.sha256(canonical(stored)).hexdigest() == whole["sha256"]


def test_references_to_objects_the_store_kept_failing_become_markers():
    encoded = base64.b64encode(PNG).decode()
    sha = hashlib.sha256(PNG).hexdigest()
    image, output = _key(TID, sha), _key(TID, hashlib.sha256(canonical("o" * 3000)).hexdigest())
    planner = _planner(inline_bytes=1024)
    data = {"image": f"data:image/png;base64,{encoded}", "output": "o" * 3000, "small": 1}
    plan = _plan(planner, data, unavailable={image, output})
    assert plan.data["image"]["$media"] == {**BLOB_UNAVAILABLE, "media_type": "image/png"}
    assert plan.data["output"] == BLOB_UNAVAILABLE and plan.data["small"] == 1
    assert planner.uploads == {} and all(ref.failed for ref in plan.refs)
    # Only the objects that failed: the rest of the event's content is still stored and referenced.
    planner = _planner(inline_bytes=1024)
    plan = _plan(planner, {"image": f"data:image/png;base64,{encoded}", "output": "o" * 3000}, unavailable={image})
    assert plan.data["image"]["$media"] == {**BLOB_UNAVAILABLE, "media_type": "image/png"}
    assert plan.data["output"]["$ref"]["payload_id"].startswith("pld_") and set(planner.uploads) == {output}
    # Bytes that already exist are still referenced.
    lookup = TrajectoryContent.from_rows([_existing("pld_img", sha, media_type="image/jpeg")])
    plan = _plan(_planner(), {"image": f"data:image/png;base64,{encoded}"}, lookup=lookup, unavailable={image})
    assert plan.data["image"]["$media"]["availability"] == "available" and not plan.refs[0].failed


def test_artifact_asset_references():
    asset = AssetView("asset_1", "u1", "w1", "assets/u1/asset_1/a.png", "image/png", 42, False)
    data = {"artifact_id": "asset_1"}
    plan = _plan(_planner(), data, event_type="artifact.recorded",
                 helpers={"asset_ref": {"asset_id": "asset_1", "oss_key": "assets/u1/asset_1/a.png",
                                        "media_type": "image/png", "size_bytes": 42}}, assets={"asset_1": asset})
    reference = plan.data["payload"]
    assert reference["payload_id"].startswith("pld_") and reference["sha256"] is None
    assert (reference["size_bytes"], reference["media_type"], reference["availability"]) == (42, "image/png", "available")
    [ref] = plan.refs
    assert (ref.storage_kind, ref.storage_key, ref.source_asset_id) == ("asset", asset.oss_key, "asset_1")
    gone = AssetView("asset_1", "u1", "w1", "assets/u1/asset_1/a.png", "image/png", 42, True)
    plan = _plan(_planner(), {"artifact_id": "asset_1"}, event_type="artifact.recorded",
                 helpers={"asset_ref": {"asset_id": "asset_1", "oss_key": "assets/u1/asset_1/a.png"}},
                 assets={"asset_1": gone})
    assert plan.data["payload"]["availability"] == "deleted"
    for bad in ({"asset_id": "asset_1", "oss_key": "../escape"}, {"oss_key": "k"}, "nope"):
        plan = _plan(_planner(), {"artifact_id": "a"}, event_type="artifact.recorded", helpers={"asset_ref": bad})
        assert plan.data["payload"] == {"availability": "not_recorded", "reason": "invalid_asset_ref"}
