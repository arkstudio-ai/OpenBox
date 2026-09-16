"""Fixed-watermark data exports built by the trajectory worker (SPEC §6.3, §8.12; maps/projection.md §7).

An admin request inserts a pending ``trajectory_exports`` row
(``create_export``). A worker claims the row with a lease, so two workers
never build the same export, and renews the lease while it builds. The ZIP is
streamed to a temporary file under a size cap:

- ``events.jsonl``: events 1..through_seq from archived segments and hot rows,
  with the data as stored (references not expanded);
- ``statistics.json``: projector statistics of a replay at ``through_seq``;
- ``payloads/{payload_id}``: every payload visible at the watermark (blobs and
  asset bytes, through ``trajectory.payload.spool_payload``);
- ``manifest.json``, written last.

The archive is uploaded from the temporary file under ``export_key`` and
verified by hashing a chunked read-back, so memory does not grow with its
size. The row completes only if neither the export nor its trajectory was
deleted in the meantime: deletion wins, and the uploaded object goes to the GC
queue. Worker start removes temporary archives that outlived their build
(``remove_stale_temp_files``).
Unfinished rows (pending, or running under an expired lease) are resumed by
the next pass of any worker, up to MAX_BUILD_ATTEMPTS builds; a worker's first
pass also takes over rows its own owner id still holds from a previous run.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update

from core.log import create_logger
from trajectory.auth import assert_admin
from trajectory.lifecycle import GC_KEY, enqueue_gc, worker_setting
from trajectory.payload import read_payload, spool_payload, validate_payload
from trajectory.projector import empty_state, reduce, statistics
from trajectory.storage import export_key, read_chunks
from trajectory.store.database import trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryExport, TrajectoryMetaAsset,
    TrajectoryPayload, TrajectorySegment)
from trajectory.types import PROJECTOR_VERSION, CorruptContent, canonical, iso, now

log = create_logger("trajectory.export")

EXPORT_FORMAT = "openbox.session-trajectory"
EXPORT_CONTENT_TYPE = "application/zip"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
LEASE_SECONDS = 120
MAX_BUILD_ATTEMPTS = 3
EXPORTS_PER_PASS = 4
EVENT_PAGE = 1000
WRITE_CHUNK_BYTES = 1024 * 1024
#: The statistics replay is CPU bound: give the event loop back this often (lease heartbeat, other worker loops).
REPLAY_YIELD_EVENTS = 200
#: What one ZIP entry adds besides its data: local header, data descriptor and central directory, ZIP64 fields.
ENTRY_OVERHEAD_BYTES = 512
#: Room kept for the manifest entry beyond the manifest text itself.
MANIFEST_RESERVE_BYTES = 4096
#: Name prefix of the temporary archives builds write to tempfile.gettempdir().
TEMP_PREFIX = "openbox-export-"
#: A temporary archive older than this outlived its build (a crash or kill); worker start removes it.
STALE_TEMP_SECONDS = 3600


class ExportTooLarge(Exception):
    """The archive outgrew TRAJECTORY_EXPORT_MAX_BYTES."""


class ExportLeaseLost(Exception):
    """The export was deleted or taken over by another worker while this one built it."""


class ExportAttemptsExceeded(Exception):
    """Repeated worker crashes must not keep rebuilding the same export forever."""


# -- Rows and downloads --

async def create_export(db, trajectory, viewer_id: str, through_seq: int) -> TrajectoryExport:
    """Insert a pending export of ``trajectory`` at ``through_seq``; a worker builds it."""
    timestamp = now()
    row = TrajectoryExport(id=f"exp_{uuid4().hex}", trajectory_id=trajectory.id, viewer_id=viewer_id,
                           through_seq=through_seq, status="pending", created_at=timestamp, updated_at=timestamp)
    db.add(row)
    await db.flush()
    return row


def export_status(row, session_id: str) -> dict:
    return {"export_id": row.id, "status": row.status, "through_seq": str(row.through_seq), "error": row.error,
            "download_url": f"/api/admin/trajectories/sessions/{session_id}/exports/{row.id}/download"
            if row.status == "completed" else None}


async def get_export(db, trajectory, export_id: str) -> TrajectoryExport:
    row = await db.scalar(select(TrajectoryExport).where(
        TrajectoryExport.id == export_id, TrajectoryExport.trajectory_id == trajectory.id))
    if row is None:
        raise LookupError("Export does not belong to this trajectory")
    return row


async def validate_export(db, trajectory, row) -> None:
    """Refuse a download that is not ready (409) or whose content was deleted since the export was created (410)."""
    if trajectory.content_expired_at is not None:
        raise FileNotFoundError("Trajectory content has expired")
    if row.status != "completed" or not row.storage_key or not row.sha256:
        raise HTTPException(409, detail="Export is not ready")
    visible = (TrajectoryPayload.trajectory_id == trajectory.id, TrajectoryPayload.first_seq <= row.through_seq)
    deleted = await db.scalar(select(TrajectoryPayload.payload_id).where(
        *visible, TrajectoryPayload.deleted_at > row.created_at).limit(1))
    # As for payload reads (SPEC §8.9): an asset reference owns no bytes and
    # needs its replica row; a copy bound to an asset goes only with a deletion
    # the replica knows about, not because the replica has not seen the asset.
    removed_source = await db.scalar(
        select(TrajectoryPayload.payload_id)
        .outerjoin(TrajectoryMetaAsset, TrajectoryMetaAsset.id == TrajectoryPayload.source_asset_id)
        .where(*visible, TrajectoryPayload.source_asset_id.is_not(None), TrajectoryPayload.availability == "available",
               or_(and_(TrajectoryPayload.storage_kind == "asset", TrajectoryMetaAsset.id.is_(None)),
                   TrajectoryMetaAsset.is_deleted.is_(True), TrajectoryMetaAsset.deleted_at.is_not(None),
                   TrajectoryMetaAsset.status == "deleted"))
        .limit(1))
    if deleted or removed_source:
        raise FileNotFoundError("Export invalidated by explicit content deletion; create a new export")


# -- Worker service --

def remove_stale_temp_files(directory: str | None = None, *, max_age_seconds: float = STALE_TEMP_SECONDS) -> int:
    """Delete ``openbox-export-*`` files older than ``max_age_seconds`` from the temporary directory builds use.

    A build removes its archive however it ends, unless its process died
    first. Returns how many files were removed; blocking, so call it in a
    worker thread.
    """
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        with os.scandir(directory or tempfile.gettempdir()) as entries:
            for entry in entries:
                if not entry.name.startswith(TEMP_PREFIX):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except OSError as exc:
        log.warning("Stale export temporary files were not removed error_type=%s", type(exc).__name__)
    if removed:
        log.info("Removed stale export temporary files count=%s", removed)
    return removed


class ExportService:
    """Builds exports under a lease; ``owner_id`` must be unique among running workers."""

    def __init__(self, settings, *, blob_store, metrics, owner_id: str, lease_seconds: float = LEASE_SECONDS):
        self.settings = settings
        self.blob_store = blob_store
        self.metrics = metrics
        self.owner_id = owner_id if len(owner_id) <= 64 else hashlib.sha256(owner_id.encode()).hexdigest()
        self.max_bytes = worker_setting(settings, "export_max_bytes", "TRAJECTORY_EXPORT_MAX_BYTES",
                                        DEFAULT_MAX_BYTES)
        self.lease = timedelta(seconds=lease_seconds)
        self._resumed = False
        self._building: set[str] = set()

    async def run_once(self) -> int:
        """Claim and build unfinished exports; returns how many builds ran (any outcome)."""
        built = 0
        try:
            for _ in range(EXPORTS_PER_PASS):
                export_id = await self._claim()
                if export_id is None:
                    break
                await self._build_claimed(export_id)
                built += 1
        finally:
            self._resumed = True
        return built

    async def build(self, export_id: str) -> str | None:
        """Claim one export and build it now; returns its status afterwards (None when the row is gone)."""
        if await self._claim(export_id):
            await self._build_claimed(export_id)
        async with trace_session() as db:
            row = await db.get(TrajectoryExport, export_id)
            return row.status if row is not None else None

    def _claimable(self, at: datetime) -> tuple:
        expired = or_(TrajectoryExport.lease_until.is_(None), TrajectoryExport.lease_until < at)
        if not self._resumed:
            # A previous run under this owner id cannot still be building.
            expired = or_(expired, TrajectoryExport.lease_owner == self.owner_id)
        return TrajectoryExport.status.in_(("pending", "running")), expired

    async def _claim(self, export_id: str | None = None) -> str | None:
        timestamp = now()
        if export_id is None:
            query = (select(TrajectoryExport.id).where(*self._claimable(timestamp))
                     .order_by(TrajectoryExport.created_at, TrajectoryExport.id).limit(EXPORTS_PER_PASS * 2))
            if self._building:
                query = query.where(TrajectoryExport.id.not_in(self._building))
            async with trace_session() as db:
                candidates = (await db.scalars(query)).all()
        else:
            candidates = [] if export_id in self._building else [export_id]
        for candidate in candidates:
            async with trace_session() as db:
                attempts = await db.scalar(
                    update(TrajectoryExport)
                    .where(TrajectoryExport.id == candidate, *self._claimable(timestamp))
                    .values(status="running", lease_owner=self.owner_id, lease_until=timestamp + self.lease,
                            updated_at=timestamp, attempts=TrajectoryExport.attempts + 1)
                    .returning(TrajectoryExport.attempts)
                    .execution_options(synchronize_session=False)
                )
            if attempts is not None:
                if attempts > MAX_BUILD_ATTEMPTS:
                    await self._fail(candidate, ExportAttemptsExceeded())
                    continue
                return candidate
        return None

    async def _build_claimed(self, export_id: str) -> None:
        self._building.add(export_id)
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".zip")
            os.close(fd)
            await self._leased(export_id, self._produce(export_id, path))
        except ExportLeaseLost:
            log.info("Trajectory export %s was deleted or taken over during its build", export_id)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._release_lease(export_id))
            raise
        except Exception as exc:
            await self._fail(export_id, exc)
        finally:
            self._building.discard(export_id)
            if path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    async def _leased(self, export_id: str, work) -> None:
        """Run ``work`` while a heartbeat renews the lease; losing the lease cancels the work."""
        build = asyncio.ensure_future(work)
        heartbeat = asyncio.create_task(self._heartbeat(export_id))
        try:
            done, _ = await asyncio.wait({build, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if build in done:
                return build.result()
            raise ExportLeaseLost(export_id)
        finally:
            for task in (build, heartbeat):
                task.cancel()
            await asyncio.gather(build, heartbeat, return_exceptions=True)

    async def _heartbeat(self, export_id: str) -> None:
        """Renew the lease every third of its length; returns once it is lost or could not be renewed in time."""
        deadline = now() + self.lease
        while True:
            await asyncio.sleep(self.lease.total_seconds() / 3)
            try:
                timestamp = now()
                async with trace_session() as db:
                    renewed = (await db.execute(
                        update(TrajectoryExport)
                        .where(TrajectoryExport.id == export_id, TrajectoryExport.lease_owner == self.owner_id,
                               TrajectoryExport.status == "running")
                        .values(lease_until=timestamp + self.lease)
                        .execution_options(synchronize_session=False)
                    )).rowcount
            except Exception as exc:
                log.warning("Trajectory export %s lease renewal failed: %s", export_id, type(exc).__name__)
                if now() >= deadline:
                    return
                continue
            if renewed != 1:
                return
            deadline = timestamp + self.lease

    async def _produce(self, export_id: str, path: str) -> None:
        async with trace_session() as db:
            row = await db.get(TrajectoryExport, export_id)
            if row is None or row.status != "running" or row.lease_owner != self.owner_id:
                raise ExportLeaseLost(export_id)
            trajectory = await db.get(SessionTrajectory, row.trajectory_id)
            if trajectory is None or trajectory.deleted_at is not None or trajectory.content_expired_at is not None:
                raise LookupError("Trajectory was deleted")
            viewer_id, through = row.viewer_id, row.through_seq
        await assert_admin(viewer_id)
        await self._write_archive(trajectory, through, path)
        await assert_admin(viewer_id)
        sha, size = await asyncio.to_thread(_file_digest, path)
        key = export_key(export_id, sha)
        await self._record_upload(export_id, key)
        # A streamed upload and a chunked read-back: memory stays bounded by a chunk whatever the archive's size.
        await self.blob_store.put_file(key, path, content_type=EXPORT_CONTENT_TYPE, if_absent=False)
        if await _stored_digest(self.blob_store, key) != sha:
            raise CorruptContent("Export digest mismatch")
        await self._complete(export_id, trajectory.id, key, sha, size)

    async def _record_upload(self, export_id: str, key: str) -> None:
        """Keep the object key on the running row before uploading, so deletion and retries can collect it."""
        async with trace_session() as db:
            row = await db.get(TrajectoryExport, export_id, with_for_update=True)
            if row is None or row.status != "running" or row.lease_owner != self.owner_id:
                raise ExportLeaseLost(export_id)
            if row.storage_key and row.storage_key != key:
                await enqueue_gc(db, GC_KEY, row.storage_key, "export_superseded")
            row.storage_key, row.updated_at = key, now()

    async def _complete(self, export_id: str, trajectory_id: str, key: str, sha: str, size: int) -> None:
        timestamp = now()
        async with trace_session() as db:
            trajectory = await db.get(SessionTrajectory, trajectory_id, with_for_update=True)
            row = await db.get(TrajectoryExport, export_id, with_for_update=True)
            live = trajectory is not None and trajectory.deleted_at is None and trajectory.content_expired_at is None
            if live and row is not None and row.status == "running" and row.lease_owner == self.owner_id:
                row.status, row.storage_key, row.sha256, row.size_bytes = "completed", key, sha, size
                row.error, row.lease_owner, row.lease_until, row.updated_at = None, None, None, timestamp
                completed = True
            else:
                completed = False
                if row is None or row.storage_key != key:
                    # Deletion won (or another worker took over with its own
                    # archive): nothing may keep this object alive.
                    await enqueue_gc(db, GC_KEY, key, "export_deleted", at=timestamp)
        if not completed:
            raise ExportLeaseLost(export_id)
        self.metrics.inc("exports_built")

    async def _fail(self, export_id: str, exc: Exception) -> None:
        log.warning("Trajectory export %s failed: %s", export_id, type(exc).__name__)
        async with trace_session() as db:
            row = await db.get(TrajectoryExport, export_id, with_for_update=True)
            if row is None or row.status != "running" or row.lease_owner != self.owner_id:
                return
            if row.storage_key:
                await enqueue_gc(db, GC_KEY, row.storage_key, "export_failed")
            row.status, row.error, row.storage_key = "failed", type(exc).__name__, None
            row.lease_owner, row.lease_until, row.updated_at = None, None, now()

    async def _release_lease(self, export_id: str) -> None:
        async with trace_session() as db:
            await db.execute(
                update(TrajectoryExport)
                .where(TrajectoryExport.id == export_id, TrajectoryExport.lease_owner == self.owner_id,
                       TrajectoryExport.status == "running")
                .values(lease_until=now())
                .execution_options(synchronize_session=False)
            )

    async def _write_archive(self, trajectory, through: int, path: str) -> dict:
        manifest = {"format": EXPORT_FORMAT, "version": 1, "projector_version": PROJECTOR_VERSION,
                    "trajectory_id": trajectory.id, "through_seq": str(through), "created_at": iso(now()),
                    "files": [], "missing_payloads": []}
        state = empty_state()
        async with _ZipWriter(path, self.max_bytes) as archive:
            async with archive.entry("events.jsonl") as entry:
                replayed = 0
                async for event in self._events(trajectory.id, through):
                    await entry.write(canonical(event) + b"\n")
                    state = reduce(state, await self._expanded(trajectory.id, event, through))
                    replayed += 1
                    if replayed % REPLAY_YIELD_EVENTS == 0:
                        # Without awaiting anything else the replay would hold the loop for the whole
                        # trajectory: no lease renewal, ingest, projection or admin API meanwhile.
                        await asyncio.sleep(0)
            manifest["files"].append(entry.describe())
            manifest["files"].append(await archive.write("statistics.json", canonical(statistics(state))))
            gaps = [{"record_id": record["record_id"], "seq": record["start_seq"], "data": record["data"]}
                    for record in state["records"].values() if record["kind"] == "gap"]
            manifest.update(user_id=trajectory.user_id, session_id=trajectory.session_id,
                            coverage_start=iso(trajectory.started_at), recording_status=trajectory.recording_status,
                            gaps=gaps, unsupported_events=state["unsupported_events"], complete=False)
            # Room the manifest entry needs at the end, kept current as payload entries are added.
            reserve = len(canonical(manifest)) + MANIFEST_RESERVE_BYTES

            def note(section: str, item: dict) -> None:
                nonlocal reserve
                manifest[section].append(item)
                reserve += len(canonical(item)) + 1

            for payload in await self._visible_payloads(trajectory.id, through):
                name = f"payloads/{payload.payload_id}"
                missing = {"payload_id": payload.payload_id, "availability": payload.availability}
                if not self._fits(archive, name, payload.size_bytes, reserve):
                    note("missing_payloads", {**missing, "reason": ExportTooLarge.__name__})
                    continue
                try:
                    async with trace_session() as db:
                        row = await validate_payload(db, trajectory.id, payload.payload_id, through_seq=through)
                    downloaded = 0

                    def consume(size: int) -> None:
                        nonlocal downloaded
                        downloaded += size
                        if not self._fits(archive, name, downloaded, reserve):
                            raise ExportTooLarge()

                    # Verify and measure before opening a ZIP entry: an unavailable or oversized
                    # source must leave no partial entry. Both the download and the ZIP copy are
                    # chunked; decoded content lives in an anonymous file, never the blob cache.
                    content = await spool_payload(row, blob_store=self.blob_store, consume=consume)
                except (FileNotFoundError, LookupError, ExportTooLarge) as exc:
                    note("missing_payloads", {**missing, "reason": type(exc).__name__})
                    continue
                try:
                    async with archive.entry(name) as entry:
                        async for chunk in content.chunks(WRITE_CHUNK_BYTES):
                            await entry.write(chunk)
                    described = entry.describe()
                finally:
                    content.close()
                note("files", {**described, "payload_id": payload.payload_id, "media_type": payload.media_type})
            manifest["complete"] = not manifest["missing_payloads"] and not state["unsupported_events"] and not gaps
            await archive.write("manifest.json", canonical(manifest))
        return manifest

    def _fits(self, archive: _ZipWriter, name: str, size: int, reserve: int) -> bool:
        """Whether an entry of ``size`` bytes still fits under the cap, leaving ``reserve`` for the manifest.

        Deflate can expand incompressible data slightly; the entry's headers
        and central directory record come on top.
        """
        worst = size + size // 1000 + ENTRY_OVERHEAD_BYTES + 2 * len(name.encode())
        return archive.size + worst + reserve <= self.max_bytes

    async def _events(self, trajectory_id: str, through: int):
        """Stored events 1..through in order: segments for archived ranges, hot rows for the rest.

        Hot rows are read before the segment lookup, so a range archived in
        between is found in its segment instead of looking like a gap.
        """
        from trajectory.segments import load_segment

        events = TrajectoryEvent.__table__
        after = 0
        while after < through:
            segment = None
            async with trace_session() as db:
                page = (await db.execute(
                    select(events)
                    .where(events.c.trajectory_id == trajectory_id, events.c.seq > after, events.c.seq <= through)
                    .order_by(events.c.seq)
                    .limit(EVENT_PAGE)
                )).mappings().all()
                if not page or page[0]["seq"] != after + 1:
                    segment = await db.scalar(select(TrajectorySegment).where(
                        TrajectorySegment.trajectory_id == trajectory_id, TrajectorySegment.from_seq <= after + 1,
                        TrajectorySegment.to_seq >= after + 1).limit(1))
            if segment is not None:
                rows = [row for row in await load_segment(self.blob_store, segment) if after < int(row["seq"]) <= through]
            elif page and page[0]["seq"] == after + 1:
                rows = page
            elif not page:
                raise CorruptContent("Committed trajectory tail is missing")
            else:
                raise CorruptContent(f"Trajectory sequence gap before {page[0]['seq']}")
            for row in rows:
                seq = int(row["seq"])
                if seq != after + 1:
                    raise CorruptContent(f"Trajectory sequence gap before {seq}")
                yield _event_dict(row)
                after = seq

    async def _expanded(self, trajectory_id: str, event: dict, through: int) -> dict:
        """The event with externalized whole data (``$payload``) loaded, as the replay needs it."""
        data = event.get("data")
        if not isinstance(data, dict) or "$payload" not in data:
            return event
        reference = data["$payload"]
        try:
            async with trace_session() as db:
                _, content = await read_payload(db, trajectory_id, reference["payload_id"], through_seq=through)
        except FileNotFoundError:
            return {**event, "data": {"$payload": {**reference, "availability": "deleted",
                                                   "reason": "explicitly_deleted"}}}
        try:
            value = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorruptContent("Invalid JSON in trajectory payload") from exc
        if not isinstance(value, dict):
            raise CorruptContent("Trajectory JSON payload is not an object")
        return {**event, "data": value}

    @staticmethod
    async def _visible_payloads(trajectory_id: str, through: int) -> list:
        async with trace_session() as db:
            return list((await db.execute(
                select(TrajectoryPayload.payload_id, TrajectoryPayload.availability, TrajectoryPayload.size_bytes,
                       TrajectoryPayload.media_type)
                .where(TrajectoryPayload.trajectory_id == trajectory_id, TrajectoryPayload.first_seq <= through)
                .order_by(TrajectoryPayload.first_seq, TrajectoryPayload.payload_id)
            )).all())


def _event_dict(row) -> dict:
    """A stored event (segment line or hot row) in the events API shape, data as stored."""
    return {"event_id": row["event_id"], "trajectory_id": row["trajectory_id"], "user_id": row["user_id"],
            "session_id": row["session_id"], **(row["context"] or {}), "source_session_id": row["source_session_id"],
            "seq": str(row["seq"]), "type": row["type"], "version": row["version"],
            "occurred_at": iso(row["occurred_at"]), "recorded_at": iso(row["recorded_at"]), "data": row["data"]}


def _file_digest(path: str) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(WRITE_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


async def _stored_digest(store, key: str) -> str:
    """sha256 of a stored object, hashed chunk by chunk as the store streams it."""
    digest = hashlib.sha256()
    async for chunk in read_chunks(store, key, chunk_bytes=WRITE_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


async def _zip_io(function, *args):
    """A cancelled build must finish the current chunk before closing its ZIP/file."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except BaseException:
        await asyncio.gather(task, return_exceptions=True)
        raise


