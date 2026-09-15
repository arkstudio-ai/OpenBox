"""Trajectory blob storage: bytes kept outside the trace database.

Blobs, cold event segments, checkpoint pages and exports live in object
storage — Aliyun OSS in production (TRAJECTORY_BLOB_PROVIDER=oss), a local
directory on desktops and in dev (local, the default) — behind the small
BlobStore protocol. Stores move opaque bytes under relative, slash-separated
keys built by the helpers below; callers compress with encode_blob and keep
the returned encoding next to the key (payload and segment rows).

Only the trajectory worker reads or writes blobs; business request paths
never do.
"""
from __future__ import annotations

import asyncio
import inspect
import operator
import os
import random
import re
import shutil
import tempfile
from pathlib import Path, PureWindowsPath
from typing import AsyncIterator, Callable, Mapping, Protocol, runtime_checkable

import zstandard

from core.log import create_logger
from core.oss import OssClient, cached_credentials
from trajectory.types import CorruptContent

log = create_logger("trajectory.storage")

#: Operation names used by fault hooks and TRAJECTORY_BLOB_FAULT.
BLOB_OPERATIONS = ("put", "get", "exists", "delete", "delete_prefix", "list")
#: Namespace of every key: TRAJECTORY_OSS_PREFIX on OSS, this relative
#: directory in a local store.
DEFAULT_KEY_PREFIX = "trajectories/"
DEFAULT_LOCAL_PATH = Path(__file__).resolve().parents[1] / ".openbox" / "trajectory-blobs"
COMPRESSIBLE_TYPES = frozenset({
    "application/json", "application/x-ndjson", "text/*", "application/xml", "application/javascript",
})
#: Below this size compression saves little and a frame header costs bytes.
COMPRESSION_MIN_BYTES = 1024
ZSTD_LEVEL = 3
#: Chunk size of streamed reads (get_chunks, read_chunks).
CHUNK_BYTES = 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Name prefix of in-flight LocalBlobStore writes; never listed as keys.
_TEMP_PREFIX = ".tmp-"


class BlobFaultInjected(ConnectionError):
    """An injected failure: looks like an unreachable store, never like a missing key."""


@runtime_checkable
class BlobStore(Protocol):
    """Opaque bytes under keys accepted by check_key.

    Every store behaves the same: put with if_absent keeps an existing object,
    put_file stores a local file's bytes as put stores data (the OSS and local
    stores stream them, so memory does not grow with the file), get raises
    FileNotFoundError for a missing key, delete is idempotent, delete_prefix
    returns how many keys it removed and list yields keys in lexical order.
    """

    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None: ...

    async def put_file(self, key: str, path: str | os.PathLike[str], *, content_type: str,
                       if_absent: bool = True) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    def list(self, prefix: str) -> AsyncIterator[str]: ...


async def read_chunks(store: BlobStore, key: str, *, chunk_bytes: int = CHUNK_BYTES) -> AsyncIterator[bytes]:
    """An object in chunks of at most chunk_bytes, streamed by the stores that offer ``get_chunks``.

    Other stores (MemoryBlobStore, wrappers of it) are read whole with ``get``
    and sliced. A missing key raises FileNotFoundError, as ``get`` does.
    """
    get_chunks = getattr(store, "get_chunks", None)
    if get_chunks is None:
        view = memoryview(await store.get(key))
        for offset in range(0, len(view), chunk_bytes):
            yield view[offset:offset + chunk_bytes]
        return
    async for chunk in get_chunks(key, chunk_bytes=chunk_bytes):
        yield chunk


# -- Encodings --

def is_compressible(media_type: str) -> bool:
    """Membership in COMPRESSIBLE_TYPES, ignoring case and parameters such as charset."""
    base = (media_type or "").split(";", 1)[0].strip().lower()
    major, slash, _ = base.partition("/")
    return base in COMPRESSIBLE_TYPES or (bool(slash) and f"{major}/*" in COMPRESSIBLE_TYPES)


def encode_blob(content: bytes, media_type: str) -> tuple[bytes, str]:
    """(stored bytes, encoding): zstd level 3 for compressible types of at
    least COMPRESSION_MIN_BYTES, identity otherwise (media is already compressed)."""
    if len(content) >= COMPRESSION_MIN_BYTES and is_compressible(media_type):
        return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(content), "zstd"
    return bytes(content), "identity"


