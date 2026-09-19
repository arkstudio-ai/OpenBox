"""Build the existing expanded checkpoint format with bounded resident memory.

Only a bounded group of record rows and referenced values is expanded at a
time. Canonical records go to a temporary file; page construction and the
whole-state digest read those bytes back instead of retaining the session.
The stored pages and digest remain readable by older workers and viewers.
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass

from sqlalchemy import LargeBinary, Text, cast, func, select, tuple_

from trajectory.config import integer
from trajectory.payload import Resolver, json_bytes_blob
from trajectory.read_budget import ReadTooLarge, read_budget
from trajectory.store.database import trace_session
from trajectory.store.models import TrajectoryRecord
from trajectory.types import PROJECTOR_VERSION, canonical, canonical_chunks

READ_BYTES = 8 * 1024 * 1024
PAGE_BYTES = 4 * 1024 * 1024
RECORD_BYTES = 16 * 1024 * 1024
TOTAL_BYTES = 512 * 1024 * 1024
PAGE_RECORDS = 100
COPY_BYTES = 256 * 1024


class CheckpointMoved(Exception):
    """Projection advanced a record while it was being captured; retry at the new head."""


async def blocking(function, *args):
    """Drain file/encoding work before cancellation can close its temporary file."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@dataclass(frozen=True)
class RecordSlice:
    record_id: str
    start_seq: int
    offset: int
    size: int


class CheckpointSpool:
    def __init__(self):
        self.file = tempfile.TemporaryFile(prefix="openbox-checkpoint-")
        self.records: list[RecordSlice] = []
        self.record_bytes = integer("TRAJECTORY_CHECKPOINT_RECORD_BYTES", RECORD_BYTES)
        self.total_bytes = integer("TRAJECTORY_CHECKPOINT_MAX_BYTES", TOTAL_BYTES)
        self.page_bytes = integer("TRAJECTORY_CHECKPOINT_PAGE_BYTES", PAGE_BYTES)

    def close(self):
        self.file.close()

    def add(self, record: dict):
        offset = self.file.tell()
        size = 0
        for chunk in canonical_chunks(record):
            size += len(chunk)
            if size > self.record_bytes or offset + size > self.total_bytes:
                raise ReadTooLarge("Checkpoint exceeds its record or temporary storage byte budget")
            self.file.write(chunk)
        self.records.append(RecordSlice(record["record_id"], int(record["start_seq"]), offset, size))

    def chunks(self, record: RecordSlice):
        self.file.seek(record.offset)
        remaining = record.size
        while remaining:
            chunk = self.file.read(min(remaining, COPY_BYTES))
            if not chunk:
                raise OSError("Checkpoint temporary file is incomplete")
            remaining -= len(chunk)
            yield chunk

    def digest(self, metadata: dict) -> str:
        """Exactly digest({...metadata, records}), including canonical record-id ordering."""
        result = hashlib.sha256()
        result.update(b"{")
        for index, key in enumerate(sorted({**metadata, "records": {}})):
            if index:
                result.update(b",")
            result.update(canonical(key) + b":")
            if key != "records":
                for chunk in canonical_chunks(metadata[key]):
                    result.update(chunk)
                continue
            result.update(b"{")
            for count, record in enumerate(sorted(self.records, key=lambda item: item.record_id)):
                if count:
                    result.update(b",")
                result.update(canonical(record.record_id) + b":")
                for chunk in self.chunks(record):
                    result.update(chunk)
            result.update(b"}")
        result.update(b"}")
        return result.hexdigest()

    def pages(self):
        page, size = [], len(b'{"records":{}}')
        for record in sorted(self.records, key=lambda item: (item.start_seq, item.record_id)):
            addition = len(canonical(record.record_id)) + 1 + record.size + bool(page)
            if page and (len(page) >= PAGE_RECORDS or size + addition > self.page_bytes):
                yield page
                page, size = [], len(b'{"records":{}}')
                addition -= 1
            page.append(record)
            size += addition
        if page:
            yield page

    def blob(self, trajectory_id: str, page: list[RecordSlice]) -> dict:
        pieces = [b'{"records":{']
        for index, record in enumerate(sorted(page, key=lambda item: item.record_id)):
            if index:
                pieces.append(b",")
            pieces.append(canonical(record.record_id) + b":")
            pieces.extend(self.chunks(record))
        pieces.append(b"}}")
        return json_bytes_blob(trajectory_id, b"".join(pieces))


async def _expand(rows, sizes, trajectory_id, through, blob_store, spool, limit):
    oversized = False
    try:
        with read_budget(limit) as budget:
            budget.consume(sum(sizes))
            async with trace_session() as db:
                resolver = Resolver(db, trajectory_id, through_seq=through, blob_store=blob_store)
                values = await resolver.expand_refs(rows)
    except ReadTooLarge:
        if len(rows) <= 1:
            raise
        oversized = True
    if oversized:
        # Release the failed resolver and exception frames before descending.
        # Otherwise each split could retain another full decoded budget.
        resolver = None
        middle = len(rows) // 2
        await _expand(rows[:middle], sizes[:middle], trajectory_id, through, blob_store, spool, limit)
        await _expand(rows[middle:], sizes[middle:], trajectory_id, through, blob_store, spool, limit)
        return
    for record in values:
        await blocking(spool.add, record)


async def capture_records(trajectory_id, through, blob_store, spool):
    """Keyset scans with a byte preflight, before loading JSON or referenced contents."""
    limit = integer("TRAJECTORY_CHECKPOINT_READ_BYTES", READ_BYTES)
    cursor = None
    while True:
        async with trace_session() as db:
            size = (func.octet_length(cast(TrajectoryRecord.data, Text)) if db.bind.dialect.name == "postgresql"
                    else func.length(cast(TrajectoryRecord.data, LargeBinary)))
            query = select(TrajectoryRecord.record_id, TrajectoryRecord.start_seq, size.label("size")).where(
                TrajectoryRecord.trajectory_id == trajectory_id, TrajectoryRecord.start_seq <= through)
            if cursor is not None:
                query = query.where(tuple_(TrajectoryRecord.start_seq, TrajectoryRecord.record_id) > cursor)
            page = (await db.execute(query.order_by(TrajectoryRecord.start_seq, TrajectoryRecord.record_id)
                                     .limit(PAGE_RECORDS))).all()
        if not page:
            return
        group, used = [], 0
        for row in page:
            if row.size > limit:
                raise ReadTooLarge("Checkpoint record exceeds its stored JSON byte budget")
            if group and used + row.size > limit:
                await _capture_group(group, trajectory_id, through, blob_store, spool, limit)
                group, used = [], 0
            group.append(row)
            used += row.size
        if group:
            await _capture_group(group, trajectory_id, through, blob_store, spool, limit)
        cursor = (page[-1].start_seq, page[-1].record_id)


async def _capture_group(group, trajectory_id, through, blob_store, spool, limit):
    async with trace_session() as db:
        rows = (await db.scalars(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory_id,
            TrajectoryRecord.record_id.in_([row.record_id for row in group])))).all()
        if len(rows) != len(group) or any(row.applied_seq > through or row.projector_version != PROJECTOR_VERSION
                                         for row in rows):
            raise CheckpointMoved()
        sizes = {row.record_id: row.size for row in group}
        data, lengths = [row.data for row in rows], [sizes[row.record_id] for row in rows]
    await _expand(data, lengths, trajectory_id, through, blob_store, spool, limit)
