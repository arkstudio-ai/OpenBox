"""Trajectory blob stores, encodings, keys, fault injection and configuration."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, unquote
from xml.etree import ElementTree

import httpx
import pytest
import zstandard

import core.oss as oss
import trajectory.storage as storage
from trajectory.storage import (
    BlobFaultInjected,
    BlobStore,
    FaultInjectingBlobStore,
    LocalBlobStore,
    MemoryBlobStore,
    OssBlobStore,
    blob_key,
    check_key,
    checkpoint_key,
    decode_blob,
    encode_blob,
    export_key,
    get_blob_store,
    parse_blob_faults,
    segment_key,
    set_blob_store,
    trajectory_prefix,
)
from trajectory.types import CorruptContent

SHA = "a" * 64
SHA2 = "0123456789abcdef" * 4
BLOB_ENV = (
    "TRAJECTORY_BLOB_PROVIDER", "TRAJECTORY_BLOB_LOCAL_PATH", "TRAJECTORY_BLOB_FAULT",
    "TRAJECTORY_OSS_BUCKET", "TRAJECTORY_OSS_REGION", "TRAJECTORY_OSS_ENDPOINT",
    "TRAJECTORY_OSS_PREFIX", "TRAJECTORY_OSS_INTERNAL",
)


@pytest.fixture(autouse=True)
def blob_environment(monkeypatch):
    for name in BLOB_ENV:
        monkeypatch.delenv(name, raising=False)
    set_blob_store(None)
    yield
    set_blob_store(None)


async def keys_of(store, prefix: str) -> list[str]:
    return [key async for key in store.list(prefix)]


# -- Encodings --

REQUEST_JSON = json.dumps([{"role": "user", "content": f"message {index}"} for index in range(200)]).encode()


@pytest.mark.parametrize("media_type", [
    "application/json", "application/x-ndjson", "text/plain; charset=utf-8", "TEXT/HTML",
    "application/xml", "application/javascript",
])
def test_compressible_types_use_zstd_level_3(media_type):
    stored, encoding = encode_blob(REQUEST_JSON, media_type)
    assert encoding == "zstd"
    assert stored == zstandard.ZstdCompressor(level=3).compress(REQUEST_JSON)
    assert len(stored) < len(REQUEST_JSON)
    assert zstandard.frame_content_size(stored) == len(REQUEST_JSON)
    assert decode_blob(stored, encoding) == REQUEST_JSON


@pytest.mark.parametrize("media_type", ["image/png", "application/octet-stream", "video/mp4", "application/pdf", "text", ""])
def test_other_types_stay_identity(media_type):
    content = os.urandom(4096)
    assert encode_blob(content, media_type) == (content, "identity")
    assert decode_blob(content, "identity") == content


def test_compression_starts_at_1024_bytes():
    assert encode_blob(b"x" * 1023, "application/json") == (b"x" * 1023, "identity")
    stored, encoding = encode_blob(b"x" * 1024, "application/json")
    assert encoding == "zstd" and decode_blob(stored, encoding) == b"x" * 1024


def test_decode_blob_keeps_every_frame_and_frames_without_a_size():
    first, second = b"first " * 500, b"second " * 500
    assert decode_blob(zstandard.compress(first, 3) + zstandard.compress(second, 3), "zstd") == first + second
    streaming = zstandard.ZstdCompressor(level=3).compressobj()
    unknown_size = streaming.compress(first) + streaming.flush()
    assert zstandard.frame_content_size(unknown_size) == -1
    assert decode_blob(unknown_size, "zstd") == first


@pytest.mark.parametrize("stored,encoding", [
    (zstandard.compress(b"payload " * 400, 3)[:-8], "zstd"),
    (zstandard.compress(b"payload " * 400, 3) + b"trailing", "zstd"),
    (b"not a zstd frame", "zstd"),
    (b"", "zstd"),
    (b"anything", "gzip"),
])
def test_decode_blob_rejects_damaged_content(stored, encoding):
    with pytest.raises(CorruptContent):
        decode_blob(stored, encoding)


# -- Keys --

def test_key_helpers_default_to_the_trajectories_prefix():
    assert blob_key("trj_1", SHA) == f"trajectories/trj_1/blobs/{SHA}"
    assert checkpoint_key("trj_1", SHA) == blob_key("trj_1", SHA)
    assert segment_key("trj_1", 1, 1000) == "trajectories/trj_1/segments/000000000001-000000001000.jsonl.zst"
    assert export_key("exp_1", SHA2) == f"trajectories/_exports/exp_1/{SHA2}.zip"
    assert trajectory_prefix("trj_1") == "trajectories/trj_1/"
    assert blob_key("trj_1", SHA).startswith(trajectory_prefix("trj_1"))


def test_oss_prefix_setting_applies_only_to_the_oss_provider(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_OSS_PREFIX", "/tenant/traces")
    assert trajectory_prefix("trj_1") == "trajectories/trj_1/"
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "oss")
    assert trajectory_prefix("trj_1") == "tenant/traces/trj_1/"
    assert export_key("exp_1", SHA) == f"tenant/traces/_exports/exp_1/{SHA}.zip"
    monkeypatch.setenv("TRAJECTORY_OSS_PREFIX", "  ")
    assert storage.key_prefix() == "trajectories/"
    monkeypatch.setenv("TRAJECTORY_OSS_PREFIX", "a/../b")
    with pytest.raises(ValueError):
        storage.key_prefix()


@pytest.mark.parametrize("call", [
    lambda: blob_key("", SHA),
    lambda: blob_key("trj/1", SHA),
    lambda: blob_key("..", SHA),
    lambda: blob_key("_exports", SHA),
    lambda: blob_key("trj\\1", SHA),
    lambda: blob_key("trj_1", "A" * 64),
    lambda: blob_key("trj_1", "abc"),
    lambda: trajectory_prefix(""),
    lambda: segment_key("trj_1", 0, 5),
    lambda: segment_key("trj_1", 6, 5),
    lambda: segment_key("trj_1", 1.0, 5),
    lambda: export_key("exp/1", SHA),
])
def test_key_helpers_reject_unsafe_parts(call):
    with pytest.raises((ValueError, TypeError)):
        call()


@pytest.mark.parametrize("key", [
    "", "/etc/passwd", "../escape", "a/../../escape", "a/..", "a..b", "a\\b", "C:\\blob", "C:blob",
    "a//b", "./a", "a/./b", "a/", "a\x00b",
])
def test_check_key_rejects_traversal_and_aliases(key):
    with pytest.raises(ValueError):
        check_key(key)


def test_check_key_prefix_rules():
    assert check_key("trajectories/trj_1/blobs/x") == "trajectories/trj_1/blobs/x"
    assert check_key("", prefix=True) == ""
    assert check_key("trajectories/", prefix=True) == "trajectories/"
    assert check_key("trajectories/trj_", prefix=True) == "trajectories/trj_"
    for bad in ("/", "a//", "../", "a/../"):
        with pytest.raises(ValueError):
            check_key(bad, prefix=True)


# -- LocalBlobStore --

async def test_local_store_round_trip_listing_and_permissions(tmp_path):
    root = tmp_path / "blobs"
    store = LocalBlobStore(root)
    assert isinstance(store, BlobStore)
    assert not root.exists()
    keys = [
        f"trajectories/trj_1/blobs/{SHA}",
        "trajectories/trj_1/segments/000000000001-000000000002.jsonl.zst",
        f"trajectories/trj_2/blobs/{SHA}",
    ]
    for index, key in enumerate(keys):
        await store.put(key, f"content {index}".encode(), content_type="application/octet-stream")
    assert [await store.get(key) for key in keys] == [b"content 0", b"content 1", b"content 2"]
    assert await store.exists(keys[0])
    assert not await store.exists("trajectories/trj_1/blobs/missing")
    with pytest.raises(FileNotFoundError):
        await store.get("trajectories/trj_1/blobs/missing")
    assert await keys_of(store, "trajectories/trj_1/") == keys[:2]
    assert await keys_of(store, "trajectories/trj_") == keys
    assert await keys_of(store, "") == keys
    assert await keys_of(store, "nothing/here/") == []
    if os.name == "posix":
        assert (root / keys[0]).stat().st_mode & 0o777 == 0o600


async def test_local_if_absent_keeps_the_first_write(tmp_path):
    store = LocalBlobStore(tmp_path)
    key = f"trajectories/trj_1/blobs/{SHA}"
    await store.put(key, b"first", content_type="application/json")
    await store.put(key, b"second", content_type="application/json")
    assert await store.get(key) == b"first"
    await store.put(key, b"third", content_type="application/json", if_absent=False)
    assert await store.get(key) == b"third"


async def test_local_delete_is_idempotent_and_prunes_empty_directories(tmp_path):
    root = tmp_path / "root"
    store = LocalBlobStore(root)
    key, sibling = f"trajectories/trj_1/blobs/{SHA}", f"trajectories/trj_2/blobs/{SHA}"
    await store.put(key, b"x", content_type="text/plain")
    await store.put(sibling, b"y", content_type="text/plain")
    await store.delete(key)
    await store.delete(key)
    assert not await store.exists(key)
    assert not (root / "trajectories" / "trj_1").exists()
    assert (root / "trajectories" / "trj_2" / "blobs").is_dir()
    await store.delete(sibling)
    assert root.is_dir() and list(root.iterdir()) == []


async def test_local_delete_prefix_counts_and_requires_a_directory_prefix(tmp_path):
    store = LocalBlobStore(tmp_path)
    for name in ("trj_1", "trj_10"):
        for index in range(3):
            await store.put(f"trajectories/{name}/blobs/{index:064x}", b"x", content_type="text/plain")
    assert await store.delete_prefix("trajectories/trj_1/") == 3
    assert len(await keys_of(store, "trajectories/")) == 3
    assert await store.delete_prefix("trajectories/missing/") == 0
    for bad in ("", "trajectories/trj_10", "../", "/etc/"):
        with pytest.raises(ValueError):
            await store.delete_prefix(bad)
    assert len(await keys_of(store, "trajectories/trj_10/")) == 3


async def test_local_directory_is_not_an_object(tmp_path):
    store = LocalBlobStore(tmp_path)
    await store.put("a/b", b"x", content_type="text/plain")
    with pytest.raises(FileNotFoundError):
        await store.get("a")
    assert not await store.exists("a")
    await store.delete("a")
    assert await store.get("a/b") == b"x"


@pytest.mark.parametrize("key", ["../escape", "a/../../escape", "/tmp/absolute", "a\\..\\b", "C:\\escape", ""])
async def test_local_store_refuses_keys_outside_its_root(tmp_path, key):
    store = LocalBlobStore(tmp_path / "root")
    with pytest.raises(ValueError):
        await store.put(key, b"x", content_type="text/plain")
    with pytest.raises(ValueError):
        await store.get(key)
    with pytest.raises(ValueError):
        await store.exists(key)
    with pytest.raises(ValueError):
        await store.delete(key)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("prefix", ["../", "/etc/", "a/../"])
async def test_local_listing_refuses_traversal_prefixes(tmp_path, prefix):
    store = LocalBlobStore(tmp_path)
    with pytest.raises(ValueError):
        await keys_of(store, prefix)
    with pytest.raises(ValueError):
        await store.delete_prefix(prefix)


async def test_local_write_renames_a_complete_temp_file_from_the_target_directory(tmp_path, monkeypatch):
    store = LocalBlobStore(tmp_path)
    key = "trajectories/trj_1/segments/000000000001-000000000002.jsonl.zst"
    target = tmp_path / key
    real_replace = os.replace
    observed = []

    def spy(source, destination):
        source, destination = Path(source), Path(destination)
        observed.append((
            source.parent == destination.parent, source.name.startswith(".tmp-"), source.read_bytes(),
            destination.read_bytes() if destination.exists() else None,
        ))
        real_replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", spy)
    await store.put(key, b"old segment", content_type="application/zstd", if_absent=False)
    await store.put(key, b"new segment", content_type="application/zstd", if_absent=False)
    monkeypatch.setattr(storage.os, "replace", real_replace)
    assert observed == [(True, True, b"old segment", None), (True, True, b"new segment", b"old segment")]
    assert target.read_bytes() == b"new segment"
    assert [path.name for path in target.parent.iterdir()] == [target.name]


async def test_local_failed_write_keeps_the_previous_object_and_no_temp_file(tmp_path, monkeypatch):
    store = LocalBlobStore(tmp_path)
    key, fresh = f"trajectories/trj_1/blobs/{SHA}", f"trajectories/trj_1/blobs/{SHA2}"
    await store.put(key, b"committed", content_type="text/plain")
    real_fsync = os.fsync

    def disk_full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage.os, "fsync", disk_full)
    with pytest.raises(OSError):
        await store.put(key, b"replacement that never lands", content_type="text/plain", if_absent=False)
    with pytest.raises(OSError):
        await store.put(fresh, b"new", content_type="text/plain")
    monkeypatch.setattr(storage.os, "fsync", real_fsync)
    assert await store.get(key) == b"committed"
    assert not await store.exists(fresh)
    assert [path.name for path in (tmp_path / key).parent.iterdir()] == [SHA]


async def test_local_concurrent_overwrites_never_expose_partial_objects(tmp_path):
    store = LocalBlobStore(tmp_path)
    key = "trajectories/trj_1/segments/000000000001-000000001000.jsonl.zst"
    versions = [bytes([index]) * (256 * 1024) for index in range(8)]
    await store.put(key, versions[0], content_type="application/zstd")
    seen = []

    async def writer(version):
        await store.put(key, version, content_type="application/zstd", if_absent=False)

    async def reader():
        for _ in range(25):
            seen.append(await store.get(key))

    await asyncio.gather(*(writer(version) for version in versions * 3), *(reader() for _ in range(4)))
    assert len(seen) == 100 and all(data in versions for data in seen)
    assert await keys_of(store, "trajectories/") == [key]
    assert [path.name for path in (tmp_path / key).parent.iterdir()] == [Path(key).name]


async def test_local_listing_skips_in_flight_temp_files(tmp_path):
    store = LocalBlobStore(tmp_path)
    key = f"trajectories/trj_1/blobs/{SHA}"
    await store.put(key, b"x", content_type="text/plain")
    (tmp_path / "trajectories" / "trj_1" / "blobs" / ".tmp-abc123").write_bytes(b"partial")
    assert await keys_of(store, "trajectories/") == [key]


async def test_local_delete_prefix_removes_temp_files_of_interrupted_writes(tmp_path):
    # A process killed between the temp write and the rename leaves the bytes
    # in a temp file. Deleting the trajectory must not leave them behind.
    root = tmp_path / "root"
    store = LocalBlobStore(root)
    key, other = f"trajectories/trj_1/blobs/{SHA}", f"trajectories/trj_2/blobs/{SHA}"
    await store.put(key, b"x", content_type="text/plain")
    await store.put(other, b"y", content_type="text/plain")
    orphans = [
        root / "trajectories" / "trj_1" / "blobs" / ".tmp-crashed",
        root / "trajectories" / "trj_1" / "segments" / ".tmp-killed",
    ]
    orphans[1].parent.mkdir(parents=True)
    for orphan in orphans:
        orphan.write_bytes(b"content of a write that never renamed")
    unrelated = root / "trajectories" / "trj_2" / "blobs" / ".tmp-in-flight"
    unrelated.write_bytes(b"another trajectory")

    assert await keys_of(store, "trajectories/trj_1/") == [key]
    assert await store.delete_prefix("trajectories/trj_1/") == 1
    assert not (root / "trajectories" / "trj_1").exists()
    assert unrelated.exists() and await store.get(other) == b"y"
    assert await store.delete_prefix("trajectories/trj_2/") == 1
    assert root.is_dir() and list(root.iterdir()) == []


@pytest.mark.parametrize("wrap", [bytearray, memoryview])
async def test_every_store_accepts_bytes_like_data_and_rejects_the_rest(tmp_path, wrap):
    key, other = blob_key("trj_1", SHA), blob_key("trj_1", SHA2)
    bucket = FakeOssBucket()
    for store in (LocalBlobStore(tmp_path), MemoryBlobStore(), oss_store(bucket)):
        source = bytearray(b"payload")
        await store.put(key, wrap(source), content_type="application/octet-stream", if_absent=False)
        source[:] = b"mutated"
        assert await store.get(key) == b"payload"
        for data in (7, "text"):
            with pytest.raises(TypeError):
                await store.put(other, data, content_type="application/octet-stream")
        assert not await store.exists(other)
    assert bucket.objects == {key: b"payload"}


# -- MemoryBlobStore --

async def test_memory_store_counters_and_semantics():
    store = MemoryBlobStore()
    assert isinstance(store, BlobStore)
    await store.put("t/a", b"12345", content_type="application/json")
    await store.put("t/a", b"ignored", content_type="application/json")
    await store.put("t/b", b"xyz", content_type="text/plain", if_absent=False)
    assert (store.puts, store.bytes) == (3, 8)
    assert store.objects == {"t/a": b"12345", "t/b": b"xyz"}
    assert store.content_types == {"t/a": "application/json", "t/b": "text/plain"}
    assert await store.get("t/a") == b"12345"
    with pytest.raises(FileNotFoundError):
        await store.get("t/missing")
    assert store.gets == 2
    assert await store.exists("t/b") and not await store.exists("t/c")
    assert await keys_of(store, "t/") == ["t/a", "t/b"]
    await store.delete("t/a")
    await store.delete("t/a")
    await store.put("u/1", b"1", content_type="text/plain")
    await store.put("u/2", b"2", content_type="text/plain")
    assert await store.delete_prefix("u/") == 2
    assert store.deletes == 4
    assert store.objects == {"t/b": b"xyz"}
    with pytest.raises(ValueError):
        await store.put("../x", b"", content_type="text/plain")
    with pytest.raises(ValueError):
        await store.delete_prefix("")


async def test_memory_store_fault_hooks():
    store = MemoryBlobStore()
    store.fail("put", times=2)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await store.put("t/a", b"x", content_type="text/plain")
    assert (store.puts, store.objects) == (0, {})
    await store.put("t/a", b"x", content_type="text/plain")
    assert store.puts == 1

    store.faults["get"] = RuntimeError("get outage")
    for _ in range(3):
        with pytest.raises(RuntimeError, match="get outage"):
            await store.get("t/a")
    assert store.gets == 0

    seen = []

    async def guarded_delete(key):
        await asyncio.sleep(0)
        seen.append(key)
        if key.endswith("locked"):
            raise PermissionError(key)

    store.faults["delete"] = guarded_delete
    await store.delete("t/a")
    with pytest.raises(PermissionError):
        await store.delete("t/locked")
    assert seen == ["t/a", "t/locked"] and store.deletes == 1

    store.fail("list", OSError("listing down"))
    with pytest.raises(OSError, match="listing down"):
        await keys_of(store, "t/")
    store.clear_faults()
    assert await keys_of(store, "t/") == []
    with pytest.raises(ValueError):
        store.fail("rename")


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_memory_store_get_hook_runs_between_read_and_return(asynchronous):
    store = MemoryBlobStore()
    await store.put("t/a", b"authoritative", content_type="text/plain")
    calls = []

    def revoke(key):
        calls.append(key)
        store.objects.pop(key)

    async def revoke_later(key):
        await asyncio.sleep(0)
        revoke(key)

    store.get_hook = revoke_later if asynchronous else revoke
    assert await store.get("t/a") == b"authoritative"
    assert calls == ["t/a"]
    with pytest.raises(FileNotFoundError):
        await store.get("t/a")
    assert calls == ["t/a"]


# -- FaultInjectingBlobStore --

def test_parse_blob_faults():
    assert parse_blob_faults("put:0.3, get:1.0,delete:0.1") == {"put": 0.3, "get": 1.0, "delete": 0.1}
    assert parse_blob_faults("PUT:1,delete_prefix:0,list:0.5") == {"put": 1.0, "delete_prefix": 0.0, "list": 0.5}
    assert parse_blob_faults("bogus:0.5,put:2,get,exists:nan,delete:-0.1,,list:x") == {}
    assert parse_blob_faults({"get": 0.25, "copy": 1.0}) == {"get": 0.25}
    assert parse_blob_faults("") == {}


async def put_outcomes(store, count: int = 200) -> list[bool]:
    outcomes = []
    for index in range(count):
        try:
            await store.put(f"t/{index}", b"x", content_type="text/plain")
            outcomes.append(True)
        except BlobFaultInjected:
            outcomes.append(False)
    return outcomes


async def test_fault_injection_is_reproducible_for_a_seed():
    first = await put_outcomes(FaultInjectingBlobStore(MemoryBlobStore(), "put:0.3", seed=42))
    again = await put_outcomes(FaultInjectingBlobStore(MemoryBlobStore(), "put:0.3", seed=42))
    other = await put_outcomes(FaultInjectingBlobStore(MemoryBlobStore(), "put:0.3", seed=7))
    assert first == again
    assert first != other
    assert 0.15 < first.count(False) / len(first) < 0.45


async def test_fault_injection_hits_only_configured_operations():
    inner = MemoryBlobStore()
    store = FaultInjectingBlobStore(inner, "put:1.0,get:1.0,list:1.0,delete_prefix:1.0,delete:0", seed=1)
    assert isinstance(store, BlobStore)
    with pytest.raises(BlobFaultInjected):
        await store.put("t/a", b"x", content_type="text/plain")
    assert inner.objects == {} and inner.puts == 0
    await inner.put("t/a", b"x", content_type="text/plain")
    with pytest.raises(BlobFaultInjected) as caught:
        await store.get("t/a")
    assert isinstance(caught.value, ConnectionError) and not isinstance(caught.value, FileNotFoundError)
    assert await store.exists("t/a")
    with pytest.raises(BlobFaultInjected):
        await keys_of(store, "t/")
    with pytest.raises(BlobFaultInjected):
        await store.delete_prefix("t/")
    await store.delete("t/a")
    assert inner.objects == {}
    assert store.injected == {"put": 1, "get": 1, "exists": 0, "delete": 0, "delete_prefix": 1, "list": 1}


# -- Configuration --

def asset_config(**overrides):
    values = {"oss_bucket": "assets", "oss_region": "cn-shanghai", "oss_endpoint": "assets.custom.example"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_store_is_local_under_the_backend_openbox_directory():
    backend = Path(storage.__file__).resolve().parents[1]
    store = get_blob_store()
    assert isinstance(store, LocalBlobStore)
    assert store.root == backend / ".openbox" / "trajectory-blobs"
    assert get_blob_store() is store


def test_local_path_setting_and_installed_stores(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTORY_BLOB_LOCAL_PATH", str(tmp_path / "blobs"))
    configured = get_blob_store()
    assert isinstance(configured, LocalBlobStore) and configured.root == tmp_path / "blobs"
    memory = MemoryBlobStore()
    set_blob_store(memory)
    assert get_blob_store() is memory
    set_blob_store(None)
    rebuilt = get_blob_store()
    assert isinstance(rebuilt, LocalBlobStore) and rebuilt is not configured


def test_oss_store_defaults_to_the_asset_bucket(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: asset_config())
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "oss")
    store = get_blob_store()
    assert isinstance(store, OssBlobStore)
    # The endpoint is derived from the region, not copied from OSS_ENDPOINT.
    assert (store.bucket, store.region, store.endpoint, store.internal) == (
        "assets", "cn-shanghai", "oss-cn-shanghai.aliyuncs.com", True
    )


def test_oss_store_settings_override_the_asset_bucket(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: asset_config())
    for name, value in {
        "TRAJECTORY_BLOB_PROVIDER": "OSS", "TRAJECTORY_OSS_BUCKET": "traces", "TRAJECTORY_OSS_REGION": "cn-beijing",
        "TRAJECTORY_OSS_ENDPOINT": "oss-cn-beijing-internal.aliyuncs.com", "TRAJECTORY_OSS_INTERNAL": "false",
    }.items():
        monkeypatch.setenv(name, value)
    store = get_blob_store()
    assert (store.bucket, store.region, store.endpoint, store.internal) == (
        "traces", "cn-beijing", "oss-cn-beijing-internal.aliyuncs.com", False
    )
    set_blob_store(None)
    monkeypatch.setenv("TRAJECTORY_OSS_INTERNAL", "sometimes")
    assert get_blob_store().internal is True


def test_unusable_provider_settings_raise(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: asset_config(oss_bucket=""))
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "oss")
    with pytest.raises(RuntimeError, match="OSS_BUCKET"):
        get_blob_store()
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "s3")
    with pytest.raises(RuntimeError, match="s3"):
        get_blob_store()


async def test_blob_fault_setting_wraps_the_configured_store(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTORY_BLOB_LOCAL_PATH", str(tmp_path))
    monkeypatch.setenv("TRAJECTORY_BLOB_FAULT", "put:1.0")
    store = get_blob_store()
    assert isinstance(store, FaultInjectingBlobStore) and isinstance(store.inner, LocalBlobStore)
    with pytest.raises(BlobFaultInjected):
        await store.put(blob_key("trj_1", SHA), b"x", content_type="text/plain")
    assert list(tmp_path.iterdir()) == []
    memory = MemoryBlobStore()
    set_blob_store(memory)
    assert get_blob_store() is memory


# -- OssBlobStore --

class FakeOssBucket:
    """A small OSS server for MockTransport: objects, forbid-overwrite, batch delete, ListObjectsV2."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = unquote(request.url.raw_path.decode().split("?", 1)[0])[1:]
        query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        if request.method == "PUT":
            if request.headers.get("x-oss-forbid-overwrite") == "true" and key in self.objects:
                return httpx.Response(409, content=b"<Error><Code>FileAlreadyExists</Code><Message>The object you specified already exists and can not be overwritten.</Message></Error>")
            self.objects[key] = request.content
            return httpx.Response(200, headers={"ETag": '"E"'})
        if request.method in ("GET", "HEAD") and key:
            if key not in self.objects:
                return httpx.Response(404, content=b"" if request.method == "HEAD" else b"<Error><Code>NoSuchKey</Code></Error>")
            body = self.objects[key]
            return httpx.Response(200, content=b"" if request.method == "HEAD" else body, headers={"Content-Length": str(len(body))})
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        if request.method == "POST" and "delete" in query:
            for node in ElementTree.fromstring(request.content).findall("Object"):
                self.objects.pop(node.findtext("Key"), None)
            return httpx.Response(200)
        if request.method == "GET" and query.get("list-type") == "2":
            matching = sorted(
                name for name in self.objects
                if name.startswith(query.get("prefix", "")) and name > query.get("continuation-token", "")
            )
            size = int(query["max-keys"])
            page, rest = matching[:size], matching[size:]
            contents = "".join(f"<Contents><Key>{quote(name)}</Key><Size>{len(self.objects[name])}</Size></Contents>" for name in page)
            token = f"<NextContinuationToken>{quote(page[-1], safe='')}</NextContinuationToken>" if rest else ""
            body = (f"<ListBucketResult><EncodingType>url</EncodingType><IsTruncated>{'true' if rest else 'false'}"
                    f"</IsTruncated>{token}{contents}</ListBucketResult>")
            return httpx.Response(200, content=body.encode())
        return httpx.Response(400)