def decode_blob(stored: bytes, encoding: str) -> bytes:
    """Inverse of encode_blob; CorruptContent when the bytes cannot be decoded.

    zstd input is decoded frame by frame: zstandard's one-shot decompress keeps
    only the first of concatenated frames and its stream reader returns b""
    for a truncated frame, so either would turn damage into short content.
    """
    if encoding == "identity":
        return bytes(stored)
    if encoding != "zstd":
        raise CorruptContent(f"Unsupported blob encoding: {encoding!r}")
    if not stored:
        raise CorruptContent("Empty zstd blob")
    chunks, rest = [], bytes(stored)
    try:
        while rest:
            frame = zstandard.ZstdDecompressor().decompressobj()
            chunks.append(frame.decompress(rest))
            if not frame.eof:
                raise CorruptContent("Truncated zstd blob")
            rest = frame.unused_data
    except zstandard.ZstdError as exc:
        raise CorruptContent("Invalid zstd blob") from exc
    return b"".join(chunks)


# -- Keys --

def check_key(key: str, *, prefix: bool = False) -> str:
    """Return key when it is a safe relative blob key (prefix=True: a listing prefix).

    Rejected: "..", backslashes, NUL, a leading "/" or drive letter, and empty
    or "." segments. That keeps LocalBlobStore inside its root and gives every
    store the same key space (no two spellings of one file).
    """
    if (not isinstance(key, str) or ".." in key or "\\" in key or "\x00" in key
            or key.startswith("/") or PureWindowsPath(key).drive):
        raise ValueError(f"Invalid blob key: {key!r}")
    body = key[:-1] if prefix and key.endswith("/") else key
    if (body or not prefix) and any(part in ("", ".") for part in body.split("/")):
        raise ValueError(f"Invalid blob key: {key!r}")
    return key


def _as_bytes(data) -> bytes:
    # Every store takes any bytes-like object (the OSS client does too) and
    # rejects the rest; bytes(5) would silently store five zero bytes.
    return data if isinstance(data, bytes) else bytes(memoryview(data))


def _check_delete_prefix(prefix: str) -> str:
    # A prefix delete must name a whole directory: "" would empty the store
    # (on OSS possibly a bucket shared with user assets) and "trj_1" would
    # also match "trj_10".
    check_key(prefix, prefix=True)
    if not prefix.endswith("/"):
        raise ValueError(f"delete_prefix needs a non-empty prefix ending in '/': {prefix!r}")
    return prefix


def key_prefix() -> str:
    """The namespace every trajectory key starts with (always ends in "/")."""
    if _provider() != "oss":
        return DEFAULT_KEY_PREFIX
    value = (os.getenv("TRAJECTORY_OSS_PREFIX") or "").strip().strip("/")
    return check_key(f"{value}/", prefix=True) if value else DEFAULT_KEY_PREFIX


def _name(kind: str, value: str, *, reserved_underscore: bool = False) -> str:
    if (not isinstance(value, str) or not value or "/" in value
            or (reserved_underscore and value.startswith("_"))):
        raise ValueError(f"Invalid {kind} for a blob key: {value!r}")
    return check_key(value)


def _trajectory(trajectory_id: str) -> str:
    # "_exports" (and any "_" name) is a reserved sibling of trajectory directories.
    return _name("trajectory_id", trajectory_id, reserved_underscore=True)


def _sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"Invalid sha256 for a blob key: {value!r}")
    return value


def blob_key(trajectory_id: str, sha256: str) -> str:
    """Content-addressed bytes of one trajectory (payloads, $ref values, checkpoint pages)."""
    return f"{key_prefix()}{_trajectory(trajectory_id)}/blobs/{_sha256(sha256)}"


def segment_key(trajectory_id: str, from_seq: int, to_seq: int) -> str:
    """Archived events from_seq..to_seq (inclusive) as zstd JSONL; the range names the object."""
    first, last = operator.index(from_seq), operator.index(to_seq)
    if not 1 <= first <= last:
        raise ValueError(f"Invalid segment range: {from_seq}-{to_seq}")
    return f"{key_prefix()}{_trajectory(trajectory_id)}/segments/{first:012d}-{last:012d}.jsonl.zst"


