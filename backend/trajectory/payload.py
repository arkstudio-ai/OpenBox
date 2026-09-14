"""Trajectory content references in the trace database (SPEC 7.3, 8.9).

Events, records and checkpoints hold references instead of large values:
``$payload`` (externalized whole event data, checkpoint pages), ``$media``
(decoded media, asset references) and ``$ref`` (content-addressed JSON
values). Reads resolve them against ``trajectory_payloads`` at a fixed
watermark H: content is visible when ``first_seq <= H`` (404 before), deleted
or expired content is 410 and damaged content is 409. Availability is resolved
per reference at read time, so a deleted attachment never disables the fast
paths of its trajectory.

Blob bytes come from the trajectory blob store through a bounded concurrent
fetcher (at most FETCH_CONCURRENCY downloads in flight per event loop) and an
LRU cache of decoded, sha256-verified blobs (TRAJECTORY_BLOB_CACHE_BYTES).
Asset references read the business assets bucket, and only here, when an
administrator opens the payload.
"""
import asyncio
import hashlib
import json
import os
import re
import weakref
from collections import OrderedDict
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm.attributes import set_committed_value

from trajectory.config import integer
from trajectory.storage import blob_key, decode_blob, encode_blob, get_blob_store
from trajectory.store.models import SessionTrajectory, TrajectoryMetaAsset, TrajectoryPayload
from trajectory.types import CorruptContent, canonical, now

JSON_MEDIA_TYPE = "application/json"
FETCH_CONCURRENCY = 16
#: Stored blobs at least this large are decoded and hashed off the event loop.
THREAD_DECODE_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


def reference(row: TrajectoryPayload) -> dict:
    return {"payload_id": row.payload_id, "sha256": row.sha256,
            "size_bytes": row.size_bytes, "media_type": row.media_type,
            "availability": row.availability}


def dedupe_key(sha256: str | None, media_type: str, source_asset_id: str | None, storage_kind: str) -> str:
    """Identity of one content reference inside a trajectory (SPEC 6.3)."""
    return hashlib.sha256(f"{sha256 or ''}:{media_type}:{source_asset_id or ''}:{storage_kind}".encode()).hexdigest()


