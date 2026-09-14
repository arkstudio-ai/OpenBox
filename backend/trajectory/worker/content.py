"""Content preparation of ingested events (SPEC §8.4).

Per event, in order: generic redaction (``sanitize``), the idempotency hash,
producer helper keys, inline base64 media, content addressing of
``request.prepared`` inputs, large values and the whole-data fallback.

This module is CPU work on plain JSON values without I/O; the ingest service
runs it in a thread. Database facts it needs (existing payload rows of a
trajectory, asset metadata) arrive as lookups, and ``ContentPlanner``
pre-assigns payload ids so that a whole-data blob already contains the final
references. The ingest transaction then checks those assumptions, inserts the
new payload rows with the event's seq as ``first_seq`` and retries the batch
if anything changed underneath.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable

from trajectory.projector import _preview
from trajectory.redaction import sanitize
from trajectory.storage import encode_blob
from trajectory.types import canonical

PREVIEW_FIELDS = ("text", "content", "input", "prompt", "questions", "requested_arguments", "arguments", "summary")
RESULT_FIELDS = ("output", "result", "answers", "model_output")
HINT_FIELDS = PREVIEW_FIELDS + RESULT_FIELDS
HELPER_KEYS = ("media_sources", "asset_ref", "source_root_session_id")
JSON_MEDIA_TYPE = "application/json"
DEFAULT_MEDIA_TYPE = "application/octet-stream"
MEDIA_TYPE_CHARS = 128
SYSTEM_REF_MIN_BYTES = 1024
TOOLS_REF_MIN_BYTES = 1024
MESSAGE_REF_MIN_BYTES = 512
VALUE_MAX_DEPTH = 6
REFERENCE_KEYS = ("$ref", "$payload")
ENVELOPE_KEYS = frozenset({"$ref", "$payload", "$media"})
BLOB_UNAVAILABLE = {"availability": "not_recorded", "reason": "blob_store_unavailable"}
ASSET_ID_CHARS = 64
OSS_KEY_CHARS = 1024
#: Sanitize can grow a value (``token=a`` becomes ``token=[REDACTED]``) but not
#: fourfold, so a smaller spool line cannot produce data above the inline limit.
SIZE_HINT_FACTOR = 4
_DATA_URL = re.compile(r"data:([^;,]+)(?:;[^,;]+)*;base64,(.*)", re.DOTALL)
_MEDIA_MARKERS = (b";base64,", b'"base64"', b'"input_audio"')
_SHA256 = re.compile(r"[0-9a-f]{64}")


def new_payload_id() -> str:
    return f"pld_{uuid.uuid4().hex}"


def normalize_media_type(value) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text[:MEDIA_TYPE_CHARS] or DEFAULT_MEDIA_TYPE


def dedupe_key(sha256: str | None, media_type: str, source_asset_id: str | None, storage_kind: str) -> str:
    """sha256 hex of ``"{sha256}:{media_type}:{source_asset_id or ''}:{storage_kind}"``."""
    return hashlib.sha256(f"{sha256 or ''}:{media_type}:{source_asset_id or ''}:{storage_kind}".encode()).hexdigest()


def hash_event(event: dict) -> str:
    """``digest(event minus event_id and occurred_at)``, the recorder's content hash.

    The hashed bytes equal ``types.canonical`` of that dict (sorted keys, no
    spaces); building them per key avoids copying the event.
    """
    parts = [canonical(key) + b":" + canonical(event[key])
             for key in sorted(event) if key not in ("event_id", "occurred_at")]
    return hashlib.sha256(b"{" + b",".join(parts) + b"}").hexdigest()


def replace_nul(value):
    """U+0000 cannot be stored in PostgreSQL text or jsonb; replace it everywhere, keys included."""
    if isinstance(value, str):
        return value.replace("\x00", "�") if "\x00" in value else value
    if isinstance(value, list):
        return [replace_nul(item) for item in value]
    if isinstance(value, dict):
        return {replace_nul(key): replace_nul(item) for key, item in value.items()}
    return value


def redact_data(data: dict, raw: bytes | None = None) -> dict:
    """Step 1: the generic ``sanitize`` pass, plus the NUL replacement the databases need.

    ``raw`` is the spool line: a NUL can only be present when it has a ``\\u0000`` escape.
    """
    data = sanitize(data)
    if raw is None or b"\\u0000" in raw:
        data = replace_nul(data)
    return data


def previews(data: dict) -> dict:
    """Step 2: ``hints.preview`` candidates, computed with the projector's ``_preview``."""
    return {name: _preview(data[name]) for name in HINT_FIELDS if data.get(name) is not None}