def oss_store(bucket: FakeOssBucket, **kwargs) -> OssBlobStore:
    kwargs.setdefault("credentials", lambda: {"access_key_id": "ak", "access_key_secret": "sk"})
    return OssBlobStore("bucket", "cn-shanghai", http=httpx.AsyncClient(transport=httpx.MockTransport(bucket)), **kwargs)


async def test_oss_blob_store_object_operations():
    bucket = FakeOssBucket()
    store = oss_store(bucket)
    assert isinstance(store, BlobStore)
    key, missing = blob_key("trj_1", SHA), blob_key("trj_1", SHA2)
    await store.put(key, b"first", content_type="application/json")
    await store.put(key, b"second", content_type="application/json")
    assert await store.get(key) == b"first"
    await store.put(key, b"third", content_type="application/json", if_absent=False)
    assert await store.get(key) == b"third"
    assert await store.exists(key) and not await store.exists(missing)
    with pytest.raises(FileNotFoundError):
        await store.get(missing)
    await store.delete(key)
    await store.delete(key)
    assert bucket.objects == {}
    puts = [request for request in bucket.requests if request.method == "PUT"]
    assert [request.headers.get("x-oss-forbid-overwrite") for request in puts] == ["true", "true", None]
    assert {request.url.host for request in bucket.requests} == {"bucket.oss-cn-shanghai-internal.aliyuncs.com"}
    count = len(bucket.requests)
    with pytest.raises(ValueError):
        await store.get("../escape")
    with pytest.raises(ValueError):
        await store.delete_prefix("")
    assert len(bucket.requests) == count