class LruCache:
    """Values under a byte budget; the least recently used go first and an
    entry larger than the whole budget is never kept."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.size = 0
        self._entries: OrderedDict = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key, value, size: int) -> None:
        previous = self._entries.pop(key, None)
        if previous is not None:
            self.size -= previous[1]
        if size > self.max_bytes:
            return
        self._entries[key] = (value, size)
        self.size += size
        while self.size > self.max_bytes:
            _, (_, evicted) = self._entries.popitem(last=False)
            self.size -= evicted

    def clear(self) -> None:
        self._entries.clear()
        self.size = 0


_blob_cache: LruCache | None = None
_fetch_limits: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def blob_cache() -> LruCache:
    """Decoded blob bytes keyed by (storage key, sha256); immutable, so never stale."""
    global _blob_cache
    if _blob_cache is None:
        _blob_cache = LruCache(integer("TRAJECTORY_BLOB_CACHE_BYTES", 256 * 1024 * 1024))
    return _blob_cache


def reset_blob_cache() -> None:
    """Drop the cache; the next use re-reads TRAJECTORY_BLOB_CACHE_BYTES."""
    global _blob_cache
    _blob_cache = None


def _fetch_limit() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _fetch_limits.get(loop)
    if semaphore is None:
        semaphore = _fetch_limits[loop] = asyncio.Semaphore(FETCH_CONCURRENCY)
    return semaphore


def _decode_verified(stored: bytes, encoding: str, sha256: str | None) -> bytes:
    content = decode_blob(stored, encoding)
    if sha256 is not None and hashlib.sha256(content).hexdigest() != sha256:
        raise CorruptContent("Trajectory content digest mismatch")
    return content


async def fetch_blob(store, key: str, encoding: str, sha256: str | None) -> bytes:
    """Decoded bytes of one stored blob, verified against sha256 when known."""
    cache = blob_cache()
    if sha256 is not None:
        cached = cache.get((key, sha256))
        if cached is not None:
            return cached
    async with _fetch_limit():
        try:
            stored = await (store if store is not None else get_blob_store()).get(key)
        except FileNotFoundError as exc:
            raise CorruptContent("Retained trajectory blob is missing") from exc
    if len(stored) >= THREAD_DECODE_BYTES:
        content = await asyncio.to_thread(_decode_verified, stored, encoding, sha256)
    else:
        content = _decode_verified(stored, encoding, sha256)
    if sha256 is not None:
        cache.put((key, sha256), content, len(content))
    return content


_asset_reader = None


def set_asset_reader(reader) -> None:
    """Install ``async reader(oss_key) -> bytes`` for asset payloads (tests, desktop); None restores OSS."""
    global _asset_reader
    _asset_reader = reader


def oss_internal_from_env() -> bool:
    """TRAJECTORY_OSS_INTERNAL (SPEC §13, default true): read the business bucket over its VPC endpoint."""
    return (os.getenv("TRAJECTORY_OSS_INTERNAL") or "true").strip().lower() not in {"0", "false", "no", "off"}


def oss_asset_reader(*, internal: bool | None = None):
    """An ``async reader(oss_key) -> bytes`` for ``set_asset_reader``: objects of the business bucket (OSS_BUCKET).

    ``internal`` selects the VPC endpoint (None: TRAJECTORY_OSS_INTERNAL), right
    for the worker next to the bucket and unreachable from a desktop.
    """
    async def read(oss_key: str) -> bytes:
        from core.oss import get_oss
        return await get_oss().get_object(oss_key, internal=oss_internal_from_env() if internal is None else internal)
    return read


async def read_asset_object(oss_key: str) -> bytes:
    """Bytes of a business asset object; FileNotFoundError when it is gone."""
    return await (_asset_reader or oss_asset_reader())(oss_key)


def require_content(trajectory) -> None:
    """Expired trajectories keep their summary, but no content read may succeed (410)."""
    if trajectory is not None and trajectory.content_expired_at is not None:
        raise FileNotFoundError("Trajectory content has expired")


def asset_deleted(asset: TrajectoryMetaAsset | None) -> bool:
    return asset is not None and bool(asset.is_deleted or asset.deleted_at is not None or asset.status == "deleted")


async def validate_payload(db, trajectory_id: str, payload_id: str, *, through_seq: int) -> TrajectoryPayload:
    require_content(await db.get(SessionTrajectory, trajectory_id))
    row = await db.scalar(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
        TrajectoryPayload.payload_id == payload_id, TrajectoryPayload.first_seq <= through_seq))
    if row is None:
        raise LookupError("Trajectory payload is not available at this position")
    if row.availability != "available":
        raise FileNotFoundError("Trajectory content has been deleted")
    if row.source_asset_id:
        asset = await db.get(TrajectoryMetaAsset, row.source_asset_id)
        # An asset reference has no bytes of its own; it needs a live replica
        # row. Copied media bound to a source goes when that source is deleted.
        if asset_deleted(asset) or (asset is None and row.storage_kind == "asset"):
            raise FileNotFoundError("Source attachment has been deleted")
    return row


async def read_payload(db, trajectory_id: str, payload_id: str, *, through_seq: int,
                       blob_store=None) -> tuple[TrajectoryPayload, bytes]:
    row = await validate_payload(db, trajectory_id, payload_id, through_seq=through_seq)
    if row.storage_kind == "asset":
        try:
            content = await read_asset_object(row.storage_key)
        except FileNotFoundError as exc:
            raise FileNotFoundError("Source attachment is unavailable") from exc
        if row.sha256 is not None and hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent("Trajectory content digest mismatch")
        return row, content
    if row.storage_kind != "blob" or row.sha256 is None:
        raise CorruptContent("Unsupported trajectory payload storage")
    return row, await fetch_blob(blob_store, row.storage_key, row.encoding, row.sha256)


async def payload_meta(db, trajectory, payload_id: str, *, through_seq: int) -> dict:
    """Payload identity without bytes, under the same visibility and status rules as the download."""
    row = await validate_payload(db, trajectory.id, payload_id, through_seq=through_seq)
    return {"payload_id": row.payload_id, "availability": row.availability, "media_type": row.media_type,
            "size_bytes": row.size_bytes, "sha256": row.sha256}


async def read_blob(db, trajectory, sha256: str, *, through_seq: int, blob_store=None) -> bytes:
    """The JSON bytes of a content-addressed value visible at through_seq in this trajectory.

    Media copies bound to an attachment are not values: only the payload
    endpoint serves them, after checking their source attachment.
    """
    require_content(trajectory)
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise LookupError("Trajectory blob is not available at this position")
    rows = (await db.scalars(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory.id,
        TrajectoryPayload.sha256 == sha256, TrajectoryPayload.storage_kind == "blob",
        TrajectoryPayload.media_type == JSON_MEDIA_TYPE, TrajectoryPayload.source_asset_id.is_(None),
        TrajectoryPayload.first_seq <= through_seq)
        .order_by(TrajectoryPayload.first_seq))).all()
    if not rows:
        raise LookupError("Trajectory blob is not available at this position")
    row = next((item for item in rows if item.availability == "available"), None)
    if row is None:
        raise FileNotFoundError("Trajectory content has been deleted")
    return await fetch_blob(blob_store, row.storage_key, row.encoding, row.sha256)


# -- Reference resolution --

def is_ref(value) -> bool:
    """A ``{"$ref": {...}}`` envelope; JSON Schema's string ``$ref`` is not one."""
    return isinstance(value, dict) and len(value) == 1 and isinstance(value.get("$ref"), dict)