def strip_helpers(data: dict) -> dict:
    """Step 3: remove the producer-only helper keys and return their values."""
    return {key: data.pop(key) for key in HELPER_KEYS if key in data}


def contains_reference(value) -> bool:
    if isinstance(value, dict):
        return any(key in value for key in REFERENCE_KEYS) or any(contains_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_reference(item) for item in value)
    return False


def final_hints(candidates: dict, data: dict) -> dict | None:
    """Previews of fields whose stored value holds references (all of them for a whole-data ``$payload``)."""
    if not candidates:
        return None
    if "$payload" in data:
        return {"preview": dict(candidates)}
    kept = {name: text for name, text in candidates.items() if contains_reference(data.get(name))}
    return {"preview": kept} if kept else None


def media_sources(value) -> dict[str, str]:
    """The ``media_sources`` helper: ``{sha256 of decoded bytes: asset id}``; malformed entries are skipped."""
    if not isinstance(value, dict):
        return {}
    return {sha: asset_id for sha, asset_id in value.items()
            if isinstance(sha, str) and _SHA256.fullmatch(sha) and isinstance(asset_id, str)
            and 0 < len(asset_id) <= ASSET_ID_CHARS}


def valid_oss_key(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= OSS_KEY_CHARS and not value.startswith("/")
            and ".." not in value and "\x00" not in value and "\\" not in value)


def _is_envelope(value) -> bool:
    return isinstance(value, dict) and any(key in value for key in ENVELOPE_KEYS)


# -- References --

def _reference(sha256: str | None, size_bytes: int, media_type: str) -> dict:
    return {"payload_id": None, "sha256": sha256, "size_bytes": size_bytes, "media_type": media_type,
            "availability": "available"}


@dataclass(eq=False)
class PendingRef:
    """One reference written into event data; it becomes, or reuses, a payload row in the transaction.

    ``style``: ``ref`` (``{"$ref": envelope}``), ``payload`` (whole data ``{"$payload": envelope}``),
    ``media`` (``{"$media": envelope, ...}``) or ``asset`` (``data.payload`` of ``artifact.recorded``).
    """
    style: str
    holder: dict
    envelope: dict
    storage_kind: str
    media_type: str
    size_bytes: int
    sha256: str | None = None
    kind: str | None = None
    source_asset_id: str | None = None
    storage_key: str | None = None
    content: bytes | None = None
    availability: str = "available"
    encoding: str = "identity"
    stored_bytes: int = 0
    payload_id: str | None = None
    #: A payload row with this dedupe key existed before the batch.
    existing: bool = False
    #: Points at a non-available row holding the same bytes: no new row, no upload.
    blocked: bool = False
    #: Content not stored because the blob store kept failing.
    failed: bool = False

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.sha256, self.media_type, self.source_asset_id, self.storage_kind)

    def fill(self, payload_id: str, availability: str) -> None:
        self.payload_id = payload_id
        self.availability = availability
        self.envelope["payload_id"] = payload_id
        if self.style == "ref":
            return
        self.envelope["availability"] = availability
        if availability == "deleted":
            self.envelope["reason"] = "explicitly_deleted"
        else:
            self.envelope.pop("reason", None)

    def mark_unavailable(self) -> None:
        self.failed = True
        self.content = None
        if self.style == "media":
            media_type = self.envelope.get("media_type")
            self.envelope.clear()
            self.envelope.update(BLOB_UNAVAILABLE, media_type=media_type)
        else:
            self.holder.clear()
            self.holder.update(BLOB_UNAVAILABLE)


def _json_ref(body: bytes, kind: str) -> tuple[dict, PendingRef]:
    sha = hashlib.sha256(body).hexdigest()
    envelope = {"sha256": sha, "size_bytes": len(body), "media_type": JSON_MEDIA_TYPE, "kind": kind, "payload_id": None}
    holder = {"$ref": envelope}
    return holder, PendingRef("ref", holder, envelope, "blob", JSON_MEDIA_TYPE, len(body), sha, kind=kind, content=body)


# -- Media (step 4) --

@dataclass(eq=False)
class MediaItem:
    holder: dict
    media_type: str
    declared: str
    content: bytes | None = None
    sha256: str | None = None


def has_media_markers(raw: bytes) -> bool:
    return any(marker in raw for marker in _MEDIA_MARKERS)