async def test_oss_blob_store_lists_and_deletes_prefixes_across_pages():
    bucket = FakeOssBucket()
    for index in range(2500):
        bucket.objects[f"trajectories/trj_1/blobs/{index:064x}"] = b"x"
    bucket.objects[f"trajectories/trj_10/blobs/{SHA}"] = b"keep"
    store = oss_store(bucket, internal=False)
    listed = await keys_of(store, "trajectories/trj_1/")
    assert len(listed) == 2500 and listed == sorted(name for name in bucket.objects if name.startswith("trajectories/trj_1/"))
    assert [request.method for request in bucket.requests] == ["GET"] * 3
    bucket.requests.clear()
    assert await store.delete_prefix("trajectories/trj_1/") == 2500
    assert list(bucket.objects) == [f"trajectories/trj_10/blobs/{SHA}"]
    assert [request.method for request in bucket.requests] == ["GET", "POST"] * 3
    assert {request.url.host for request in bucket.requests} == {"bucket.oss-cn-shanghai.aliyuncs.com"}


async def test_oss_blob_store_signs_each_listing_page_with_current_credentials():
    bucket = FakeOssBucket()
    for index in range(2500):
        bucket.objects[f"trajectories/trj_1/blobs/{index:064x}"] = b"x"
    loads = []

    def credentials():
        loads.append(True)
        return {"access_key_id": f"ak-{len(loads)}", "access_key_secret": "sk"}

    store = oss_store(bucket, credentials=credentials)
    assert len(await keys_of(store, "trajectories/trj_1/")) == 2500
    assert [request.headers["authorization"].split(":")[0] for request in bucket.requests] == [
        "OSS ak-1", "OSS ak-2", "OSS ak-3",
    ]
    bucket.requests.clear()
    assert await store.delete_prefix("trajectories/trj_1/") == 2500
    assert [(request.method, request.headers["authorization"].split(":")[0]) for request in bucket.requests] == [
        ("GET", "OSS ak-4"), ("POST", "OSS ak-4"), ("GET", "OSS ak-5"), ("POST", "OSS ak-5"),
        ("GET", "OSS ak-6"), ("POST", "OSS ak-6"),
    ]