def _chunks(items: list, size: int = 500):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def _payload_ids(value, found: set) -> None:
    if isinstance(value, list):
        for child in value:
            _payload_ids(child, found)
    elif isinstance(value, dict) and not is_ref(value):
        if isinstance(value.get("payload_id"), str) and "sha256" in value:
            found.add(value["payload_id"])
            return
        for child in value.values():
            _payload_ids(child, found)


class Resolver:
    """Resolves the references of one read at a fixed watermark.

    Lookups are batched: every round of new identities costs one query plus
    concurrent blob fetches, however many values share them. through_seq=None
    (the projection worker) skips the visibility bound. Results may share
    unchanged sub-objects with the inputs and with each other: read-only.
    """

    def __init__(self, db, trajectory_id: str, *, through_seq: int | None, blob_store=None):
        self.db = db
        self.trajectory_id = trajectory_id
        self.through_seq = through_seq
        self.blob_store = blob_store
        self._rows: dict[str, TrajectoryPayload | None] = {}
        self._assets: dict[str, TrajectoryMetaAsset | None] = {}
        self._values: dict[str, object] = {}
        self._failed: set[str] = set()
        self._expanded: dict[tuple, object] = {}

    async def rows(self, payload_ids) -> dict[str, TrajectoryPayload | None]:
        wanted = list(dict.fromkeys(payload_ids))
        missing = [identity for identity in wanted if identity not in self._rows]
        for chunk in _chunks(missing):
            found = {row.payload_id: row for row in (await self.db.scalars(select(TrajectoryPayload).where(
                TrajectoryPayload.trajectory_id == self.trajectory_id, TrajectoryPayload.payload_id.in_(chunk)))).all()}
            for identity in chunk:
                self._rows[identity] = found.get(identity)
        return {identity: self._rows[identity] for identity in wanted}

    def visible_row(self, payload_id: str) -> TrajectoryPayload | None:
        row = self._rows.get(payload_id)
        if row is None or (self.through_seq is not None and row.first_seq > self.through_seq):
            return None
        return row

    async def _load_assets(self, asset_ids) -> None:
        missing = [identity for identity in dict.fromkeys(asset_ids) if identity not in self._assets]
        for chunk in _chunks(missing):
            found = {asset.id: asset for asset in (await self.db.scalars(
                select(TrajectoryMetaAsset).where(TrajectoryMetaAsset.id.in_(chunk)))).all()}
            for identity in chunk:
                self._assets[identity] = found.get(identity)

    async def _json(self, rows: list[TrajectoryPayload], message: str) -> list:
        contents = await asyncio.gather(*(fetch_blob(self.blob_store, row.storage_key, row.encoding, row.sha256)
                                          for row in rows))
        values = []
        for content in contents:
            try:
                values.append(json.loads(content))
            except (ValueError, UnicodeDecodeError) as exc:
                raise CorruptContent(message) from exc
        return values

    async def expand_payloads(self, values: list) -> list:
        """Whole event data stored as ``{"$payload": ref}`` becomes the stored object again."""
        wanted = {value["$payload"]["payload_id"]: value["$payload"] for value in values
                  if isinstance(value, dict) and isinstance(value.get("$payload"), dict)
                  and isinstance(value["$payload"].get("payload_id"), str)}
        if not wanted:
            return values
        await self.rows(wanted)
        live = []
        for identity in wanted:
            row = self.visible_row(identity)
            if row is None:
                raise LookupError("Trajectory payload is not available at this position")
            if row.availability == "available":
                if row.storage_kind != "blob" or row.sha256 is None:
                    raise CorruptContent("Unsupported trajectory payload storage")
                live.append(row)
        loaded = {}
        for row, value in zip(live, await self._json(live, "Invalid JSON in trajectory payload")):
            if not isinstance(value, dict):
                raise CorruptContent("Trajectory JSON payload is not an object")
            loaded[row.payload_id] = value
        result = []
        for value in values:
            info = value.get("$payload") if isinstance(value, dict) else None
            identity = info.get("payload_id") if isinstance(info, dict) else None
            if identity in loaded:
                result.append(loaded[identity])
            elif identity in wanted:
                result.append({"$payload": {**info, "availability": "deleted", "reason": "explicitly_deleted"}})
            else:
                result.append(value)
        return result

    def _collect(self, value, skip_kinds, found: dict, seen: set) -> None:
        if isinstance(value, list):
            for child in value:
                self._collect(child, skip_kinds, found, seen)
        elif isinstance(value, dict):
            if not is_ref(value):
                for child in value.values():
                    self._collect(child, skip_kinds, found, seen)
                return
            info = value["$ref"]
            identity = info.get("payload_id")
            if not isinstance(identity, str) or info.get("kind") in skip_kinds or identity in seen:
                return
            seen.add(identity)
            if identity in self._values:
                self._collect(self._values[identity], skip_kinds, found, seen)
            elif identity not in self._failed:
                found[identity] = info

    async def _load_values(self, found: dict) -> None:
        await self.rows(found)
        live = []
        for identity, info in found.items():
            row = self.visible_row(identity)
            if row is None or row.availability != "available" or row.storage_kind != "blob" or row.sha256 is None:
                self._failed.add(identity)
            elif info.get("sha256") not in (None, row.sha256):
                raise CorruptContent("Trajectory reference does not match its content")
            else:
                live.append(row)
        for row, value in zip(live, await self._json(live, "Invalid JSON in trajectory content")):
            self._values[row.payload_id] = value

    async def expand_refs(self, values: list, *, skip_kinds=frozenset()) -> list:
        """``$ref`` values (nested ones included) replaced by their JSON; kinds in skip_kinds stay references."""
        skip_kinds = frozenset(skip_kinds)
        seen: set[str] = set()
        found: dict = {}
        for value in values:
            self._collect(value, skip_kinds, found, seen)
        while found:
            await self._load_values(found)
            nested: dict = {}
            for identity in found:
                if identity in self._values:
                    self._collect(self._values[identity], skip_kinds, nested, seen)
            found = nested
        return [self._substitute(value, skip_kinds) for value in values]

    def _substitute(self, value, skip_kinds):
        if isinstance(value, list):
            items = [self._substitute(child, skip_kinds) for child in value]
            return items if any(item is not child for item, child in zip(items, value)) else value
        if not isinstance(value, dict):
            return value
        if is_ref(value):
            info = value["$ref"]
            identity = info.get("payload_id")
            if not isinstance(identity, str) or info.get("kind") in skip_kinds:
                return value
            if identity not in self._values:
                row = self.visible_row(identity)
                marker = {**info, "availability": row.availability if row is not None else "not_recorded"}
                if row is not None and row.availability == "deleted":
                    marker["reason"] = "explicitly_deleted"
                return {"$ref": marker}
            key = (identity, skip_kinds)
            if key not in self._expanded:
                self._expanded[key] = self._substitute(self._values[identity], skip_kinds)
            return self._expanded[key]
        result, changed = {}, False
        for key, child in value.items():
            item = self._substitute(child, skip_kinds)
            changed = changed or item is not child
            result[key] = item
        return result if changed else value

    async def visible(self, values: list) -> list:
        """Payload references (``$payload``, ``$media``, artifact payloads) with their current availability."""
        identities: set = set()
        for value in values:
            _payload_ids(value, identities)
        if not identities:
            return values
        rows = await self.rows(identities)
        await self._load_assets([row.source_asset_id for row in rows.values() if row is not None and row.source_asset_id])
        return [self._rewrite(value) for value in values]

    def _availability(self, row: TrajectoryPayload | None) -> tuple[str, str | None]:
        if row is None:
            return "not_recorded", None
        if row.availability == "deleted":
            return "deleted", "explicitly_deleted"
        if row.availability != "available":
            return row.availability, None
        if row.source_asset_id and asset_deleted(self._assets.get(row.source_asset_id)):
            return "deleted", "source_attachment_deleted"
        return "available", None

    def _rewrite(self, value):
        if isinstance(value, list):
            items = [self._rewrite(child) for child in value]
            return items if any(item is not child for item, child in zip(items, value)) else value
        if not isinstance(value, dict) or is_ref(value):
            return value
        if isinstance(value.get("payload_id"), str) and "sha256" in value:
            availability, reason = self._availability(self.visible_row(value["payload_id"]))
            return {**value, "availability": availability, **({"reason": reason} if reason else {})}
        result, changed = {}, False
        for key, child in value.items():
            item = self._rewrite(child)
            changed = changed or item is not child
            result[key] = item
        payload = result.get("payload")
        if isinstance(payload, dict) and payload.get("availability") == "deleted" and value.get("availability") != "deleted":
            return {**result, "availability": "deleted"}
        return result if changed else value