def extract_media(data: dict) -> list[MediaItem]:
    """Replace inline base64 media (in place) with holder dicts; identical media share one holder.

    Recognized: ``data:<mime>;base64,`` strings, ``{"type": "base64", "data"}`` objects and
    ``input_audio.data``. Holders stay empty until ``ContentPlanner`` binds them.
    """
    items: dict[tuple[str, str], MediaItem] = {}

    def media(encoded: str, declared) -> dict:
        media_type = normalize_media_type(declared)
        item = items.get((encoded, media_type))
        if item is None:
            item = MediaItem({}, media_type, str(declared)[:MEDIA_TYPE_CHARS])
            try:
                item.content = base64.b64decode(encoded, validate=True)
                item.sha256 = hashlib.sha256(item.content).hexdigest()
            except (binascii.Error, ValueError):
                pass
            items[(encoded, media_type)] = item
        return item.holder

    def walk(value, parent_key=None):
        if isinstance(value, str):
            if value.startswith("data:"):
                match = _DATA_URL.fullmatch(value)
                if match:
                    return media(match[2], match[1])
            return value
        if isinstance(value, list):
            for index, child in enumerate(value):
                value[index] = walk(child, parent_key)
            return value
        if isinstance(value, dict):
            if value.get("type") == "base64" and isinstance(value.get("data"), str):
                return media(value["data"], value.get("media_type") or DEFAULT_MEDIA_TYPE)
            if parent_key == "input_audio" and isinstance(value.get("data"), str):
                return media(value["data"], f"audio/{value.get('format') or 'wav'}")
            for key, child in value.items():
                value[key] = walk(child, key)
            return value
        return value

    walk(data)
    return list(items.values())


@dataclass(frozen=True)
class AssetView:
    """What ingest knows about a business asset from ``trajectory_meta_assets``."""
    id: str
    user_id: str | None
    workspace_id: str | None
    oss_key: str | None
    mime: str | None
    size: int | None
    deleted: bool


@dataclass(frozen=True)
class ExistingPayload:
    payload_id: str
    dedupe_key: str
    sha256: str | None
    availability: str
    storage_kind: str
    encoding: str
    stored_bytes: int
    source_asset_id: str | None


@dataclass
class TrajectoryContent:
    """Payload rows of one trajectory that matter to a batch, by dedupe key and by sha256."""
    rows: dict[str, ExistingPayload] = field(default_factory=dict)
    #: sha256 -> an available blob row: the object exists under the trajectory's blob key.
    blobs: dict[str, ExistingPayload] = field(default_factory=dict)
    #: sha256 -> a non-available row of bytes that have no available blob row.
    blocked: dict[str, ExistingPayload] = field(default_factory=dict)
    #: sha256 -> source asset ids of asset-bound rows.
    candidates: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows) -> TrajectoryContent:
        content = cls()
        rows = list(rows)
        for row in rows:
            content.rows[row.dedupe_key] = row
            if row.sha256 is None:
                continue
            if row.source_asset_id:
                content.candidates.setdefault(row.sha256, set()).add(row.source_asset_id)
            if row.storage_kind == "blob" and row.availability == "available":
                content.blobs[row.sha256] = row
        for row in rows:
            if row.sha256 is not None and row.availability != "available" and row.sha256 not in content.blobs:
                content.blocked.setdefault(row.sha256, row)
        return content


@dataclass
class Upload:
    key: str
    data: bytes
    content_type: str
    #: Indexes of the events whose references need this object.
    events: set[int] = field(default_factory=set)


@dataclass(eq=False)
class ContentPlan:
    data: dict
    refs: list[PendingRef]
    previews: dict


@dataclass
class _Planned:
    payload_id: str
    availability: str
    existing: bool
    blocked: bool = False