def checkpoint_key(trajectory_id: str, sha256: str) -> str:
    """Checkpoint pages are content addressed, so they share blob_key."""
    return blob_key(trajectory_id, sha256)


def export_key(export_id: str, sha256: str) -> str:
    return f"{key_prefix()}_exports/{_name('export_id', export_id)}/{_sha256(sha256)}.zip"


def trajectory_prefix(trajectory_id: str) -> str:
    """Everything stored for one trajectory; retention deletes it as one prefix."""
    return f"{key_prefix()}{_trajectory(trajectory_id)}/"


# -- Stores --

class OssBlobStore:
    """Blobs in an OSS bucket through the core.oss server-side operations.

    Each call signs with core.oss.cached_credentials() (re-read every 10
    minutes) unless a credentials callable is injected; internal=True sends
    traffic to the region's intranet host when the endpoint is Aliyun's.
    """

    def __init__(
        self,
        bucket: str,
        region: str,
        endpoint: str = "",
        *,
        internal: bool = True,
        credentials: Callable[[], dict] | None = None,
        http=None,
    ):
        if not bucket:
            raise ValueError("OssBlobStore needs a bucket")
        self.bucket = bucket
        self.region = region
        self.endpoint = endpoint or f"oss-{region}.aliyuncs.com"
        self.internal = internal
        self._credentials = credentials
        self._http = http

    @classmethod
    def from_env(cls) -> OssBlobStore:
        """TRAJECTORY_OSS_* settings, falling back to the asset bucket (OSS_BUCKET, OSS_REGION)."""
        from core.config import get_config

        config = get_config()
        bucket = (os.getenv("TRAJECTORY_OSS_BUCKET") or config.oss_bucket or "").strip()
        if not bucket:
            raise RuntimeError("Trajectory OSS blob storage needs TRAJECTORY_OSS_BUCKET or OSS_BUCKET")
        region = (os.getenv("TRAJECTORY_OSS_REGION") or config.oss_region or "").strip()
        endpoint = (os.getenv("TRAJECTORY_OSS_ENDPOINT") or "").strip() or f"oss-{region}.aliyuncs.com"
        return cls(bucket, region, endpoint, internal=_flag("TRAJECTORY_OSS_INTERNAL", True))

    def _client(self) -> OssClient:
        creds = (self._credentials or cached_credentials)()
        return OssClient(
            self.bucket, self.region, self.endpoint, creds["access_key_id"], creds["access_key_secret"],
            security_token=creds.get("security_token"), http=self._http,
        )

    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None:
        # x-oss-forbid-overwrite makes if_absent atomic on the server.
        await self._client().put_object(
            check_key(key), data, content_type=content_type, forbid_overwrite=if_absent, internal=self.internal
        )

    async def put_file(self, key: str, path: str | os.PathLike[str], *, content_type: str,
                       if_absent: bool = True) -> None:
        await self._client().put_object_file(
            check_key(key), path, content_type=content_type, forbid_overwrite=if_absent, internal=self.internal
        )

    async def get(self, key: str) -> bytes:
        return await self._client().get_object(check_key(key), internal=self.internal)

    async def get_chunks(self, key: str, *, chunk_bytes: int = CHUNK_BYTES) -> AsyncIterator[bytes]:
        async for chunk in self._client().get_object_chunks(check_key(key), internal=self.internal,
                                                             chunk_bytes=chunk_bytes):
            yield chunk

    async def exists(self, key: str) -> bool:
        return await self._client().head_object_info(check_key(key), internal=self.internal) is not None

    async def delete(self, key: str) -> None:
        await self._client().delete_object_key(check_key(key), internal=self.internal)

    async def delete_prefix(self, prefix: str) -> int:
        _check_delete_prefix(prefix)
        # Page by page: a continuation token names the last listed key, so
        # deleting a page does not disturb the next one. Each page signs with
        # a fresh client, so a long run picks up refreshed credentials.
        token, deleted = None, 0
        while True:
            client = self._client()
            objects, token = await client.list_objects(prefix, continuation_token=token, internal=self.internal)
            if objects:
                deleted += await client.delete_objects([item["key"] for item in objects], internal=self.internal)
            if token is None:
                return deleted

    async def list(self, prefix: str) -> AsyncIterator[str]:
        check_key(prefix, prefix=True)
        token = None
        while True:
            # The consumer may take long between pages: sign each page anew.
            objects, token = await self._client().list_objects(prefix, continuation_token=token, internal=self.internal)
            for item in objects:
                yield item["key"]
            if token is None:
                return