async def expand_all(db, trajectory_id: str, values: list, *, through_seq: int, refs: bool = True,
                     blob_store=None, resolver: Resolver | None = None) -> list:
    """Stored event data as the admin API returns it: ``$payload`` objects loaded as
    today, ``$ref`` values expanded unless refs=False, and payload references
    carrying their current availability (deletion applies at every watermark)."""
    resolver = resolver or Resolver(db, trajectory_id, through_seq=through_seq, blob_store=blob_store)
    values = await resolver.expand_payloads(values)
    if refs:
        values = await resolver.expand_refs(values)
    return await resolver.visible(values)


async def expand(db, trajectory_id: str, data: dict, *, through_seq: int, refs: bool = True, blob_store=None) -> dict:
    return (await expand_all(db, trajectory_id, [data], through_seq=through_seq, refs=refs, blob_store=blob_store))[0]


async def visible_references(db, trajectory_id: str, value, *, through_seq: int):
    """Current deletion rules apply even while viewing a historical prefix."""
    return (await Resolver(db, trajectory_id, through_seq=through_seq).visible([value]))[0]


async def expand_pages(db, trajectory_id: str, references: list, *, through_seq: int, blob_store=None) -> list[dict]:
    """Checkpoint record pages, each digest-verified; any unavailable page is corruption (409)."""
    ids = [item["$payload"]["payload_id"] for item in references]
    rows = {}
    for chunk in _chunks(ids):
        rows.update((row.payload_id, row) for row in (await db.scalars(select(TrajectoryPayload).where(
            TrajectoryPayload.trajectory_id == trajectory_id, TrajectoryPayload.payload_id.in_(chunk),
            TrajectoryPayload.first_seq <= through_seq))).all())

    async def page(identity):
        row = rows.get(identity)
        if row is None or row.availability != "available" or row.storage_kind != "blob" or row.sha256 is None:
            raise CorruptContent("Checkpoint page is unavailable")
        content = await fetch_blob(blob_store, row.storage_key, row.encoding, row.sha256)
        try:
            value = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorruptContent("Invalid checkpoint page") from exc
        if not isinstance(value, dict) or not isinstance(value.get("records"), dict):
            raise CorruptContent("Invalid checkpoint page shape")
        return value
    return list(await asyncio.gather(*(page(identity) for identity in ids)))