class _ZipWriter:
    """A ZIP file written entry by entry from the event loop, with file I/O and deflate in worker threads."""

    def __init__(self, path: str, max_bytes: int):
        self._path = path
        self._max_bytes = max_bytes
        self._file = None
        self._zip = None

    async def __aenter__(self) -> _ZipWriter:
        def open_archive():
            self._file = open(self._path, "wb")
            self._zip = zipfile.ZipFile(self._file, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)

        try:
            await _zip_io(open_archive)
        except BaseException:
            if self._zip is not None:
                self._zip.close()
            if self._file is not None:
                self._file.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        def close():
            try:
                if exc_type is None:
                    self._zip.close()
                else:
                    # The temporary file is discarded, but the ZipFile must
                    # not try to finish it later from its finalizer.
                    with contextlib.suppress(Exception):
                        self._zip.close()
            finally:
                self._file.close()

        await _zip_io(close)
        if exc_type is None and os.path.getsize(self._path) > self._max_bytes:
            raise ExportTooLarge(f"Export exceeds {self._max_bytes} bytes")

    @property
    def size(self) -> int:
        return self._file.tell()

    def check_size(self) -> None:
        if self.size > self._max_bytes:
            raise ExportTooLarge(f"Export exceeds {self._max_bytes} bytes")

    def entry(self, name: str) -> _ZipEntry:
        return _ZipEntry(self, name)

    async def write(self, name: str, content: bytes) -> dict:
        async with self.entry(name) as entry:
            await entry.write(content)
        return entry.describe()