class LocalBlobStore:
    """Blobs as files under root (desktop and dev).

    A write goes to a temp file in the target directory, is fsynced and then
    renamed over the key, so readers see the old or the new object, never a
    partial one. Blocking file I/O runs in worker threads.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None:
        path, content = self._path(key), _as_bytes(data)
        await asyncio.to_thread(self._write, path, if_absent, lambda handle: handle.write(content))

    async def put_file(self, key: str, path: str | os.PathLike[str], *, content_type: str,
                       if_absent: bool = True) -> None:
        target, source = self._path(key), os.fspath(path)
        await asyncio.to_thread(self._write, target, if_absent, lambda handle: _copy_file(source, handle))

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._read, self._path(key), key)

    async def get_chunks(self, key: str, *, chunk_bytes: int = CHUNK_BYTES) -> AsyncIterator[bytes]:
        handle = await asyncio.to_thread(self._open, self._path(key), key)
        try:
            while chunk := await asyncio.to_thread(handle.read, chunk_bytes):
                yield chunk
        finally:
            handle.close()

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._remove, self._path(key))

    async def delete_prefix(self, prefix: str) -> int:
        _check_delete_prefix(prefix)
        return await asyncio.to_thread(self._remove_all, prefix)

    async def list(self, prefix: str) -> AsyncIterator[str]:
        check_key(prefix, prefix=True)
        for key in await asyncio.to_thread(_walk_keys, self.root, prefix):
            yield key

    def _write(self, path: Path, if_absent: bool, fill: Callable[..., object]) -> None:
        # Check-then-write is enough here: if_absent keys are content addressed
        # (a racing duplicate writes the same bytes) and one worker writes (§8.1).
        if if_absent and path.is_file():
            return
        fd, temp = _temp_file(path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                fill(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise
        _fsync_directory(path.parent)

    @staticmethod
    def _read(path: Path, key: str) -> bytes:
        try:
            return path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise FileNotFoundError(f"Blob not found: {key}") from exc

    @staticmethod
    def _open(path: Path, key: str):
        try:
            return open(path, "rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise FileNotFoundError(f"Blob not found: {key}") from exc

    def _remove(self, path: Path) -> bool:
        if path.is_dir():
            return False
        try:
            path.unlink()
        except (FileNotFoundError, NotADirectoryError):
            return False
        self._prune(path.parent)
        return True

    def _remove_all(self, prefix: str) -> int:
        keys, temps = _walk(self.root, prefix)
        # A write interrupted before its rename (crash, kill) leaves a temp
        # file holding object bytes. A prefix delete (tombstone, expiry)
        # removes those too, uncounted, so no content outlives it and the
        # emptied directories can be pruned. A write still in flight under the
        # prefix fails and is retried by its caller.
        for name in temps:
            self._remove(self.root / name)
        return sum(self._remove(self.root / key) for key in keys)

    def _prune(self, directory: Path) -> None:
        # Drop directories a delete left empty, up to (not including) the root.
        while directory != self.root and self.root in directory.parents:
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent


def _temp_file(directory: Path) -> tuple[int, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=directory)
    except FileNotFoundError:
        # A concurrent delete pruned the directory between mkdir and mkstemp.
        directory.mkdir(parents=True, exist_ok=True)
        return tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=directory)


def _copy_file(source: str, handle) -> None:
    with open(source, "rb") as reader:
        shutil.copyfileobj(reader, handle, CHUNK_BYTES)


def _fsync_directory(directory: Path) -> None:
    # Make the rename durable where the platform lets a directory be opened.
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _walk_keys(root: Path, prefix: str) -> list[str]:
    """Sorted keys under root that start with prefix, skipping temp files."""
    return _walk(root, prefix)[0]


def _walk(root: Path, prefix: str) -> tuple[list[str], list[str]]:
    """(sorted keys, temp files of in-flight or interrupted writes) under root
    whose relative paths start with prefix."""
    directory = prefix.rpartition("/")[0]
    start = root / directory if directory else root
    if not start.is_dir():
        return [], []
    keys, temps = [], []
    for current, _, files in os.walk(start):
        relative = Path(current).relative_to(root).as_posix()
        base = "" if relative == "." else f"{relative}/"
        for name in files:
            if (base + name).startswith(prefix):
                (temps if name.startswith(_TEMP_PREFIX) else keys).append(base + name)
    return sorted(keys), temps


class MemoryBlobStore:
    """In-process store for tests.

    Counters: puts (put and put_file calls, if_absent no-ops included), gets (get calls,
    hits and misses), deletes (delete calls plus keys removed by
    delete_prefix) and bytes (bytes stored by puts that wrote). A call stopped
    by a fault changes nothing.

    faults[operation] is an exception to raise or a callable (sync or async)
    that receives the key and may raise; fail() installs a counted one.
    get_hook(key) runs after an object was read and before get returns — the
    window in which a slow download races other writers.
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.puts = 0
        self.gets = 0
        self.deletes = 0
        self.bytes = 0
        self.faults: dict[str, BaseException | Callable[[str], object]] = {}
        self.get_hook: Callable[[str], object] | None = None

    def fail(self, operation: str, error: BaseException | None = None, *, times: int | None = None) -> None:
        """Make operation raise error (default ConnectionError) for the next `times` calls, or always."""
        if operation not in BLOB_OPERATIONS:
            raise ValueError(f"Unknown blob operation: {operation!r}")
        error = error or ConnectionError(f"Injected {operation} failure")
        remaining = times

        def hook(_key: str) -> None:
            nonlocal remaining
            if remaining is not None:
                if remaining <= 0:
                    return
                remaining -= 1
            raise error

        self.faults[operation] = hook

    def clear_faults(self) -> None:
        self.faults.clear()

    async def _fault(self, operation: str, key: str) -> None:
        fault = self.faults.get(operation)
        if fault is None:
            return
        if isinstance(fault, BaseException):
            raise fault
        result = fault(key)
        if inspect.isawaitable(result):
            await result

    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None:
        check_key(key)
        stored = _as_bytes(data)
        await self._fault("put", key)
        self.puts += 1
        if if_absent and key in self.objects:
            return
        self.objects[key] = stored
        self.content_types[key] = content_type
        self.bytes += len(stored)

    async def put_file(self, key: str, path: str | os.PathLike[str], *, content_type: str,
                       if_absent: bool = True) -> None:
        """Reads the file whole (this store keeps objects in memory anyway) and puts it."""
        check_key(key)
        data = await asyncio.to_thread(Path(path).read_bytes)
        await self.put(key, data, content_type=content_type, if_absent=if_absent)

    async def get(self, key: str) -> bytes:
        check_key(key)
        await self._fault("get", key)
        self.gets += 1
        if key not in self.objects:
            raise FileNotFoundError(f"Blob not found: {key}")
        data = self.objects[key]
        if self.get_hook is not None:
            result = self.get_hook(key)
            if inspect.isawaitable(result):
                await result
        return data

    async def exists(self, key: str) -> bool:
        check_key(key)
        await self._fault("exists", key)
        return key in self.objects

    async def delete(self, key: str) -> None:
        check_key(key)
        await self._fault("delete", key)
        self.deletes += 1
        self.objects.pop(key, None)
        self.content_types.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        _check_delete_prefix(prefix)
        await self._fault("delete_prefix", prefix)
        doomed = [key for key in self.objects if key.startswith(prefix)]
        for key in doomed:
            del self.objects[key]
            self.content_types.pop(key, None)
        self.deletes += len(doomed)
        return len(doomed)

    async def list(self, prefix: str) -> AsyncIterator[str]:
        check_key(prefix, prefix=True)
        await self._fault("list", prefix)
        for key in sorted(key for key in self.objects if key.startswith(prefix)):
            yield key