async def test_oss_blob_store_reports_a_missing_bucket_instead_of_missing_objects():
    missing = b"<Error><Code>NoSuchBucket</Code><Message>The specified bucket does not exist.</Message></Error>"

    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(404, headers={"x-oss-err": base64.b64encode(missing).decode()})
        return httpx.Response(404, content=missing)

    store = OssBlobStore(
        "renamed-bucket", "cn-shanghai", credentials=lambda: {"access_key_id": "ak", "access_key_secret": "sk"},
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    key = blob_key("trj_1", SHA)
    for call in (
        lambda: store.get(key),
        lambda: store.exists(key),
        lambda: store.delete(key),
        lambda: store.put(key, b"x", content_type="text/plain"),
        lambda: store.delete_prefix("trajectories/trj_1/"),
        lambda: keys_of(store, "trajectories/"),
    ):
        with pytest.raises(oss.OssError) as caught:
            await call()
        assert (caught.value.status, caught.value.code) == (404, "NoSuchBucket")


async def test_oss_blob_store_signs_with_credentials_refreshed_every_ten_minutes(monkeypatch):
    bucket = FakeOssBucket()
    now = [0.0]
    loads = []

    def load():
        loads.append(True)
        return {"access_key_id": f"ak-{len(loads)}", "access_key_secret": "sk", "security_token": f"sts-{len(loads)}"}

    oss.clear_credentials_cache()
    monkeypatch.setattr(oss, "_clock", lambda: now[0])
    monkeypatch.setattr(oss, "load_credentials", load)
    try:
        store = OssBlobStore("bucket", "cn-shanghai", http=httpx.AsyncClient(transport=httpx.MockTransport(bucket)))
        await store.exists(blob_key("trj_1", SHA))
        await store.exists(blob_key("trj_1", SHA))
        now[0] += 600
        await store.exists(blob_key("trj_1", SHA))
    finally:
        oss.clear_credentials_cache()
    assert [request.headers["authorization"].split(":")[0] for request in bucket.requests] == ["OSS ak-1", "OSS ak-1", "OSS ak-2"]
    assert [request.headers["x-oss-security-token"] for request in bucket.requests] == ["sts-1", "sts-1", "sts-2"]


# -- put_file --

async def test_local_put_file_copies_in_chunks_into_a_renamed_temp_file(tmp_path, monkeypatch):
    root, source = tmp_path / "root", tmp_path / "archive.zip"
    store = LocalBlobStore(root)
    content = os.urandom(storage.CHUNK_BYTES * 2 + 17)
    source.write_bytes(content)
    key = export_key("exp_1", SHA)
    lengths, real_copy = [], storage.shutil.copyfileobj

    def copy(reader, writer, length):
        lengths.append(length)
        real_copy(reader, writer, length)

    monkeypatch.setattr(storage.shutil, "copyfileobj", copy)
    await store.put_file(key, source, content_type="application/zip", if_absent=False)
    assert await store.get(key) == content and lengths == [storage.CHUNK_BYTES]
    source.write_bytes(b"replacement")
    await store.put_file(key, str(source), content_type="application/zip")
    assert await store.get(key) == content  # if_absent keeps the first object
    await store.put_file(key, source, content_type="application/zip", if_absent=False)
    assert await store.get(key) == b"replacement"
    with pytest.raises(FileNotFoundError):
        await store.put_file(blob_key("trj_1", SHA), tmp_path / "missing.zip", content_type="application/zip")
    assert await keys_of(store, "") == [key] and list(root.rglob(".tmp-*")) == []
    with pytest.raises(ValueError):
        await store.put_file("../escape", source, content_type="application/zip")


async def test_memory_put_file_is_a_counted_put_of_the_file_bytes(tmp_path):
    store = MemoryBlobStore()
    source = tmp_path / "archive.zip"
    source.write_bytes(b"zip bytes")
    await store.put_file("t/a", source, content_type="application/zip")
    await store.put_file("t/a", str(source), content_type="text/plain")
    assert (store.objects, store.content_types, store.puts, store.bytes) == (
        {"t/a": b"zip bytes"}, {"t/a": "application/zip"}, 2, 9)
    store.fail("put", times=1)
    with pytest.raises(ConnectionError):
        await store.put_file("t/b", source, content_type="application/zip")
    assert "t/b" not in store.objects
    with pytest.raises(ValueError):
        await store.put_file("../x", source, content_type="application/zip")


async def test_wrappers_delegate_put_file(tmp_path):
    from trajectory.worker.services import GuardedGcBlobStore

    source = tmp_path / "archive.zip"
    source.write_bytes(b"zip")
    inner = MemoryBlobStore()
    faulty = FaultInjectingBlobStore(inner, "put:1.0", seed=1)
    with pytest.raises(BlobFaultInjected):
        await faulty.put_file("t/a", source, content_type="application/zip")
    assert inner.objects == {} and faulty.injected["put"] == 1
    faulty.probabilities = {}
    await GuardedGcBlobStore(faulty, guard=None).put_file("t/a", source, content_type="application/zip")
    assert inner.objects == {"t/a": b"zip"}


async def test_oss_blob_store_put_file_streams_the_file_with_its_md5(tmp_path):
    bucket = FakeOssBucket()
    store = oss_store(bucket)
    source = tmp_path / "archive.zip"
    content = os.urandom(storage.CHUNK_BYTES + 5)
    source.write_bytes(content)
    key = export_key("exp_1", SHA)
    await store.put_file(key, source, content_type="application/zip", if_absent=False)
    await store.put_file(key, source, content_type="application/zip")
    assert bucket.objects == {key: content}
    first, second = [request for request in bucket.requests if request.method == "PUT"]
    assert first.headers["content-md5"] == base64.b64encode(hashlib.md5(content).digest()).decode()
    assert first.headers["content-length"] == str(len(content)) and "transfer-encoding" not in first.headers
    assert (first.headers.get("x-oss-forbid-overwrite"), second.headers["x-oss-forbid-overwrite"]) == (None, "true")
    assert first.url.host == "bucket.oss-cn-shanghai-internal.aliyuncs.com"