class ContentPlanner:
    """Steps 4-8 for the events of one batch, sharing payload ids and uploads across events."""

    def __init__(self, *, inline_bytes: int, blob_key: Callable[[str, str], str]):
        self.inline_bytes = inline_bytes
        self.blob_key = blob_key
        self._planned: dict[tuple[str, str], _Planned] = {}
        self._objects: dict[str, tuple[str, int]] = {}
        self._candidates: dict[tuple[str, str], set[str]] = {}
        self.uploads: dict[str, Upload] = {}

    def plan(self, *, index: int, trajectory_id: str, event_type: str, data: dict, media: list[MediaItem],
             helpers: dict, lookup: TrajectoryContent, assets: dict[str, AssetView], owner_user_id: str,
             workspace_id: str | None, unavailable: bool = False, size_hint: int | None = None) -> ContentPlan:
        """Rewrite ``data`` (in place, or replaced by ``$payload``) and collect its references.

        ``unavailable`` stores no new bytes for this event (the blob store failed too often);
        ``size_hint`` (the spool line length) lets small events skip the size checks.
        """
        refs: list[PendingRef] = []

        def add(ref: PendingRef | None) -> None:
            if ref is not None:
                self._plan_ref(ref, index, trajectory_id, lookup, unavailable)
                refs.append(ref)

        sources = media_sources(helpers.get("media_sources"))
        for item in media:
            add(self._bind_media(item, trajectory_id, sources, lookup, assets, owner_user_id, workspace_id))
        if event_type == "artifact.recorded" and "asset_ref" in helpers:
            add(self._asset_reference(data, helpers["asset_ref"], assets))
        candidates = previews(data)
        if event_type == "request.prepared" and isinstance(data.get("input"), dict):
            for ref in self._address_request_input(data["input"]):
                add(ref)
        if size_hint is not None and size_hint * SIZE_HINT_FACTOR <= self.inline_bytes:
            return ContentPlan(data, refs, candidates)
        body = canonical(data)
        if len(body) > self.inline_bytes:
            for ref in self._externalize_values(data):
                add(ref)
            body = canonical(data)
            if len(body) > self.inline_bytes:
                sha = hashlib.sha256(body).hexdigest()
                envelope = _reference(sha, len(body), JSON_MEDIA_TYPE)
                data = {"$payload": envelope}
                add(PendingRef("payload", data, envelope, "blob", JSON_MEDIA_TYPE, len(body), sha, content=body))
        return ContentPlan(data, refs, candidates)

    # Step 4 -----------------------------------------------------------------

    def _bind_media(self, item: MediaItem, trajectory_id: str, sources: dict[str, str], lookup: TrajectoryContent,
                    assets: dict[str, AssetView], owner_user_id: str, workspace_id: str | None) -> PendingRef | None:
        holder, media_type = item.holder, item.media_type
        holder.clear()
        if item.content is None:
            holder["$media"] = {"availability": "not_recorded", "reason": "invalid_base64", "media_type": media_type}
            return None
        sha = item.sha256
        source_id = sources.get(sha)
        if source_id is None:
            candidates = lookup.candidates.get(sha, set()) | self._candidates.get((trajectory_id, sha), set())
            if len(candidates) > 1:
                # Without a producer binding, picking one live copy could bypass
                # the deletion of the source the model actually saw.
                holder.update({"$media": {"availability": "not_recorded", "reason": "ambiguous_asset_source",
                                          "sha256": sha, "media_type": media_type},
                               "source_kind": "ambiguous_asset", "original_encoding": "base64"})
                return None
            if candidates:
                source_id = next(iter(candidates))
        asset = assets.get(source_id) if source_id else None
        if asset is not None and not asset.deleted and asset.user_id not in (None, owner_user_id) and not (
                workspace_id and asset.workspace_id == workspace_id):
            # A binding to an asset outside the execution scope is ignored.
            source_id, asset = None, None
        if asset is not None and asset.deleted:
            holder.update({"$media": {"availability": "deleted", "reason": "source_attachment_deleted",
                                      "sha256": sha, "media_type": media_type},
                           "source_asset_id": source_id, "source_kind": "asset", "original_encoding": "base64"})
            return None
        envelope = _reference(sha, len(item.content), media_type)
        holder.update({"$media": envelope, "source_kind": "asset" if source_id else "inline_non_asset",
                       "original_encoding": "base64", "declared_media_type": item.declared})
        if source_id:
            holder["source_asset_id"] = source_id
            self._candidates.setdefault((trajectory_id, sha), set()).add(source_id)
        if asset is not None and valid_oss_key(asset.oss_key):
            # A known live asset: reference its object, store no bytes.
            return PendingRef("media", holder, envelope, "asset", media_type, len(item.content), sha,
                              source_asset_id=source_id, storage_key=asset.oss_key)
        # Unbound media, or an asset whose metadata has not arrived yet: keep the
        # bytes, still bound to the asset so that its deletion reaches them.
        return PendingRef("media", holder, envelope, "blob", media_type, len(item.content), sha,
                          source_asset_id=source_id, content=item.content)

    @staticmethod
    def _asset_reference(data: dict, asset_ref, assets: dict[str, AssetView]) -> PendingRef | None:
        asset_id = asset_ref.get("asset_id") if isinstance(asset_ref, dict) else None
        if not isinstance(asset_id, str) or not 0 < len(asset_id) <= ASSET_ID_CHARS:
            data["payload"] = {"availability": "not_recorded", "reason": "invalid_asset_ref"}
            return None
        asset = assets.get(asset_id)
        oss_key = asset.oss_key if asset is not None and valid_oss_key(asset.oss_key) else asset_ref.get("oss_key")
        if not valid_oss_key(oss_key):
            data["payload"] = {"availability": "not_recorded", "reason": "invalid_asset_ref"}
            return None
        media_type = normalize_media_type(asset_ref.get("media_type") or (asset.mime if asset else None))
        size = asset_ref.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = asset.size if asset is not None and isinstance(asset.size, int) else 0
        envelope = _reference(None, size, media_type)
        data["payload"] = envelope
        ref = PendingRef("asset", data, envelope, "asset", media_type, size, None,
                         source_asset_id=asset_id, storage_key=oss_key)
        if asset is not None and asset.deleted:
            ref.availability = "deleted"
        return ref

    # Steps 5-7 --------------------------------------------------------------

    @staticmethod
    def _address_request_input(value: dict) -> list[PendingRef]:
        refs = []
        for key, kind, minimum in (("system", "system", SYSTEM_REF_MIN_BYTES),
                                   ("instructions", "system", SYSTEM_REF_MIN_BYTES),
                                   ("tools", "tools", TOOLS_REF_MIN_BYTES)):
            if value.get(key) is None or _is_envelope(value[key]):
                continue
            body = canonical(value[key])
            if len(body) >= minimum:
                value[key], ref = _json_ref(body, kind)
                refs.append(ref)
        for key in ("messages", "input"):
            items = value.get(key)
            if not isinstance(items, list):
                continue
            for position, item in enumerate(items):
                if _is_envelope(item):
                    continue
                body = canonical(item)
                if len(body) >= MESSAGE_REF_MIN_BYTES:
                    items[position], ref = _json_ref(body, "message")
                    refs.append(ref)
        return refs

    def _externalize_values(self, data: dict) -> list[PendingRef]:
        refs: list[PendingRef] = []
        limit = self.inline_bytes

        def shrink(value, depth: int):
            # Bottom-up: large leaves go first, so small siblings stay inline.
            if _is_envelope(value):
                return value
            if depth < VALUE_MAX_DEPTH:
                if isinstance(value, dict):
                    for key in list(value):
                        value[key] = shrink(value[key], depth + 1)
                elif isinstance(value, list):
                    for position, child in enumerate(value):
                        value[position] = shrink(child, depth + 1)
            if depth == 0:
                return value
            body = canonical(value)
            if len(body) <= limit:
                return value
            holder, ref = _json_ref(body, "value")
            refs.append(ref)
            return holder

        shrink(data, 0)
        return refs

    # Payload ids and uploads -------------------------------------------------

    def _plan_ref(self, ref: PendingRef, index: int, trajectory_id: str, lookup: TrajectoryContent,
                  unavailable: bool) -> None:
        key = (trajectory_id, ref.dedupe_key)
        planned = self._planned.get(key)
        blob = ref.storage_kind == "blob"
        object_key = self.blob_key(trajectory_id, ref.sha256) if blob else None
        if planned is None:
            existing = lookup.rows.get(ref.dedupe_key)
            if existing is not None:
                planned = _Planned(existing.payload_id, existing.availability, existing=True)
            elif blob and ref.sha256 in lookup.blocked and object_key not in self._objects:
                blocked = lookup.blocked[ref.sha256]
                planned = _Planned(blocked.payload_id, blocked.availability, existing=True, blocked=True)
            elif blob and unavailable and object_key not in self._objects and ref.sha256 not in lookup.blobs:
                ref.mark_unavailable()
                return
            else:
                planned = _Planned(new_payload_id(), ref.availability, existing=False)
            self._planned[key] = planned
        ref.existing, ref.blocked = planned.existing, planned.blocked
        ref.fill(planned.payload_id, planned.availability)
        if not blob:
            ref.content = None
            return
        ref.storage_key = object_key
        if planned.blocked or (planned.existing and planned.availability != "available"):
            # Never upload bytes for content that was deleted or expired.
            ref.content = None
            return
        known = self._objects.get(object_key)
        if known is None and ref.sha256 in lookup.blobs:
            row = lookup.blobs[ref.sha256]
            known = self._objects[object_key] = (row.encoding, row.stored_bytes)
        if known is None and ref.content is not None:
            stored, encoding = encode_blob(ref.content, ref.media_type)
            known = self._objects[object_key] = (encoding, len(stored))
            self.uploads[object_key] = Upload(object_key, stored, ref.media_type, {index})
        elif object_key in self.uploads:
            self.uploads[object_key].events.add(index)
        if known is not None:
            ref.encoding, ref.stored_bytes = known
        ref.content = None