def parse_blob_faults(spec: str | Mapping[str, float]) -> dict[str, float]:
    """"put:0.3,get:1.0,delete:0.1" (or a mapping) -> {operation: probability}.

    Operations come from BLOB_OPERATIONS; unknown names, malformed entries and
    probabilities outside [0, 1] are ignored with a warning.
    """
    if isinstance(spec, Mapping):
        entries = list(spec.items())
    else:
        entries = [entry.partition(":")[::2] for entry in (spec or "").split(",") if entry.strip()]
    faults = {}
    for operation, value in entries:
        name = str(operation).strip().lower()
        try:
            probability = float(value)
        except (TypeError, ValueError):
            probability = -1.0
        if name not in BLOB_OPERATIONS or not 0.0 <= probability <= 1.0:
            log.warning("Ignoring invalid blob fault %s:%s", operation, value)
            continue
        faults[name] = probability
    return faults


class FaultInjectingBlobStore:
    """Wraps a store and fails its operations with fixed probabilities.

    Configured by TRAJECTORY_BLOB_FAULT for drills and chaos tests. A fault is
    decided before the wrapped call (a failed put writes nothing) by a
    random.Random that `seed` makes reproducible; `injected` counts faults per
    operation.
    """

    def __init__(self, inner: BlobStore, faults: str | Mapping[str, float], *, seed: int | None = None):
        self.inner = inner
        self.probabilities = parse_blob_faults(faults)
        self.injected = dict.fromkeys(BLOB_OPERATIONS, 0)
        self._random = random.Random(seed)

    def _maybe_fail(self, operation: str, key: str) -> None:
        probability = self.probabilities.get(operation, 0.0)
        if probability and self._random.random() < probability:
            self.injected[operation] += 1
            raise BlobFaultInjected(f"Injected blob store fault: {operation} {key}")

    async def put(self, key: str, data: bytes, *, content_type: str, if_absent: bool = True) -> None:
        self._maybe_fail("put", key)
        await self.inner.put(key, data, content_type=content_type, if_absent=if_absent)

    async def put_file(self, key: str, path: str | os.PathLike[str], *, content_type: str,
                       if_absent: bool = True) -> None:
        self._maybe_fail("put", key)
        await self.inner.put_file(key, path, content_type=content_type, if_absent=if_absent)

    async def get(self, key: str) -> bytes:
        self._maybe_fail("get", key)
        return await self.inner.get(key)

    async def get_chunks(self, key: str, *, chunk_bytes: int = CHUNK_BYTES) -> AsyncIterator[bytes]:
        self._maybe_fail("get", key)
        async for chunk in read_chunks(self.inner, key, chunk_bytes=chunk_bytes):
            yield chunk

    async def exists(self, key: str) -> bool:
        self._maybe_fail("exists", key)
        return await self.inner.exists(key)

    async def delete(self, key: str) -> None:
        self._maybe_fail("delete", key)
        await self.inner.delete(key)

    async def delete_prefix(self, prefix: str) -> int:
        self._maybe_fail("delete_prefix", prefix)
        return await self.inner.delete_prefix(prefix)

    async def list(self, prefix: str) -> AsyncIterator[str]:
        self._maybe_fail("list", prefix)
        async for key in self.inner.list(prefix):
            yield key