# -- Content-addressed JSON blobs written by the projection worker --

def json_blob(trajectory_id: str, value) -> dict:
    """The canonical JSON of value as a content-addressed blob of one trajectory (not yet stored)."""
    content = canonical(value)
    sha = hashlib.sha256(content).hexdigest()
    stored, encoding = encode_blob(content, JSON_MEDIA_TYPE)
    return {"sha256": sha, "size_bytes": len(content), "stored": stored, "stored_bytes": len(stored),
            "encoding": encoding, "storage_key": blob_key(trajectory_id, sha),
            "dedupe_key": dedupe_key(sha, JSON_MEDIA_TYPE, None, "blob")}


def _insert(db, model):
    return (sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert)(model)


async def existing_payloads(db, trajectory_id: str, dedupe_keys) -> dict[str, TrajectoryPayload]:
    rows = {}
    for chunk in _chunks(list(dict.fromkeys(dedupe_keys))):
        rows.update((row.dedupe_key, row) for row in (await db.scalars(select(TrajectoryPayload).where(
            TrajectoryPayload.trajectory_id == trajectory_id, TrajectoryPayload.dedupe_key.in_(chunk)))).all())
    return rows


async def upload_json_blobs(store, blobs: list[dict], *, metrics=None) -> None:
    """Idempotent puts (content-addressed keys, if_absent), bounded like fetches."""
    async def put(blob):
        async with _fetch_limit():
            try:
                await store.put(blob["storage_key"], blob["stored"], if_absent=True,
                                content_type="application/zstd" if blob["encoding"] == "zstd" else JSON_MEDIA_TYPE)
            except Exception:
                if metrics is not None:
                    metrics.inc("blob_put_failures")
                raise
        if metrics is not None:
            metrics.inc("blob_puts")
            metrics.inc("blob_put_bytes", blob["stored_bytes"])
    unique = {blob["dedupe_key"]: blob for blob in blobs}
    await asyncio.gather(*(put(blob) for blob in unique.values()))