class _ZipEntry:
    def __init__(self, archive: _ZipWriter, name: str):
        self._archive = archive
        self.name = name
        self.size = 0
        self._digest = hashlib.sha256()
        self._buffer = bytearray()
        self._handle = None

    async def __aenter__(self) -> _ZipEntry:
        info = zipfile.ZipInfo(self.name, date_time=time.gmtime()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED

        def open_entry():
            self._handle = self._archive._zip.open(info, "w", force_zip64=True)

        try:
            await _zip_io(open_entry)
        except BaseException:
            if self._handle is not None:
                self._handle.close()
            raise
        return self

    async def write(self, data: bytes) -> None:
        self._digest.update(data)
        self.size += len(data)
        view = memoryview(data)
        while view:
            take = min(len(view), WRITE_CHUNK_BYTES - len(self._buffer))
            self._buffer.extend(view[:take])
            view = view[take:]
            if len(self._buffer) == WRITE_CHUNK_BYTES:
                await self._flush()

    async def _flush(self) -> None:
        chunk, self._buffer = bytes(self._buffer), bytearray()
        if chunk:
            await _zip_io(self._handle.write, chunk)
        self._archive.check_size()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                await self._flush()
        finally:
            await _zip_io(self._handle.close)
        if exc_type is None:
            self._archive.check_size()

    def describe(self) -> dict:
        return {"path": self.name, "sha256": self._digest.hexdigest(), "size_bytes": self.size}