# -- Configuration --

_override: BlobStore | None = None
_configured: BlobStore | None = None


def _provider() -> str:
    return (os.getenv("TRAJECTORY_BLOB_PROVIDER") or "local").strip().lower()


def _flag(name: str, default: bool) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    log.warning("Invalid %s=%r; using %s", name, value, default)
    return default


def blob_store_from_env() -> BlobStore:
    """The store TRAJECTORY_BLOB_PROVIDER selects, wrapped when TRAJECTORY_BLOB_FAULT is set."""
    provider = _provider()
    if provider == "local":
        path = os.getenv("TRAJECTORY_BLOB_LOCAL_PATH") or DEFAULT_LOCAL_PATH
        store: BlobStore = LocalBlobStore(Path(path).expanduser())
    elif provider == "oss":
        store = OssBlobStore.from_env()
    else:
        raise RuntimeError(f"Unsupported TRAJECTORY_BLOB_PROVIDER: {provider!r}")
    faults = (os.getenv("TRAJECTORY_BLOB_FAULT") or "").strip()
    if faults:
        store = FaultInjectingBlobStore(store, faults)
        log.warning("Trajectory blob fault injection active: %s", faults)
    return store


def get_blob_store() -> BlobStore:
    """The installed store, else the environment's (built once, then reused)."""
    global _configured
    if _override is not None:
        return _override
    if _configured is None:
        _configured = blob_store_from_env()
    return _configured


def set_blob_store(store: BlobStore | None) -> None:
    """Install a store (tests, embedded bootstrap), used as given; None returns
    to the environment configuration, rebuilt on the next get_blob_store()."""
    global _override, _configured
    _override = store
    _configured = None