async def ensure_payload_rows(db, trajectory_id: str, blobs: list[dict], *,
                              first_seq: int) -> tuple[dict[str, TrajectoryPayload], int]:
    """(rows by dedupe_key, stored bytes of the rows this call inserted) for uploaded blobs.

    Existing rows keep their payload_id; a row another writer inserted
    meanwhile wins the unique key and is returned instead. Every returned row
    is visible from first_seq on: a row that starts later moves down to it,
    since the caller references the content at first_seq (a record value can
    equal a value ingest stored for a later event, and a state at first_seq
    must still expand it).
    """
    unique = {blob["dedupe_key"]: blob for blob in blobs}
    rows = await existing_payloads(db, trajectory_id, unique)
    missing = [blob for key, blob in unique.items() if key not in rows]
    inserted = 0
    if missing:
        at = now()
        created = {blob["dedupe_key"]: blob.get("payload_id") or f"pld_{uuid4().hex}" for blob in missing}
        for chunk in _chunks(missing):
            statement = _insert(db, TrajectoryPayload).values([
                {"payload_id": created[blob["dedupe_key"]], "trajectory_id": trajectory_id,
                 "dedupe_key": blob["dedupe_key"], "sha256": blob["sha256"], "size_bytes": blob["size_bytes"],
                 "stored_bytes": blob["stored_bytes"], "media_type": JSON_MEDIA_TYPE, "encoding": blob["encoding"],
                 "storage_kind": "blob", "storage_key": blob["storage_key"], "source_asset_id": None,
                 "availability": "available", "first_seq": first_seq, "created_at": at, "deleted_at": None}
                for blob in chunk])
            await db.execute(statement.on_conflict_do_nothing(index_elements=["trajectory_id", "dedupe_key"]))
        rows.update(await existing_payloads(db, trajectory_id, created))
        inserted = sum(blob["stored_bytes"] for blob in missing
                       if blob["dedupe_key"] in rows and rows[blob["dedupe_key"]].payload_id == created[blob["dedupe_key"]])
    later = sorted(row.payload_id for row in rows.values() if row.first_seq > first_seq)
    for chunk in _chunks(later):
        await db.execute(update(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
            TrajectoryPayload.payload_id.in_(chunk), TrajectoryPayload.first_seq > first_seq)
            .values(first_seq=first_seq).execution_options(synchronize_session=False))
    for row in rows.values():
        if row.first_seq > first_seq:
            set_committed_value(row, "first_seq", first_seq)
    return rows, inserted
