"""Projection of committed trajectory events (SPEC 8.8).

Each pass takes live trajectories whose ``projected_seq`` lags ``committed_seq``
and folds one bounded batch of events per trajectory with trajectory.projector.
References stay in place (previews come from the ingest hints) except where
the reducer reads a value itself (repository.reduction_events). The batch first
loads every record it can touch: explicit targets, the assistants of committed
messages, the latest system record of each (agent_id, source_session_id) and,
for interruptions and gaps, every open tool/request/assistant/step record of
the run. Each touched record is written once per batch, values above
TRAJECTORY_RECORD_INLINE_BYTES as content-addressed ``$ref`` blobs, together
with its (record, event) links and the session summary.

Blob uploads run outside database transactions. The write transaction locks
the trajectory row and gives up when the trajectory was deleted, expired or
projected by someone else in the meantime.
"""
import asyncio
import hashlib
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.log import create_logger
from trajectory.config import integer
from trajectory.payload import JSON_MEDIA_TYPE, Resolver, ensure_payload_rows, existing_payloads, is_ref, json_blob, upload_json_blobs
from trajectory.projector import TERMINAL, contribution, reduce, targets
from trajectory.repository import (checkpoint_blobs, expanded_state, record_key, records_for_reduction, reduction_events,
    store_checkpoint, stored_events)
from trajectory.store.database import trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryRecord, TrajectoryRecordEvent,
    TrajectorySessionSummary)
from trajectory.types import EVENT_TYPES, PROJECTOR_VERSION, CorruptContent, canonical

log = create_logger("trajectory.projection")

SEARCH_DOC_CHARS = 4000
#: Values nested deeper than this inside record data are sized and stored as a whole.
EXTERNALIZE_DEPTH = 6
#: Trajectories visited per pass; the rest wait for the next pass.
PASS_TRAJECTORIES = 100
_CHUNK = 500
_REF_SIZE = len(canonical({"$ref": {"sha256": "0" * 64, "size_bytes": 0, "media_type": JSON_MEDIA_TYPE,
                                    "kind": "value", "payload_id": "pld_" + "0" * 32}}))
_LOG_EVERY_SECONDS = 60.0
_LOGGED_LIMIT = 1000


def _setting(settings, name: str, env: str, default: int) -> int:
    value = getattr(settings, name, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return integer(env, default)


def _insert(db, model):
    return (sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert)(model)


def _chunks(items: list):
    for offset in range(0, len(items), _CHUNK):
        yield items[offset:offset + _CHUNK]


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def search_doc(record: dict) -> str:
    """kind, id, title, previews, status reason, tool name and error text of a record (at most 4000 characters)."""
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    error = data.get("error")
    parts = [record.get("kind"), record.get("record_id"), record.get("title"), record.get("preview"),
             record.get("result_preview"), record.get("status_reason"), data.get("tool"), data.get("name")]
    parts.extend([error.get("message"), error.get("type"), error.get("code")] if isinstance(error, dict) else [error])
    text = " ".join(str(part) for part in parts if part not in (None, "") and not isinstance(part, (dict, list)))
    return text.replace("\x00", "")[:SEARCH_DOC_CHARS]


def reference_id(trajectory_id: str, dedupe_key: str) -> str:
    """Payload id of a record value stored by projection, derived from its content.

    A container that holds a reference is serialized (and hashed) with that
    reference's payload id, so the id must be known before any row exists.
    """
    return "pld_" + hashlib.sha256(f"{trajectory_id}:{dedupe_key}".encode()).hexdigest()[:32]


class _ReferenceRace(Exception):
    """Another writer stored a record value under a different payload id meanwhile."""


#: Rounds of re-serializing records after finding values already stored under
#: other payload ids; each round settles one more level of nesting.
_REFERENCE_ROUNDS = EXTERNALIZE_DEPTH + 2


class _Externalizer:
    """Replaces large values inside records by ``$ref`` envelopes.

    ``known`` maps the dedupe keys of values already stored under another
    payload id (by ingest, for example); every other value uses reference_id.
    """

    def __init__(self, trajectory_id: str, inline_bytes: int, known: dict[str, str]):
        self.trajectory_id = trajectory_id
        self.inline_bytes = inline_bytes
        self.known = known
        self.blobs: dict[str, dict] = {}

    def record(self, record: dict) -> dict:
        result = dict(record)
        for field in ("data", "blocks"):
            if isinstance(record.get(field), (dict, list)):
                result[field] = self._container(record[field], 1)[0]
        return result

    def _value(self, value, depth: int):
        if is_ref(value) or not isinstance(value, (dict, list, str)):
            return value, len(canonical(value))
        if isinstance(value, str) or depth >= EXTERNALIZE_DEPTH:
            size = len(canonical(value))
        else:
            value, size = self._container(value, depth + 1)
        return (self._reference(value), _REF_SIZE) if size > self.inline_bytes else (value, size)

    def _container(self, value, depth: int):
        # Children first: an unchanged prompt keeps its own blob while the
        # container around it changes.
        changed = False
        if isinstance(value, dict):
            items, size = {}, 2 + max(len(value) - 1, 0)
            for key, child in value.items():
                item, child_size = self._value(child, depth)
                changed = changed or item is not child
                items[key] = item
                size += len(canonical(key)) + 1 + child_size
        else:
            items, size = [], 2 + max(len(value) - 1, 0)
            for child in value:
                item, child_size = self._value(child, depth)
                changed = changed or item is not child
                items.append(item)
                size += child_size
        return (items if changed else value), size

    def _reference(self, value) -> dict:
        blob = json_blob(self.trajectory_id, value)
        blob["payload_id"] = self.known.get(blob["dedupe_key"]) or reference_id(self.trajectory_id, blob["dedupe_key"])
        self.blobs.setdefault(blob["dedupe_key"], blob)
        return {"$ref": {"sha256": blob["sha256"], "size_bytes": blob["size_bytes"], "media_type": JSON_MEDIA_TYPE,
                         "kind": "value", "payload_id": blob["payload_id"]}}


def _summary_rules(events: list[dict], running_status: str, model: str | None) -> tuple[str, str | None, bool]:
    """(running_status, model, gap seen) after the batch, by the same per-event rules as before."""
    gap = False
    for event in events:
        family, _, action = event["type"].partition(".")
        data = event["data"] if isinstance(event["data"], dict) else {}
        if family == "run":
            if action == "started":
                running_status = "running"
            elif action in {"finished", "interrupted"}:
                running_status = "waiting" if data.get("status") == "waiting" else "error" if data.get("status") == "failed" else "idle"
        if family in {"permission", "question"} and action in {"requested", "asked"}:
            running_status = "waiting"
        if event["type"] == "request.started" and data.get("model"):
            model = str(data["model"])[:128]
        if event["type"] == "recording.gap" and data.get("phase") != "paused":
            gap = True
    return running_status, model, gap


class ProjectionService:
    def __init__(self, settings, *, blob_store, metrics):
        self.settings = settings
        self.blob_store = blob_store
        self.metrics = metrics
        self.batch_events = _setting(settings, "projection_batch_events", "TRAJECTORY_PROJECTION_BATCH_EVENTS", 200)
        self.record_inline_bytes = _setting(settings, "record_inline_bytes", "TRAJECTORY_RECORD_INLINE_BYTES", 16384)
        self.checkpoint_interval = _setting(settings, "checkpoint_interval", "TRAJECTORY_CHECKPOINT_INTERVAL", 1000)
        self._logged: dict[tuple[str, str], float] = {}

    async def run_once(self) -> int:
        """One pass: a batch for each lagging trajectory, then its due checkpoint.

        Returns events projected plus checkpoints written (0 when idle). One
        failing trajectory is logged and skipped; the others still progress.
        """
        async with trace_session() as db:
            candidates = (await db.scalars(select(SessionTrajectory.id).where(
                SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None),
                (SessionTrajectory.projected_seq < SessionTrajectory.committed_seq)
                | (SessionTrajectory.projected_seq - SessionTrajectory.checkpoint_seq >= self.checkpoint_interval))
                .order_by(SessionTrajectory.last_activity_at, SessionTrajectory.id).limit(PASS_TRAJECTORIES))).all()
        progress = 0
        for trajectory_id in candidates:
            try:
                progress += await self.project(trajectory_id)
                progress += int(await self.maybe_checkpoint(trajectory_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_failure(trajectory_id, exc)
        await self._lag_gauge()
        return progress

    def _log_failure(self, trajectory_id: str, exc: Exception) -> None:
        key, at = (trajectory_id, type(exc).__name__), time.monotonic()
        if at - self._logged.get(key, -_LOG_EVERY_SECONDS) >= _LOG_EVERY_SECONDS:
            if len(self._logged) >= _LOGGED_LIMIT:
                self._logged = {item: logged for item, logged in self._logged.items() if at - logged < _LOG_EVERY_SECONDS}
            self._logged[key] = at
            # Type only: messages of storage and database errors can carry content.
            log.warning("Trajectory projection failed trajectory_id=%s error_type=%s", trajectory_id, type(exc).__name__)

    async def _lag_gauge(self) -> None:
        if self.metrics is None:
            return
        async with trace_session() as db:
            lag = await db.scalar(select(func.coalesce(func.sum(SessionTrajectory.committed_seq - SessionTrajectory.projected_seq), 0))
                                  .where(SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None)))
        self.metrics.set_gauge("projection_lag_events", int(lag or 0))

    async def project(self, trajectory_id: str, max_events: int | None = None) -> int:
        """Project the next batch (at most max_events, default 5 x TRAJECTORY_PROJECTION_BATCH_EVENTS); returns its size."""
        limit = max_events or self.batch_events * 5
        async with trace_session() as db:
            trajectory = await db.get(SessionTrajectory, trajectory_id)
            if trajectory is None or trajectory.deleted_at is not None or trajectory.content_expired_at is not None:
                return 0
            base = trajectory.projected_seq
            until = min(trajectory.committed_seq, base + limit)
            if until <= base:
                return 0
            rows = await stored_events(db, trajectory, base, until, limit, blob_store=self.blob_store)
            if [row["seq"] for row in rows] != list(range(base + 1, until + 1)):
                raise CorruptContent("Committed trajectory events are not contiguous")
            resolver = Resolver(db, trajectory_id, through_seq=None, blob_store=self.blob_store)
            batch = await reduction_events(resolver, rows)
            ids = await self._affected(db, trajectory_id, [event for event, _ in batch])
            loaded = []
            for chunk in _chunks(sorted(ids)):
                loaded.extend((await db.scalars(select(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory_id,
                    TrajectoryRecord.record_id.in_(chunk)))).all())
            records = await records_for_reduction(resolver, loaded)

        state = {"projector_version": PROJECTOR_VERSION, "through_seq": str(base), "records": records,
                 "unsupported_events": [], "coverage_start": None}
        before = {record_id: contribution(value) for record_id, value in records.items()}
        links: set[tuple[str, int]] = set()
        for event, hints in batch:
            previous = state["records"]
            state = reduce(state, event, hints)
            links.update((record_id, int(event["seq"])) for record_id, value in state["records"].items()
                         if previous.get(record_id) is not value)
        touched = sorted({record_id for record_id, _ in links})
        prepared, blobs = await self._externalize(trajectory_id, [state["records"][record_id] for record_id in touched])
        events = [event for event, _ in batch]
        try:
            async with trace_session() as db:
                locked = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.id == trajectory_id).with_for_update())
                if (locked is None or locked.deleted_at is not None or locked.content_expired_at is not None
                        or locked.projected_seq != base):
                    return 0
                inserted = 0
                if blobs:
                    payloads, inserted = await ensure_payload_rows(db, trajectory_id, list(blobs.values()), first_seq=until)
                    if any(payloads[key].payload_id != blob["payload_id"] for key, blob in blobs.items()):
                        raise _ReferenceRace()
                await self._write_records(db, trajectory_id, prepared, links)
                await self._write_summary(db, locked, events, state, before, until)
                locked.projected_seq = until
                locked.stored_bytes = (locked.stored_bytes or 0) + inserted
        except _ReferenceRace:
            # Rolled back; the next pass serializes the records with that row's id.
            return 0
        return len(events)

    async def _externalize(self, trajectory_id: str, records: list[dict]) -> tuple[list[dict], dict[str, dict]]:
        """(records with large values as ``$ref``, their blobs by dedupe key), every new blob uploaded."""
        known: dict[str, str] = {}
        for _ in range(_REFERENCE_ROUNDS):
            externalizer = _Externalizer(trajectory_id, self.record_inline_bytes, known)
            prepared = [externalizer.record(record) for record in records]
            if not externalizer.blobs:
                return prepared, {}
            async with trace_session() as db:
                rows = await existing_payloads(db, trajectory_id, externalizer.blobs)
            stale = {key: row.payload_id for key, row in rows.items() if row.payload_id != externalizer.blobs[key]["payload_id"]}
            if not stale:
                await upload_json_blobs(self.blob_store, [blob for key, blob in externalizer.blobs.items() if key not in rows],
                                        metrics=self.metrics)
                return prepared, externalizer.blobs
            known.update(stale)
        raise CorruptContent("Record value references did not settle")

    async def _affected(self, db, trajectory_id: str, events: list[dict]) -> set[str]:
        ids, message_ids, system_keys, run_ids = set(), set(), set(), set()
        for event in events:
            if event.get("version") != 1 or event["type"] not in EVENT_TYPES:
                continue
            ids.update(record_id for record_id, _ in targets(event))
            data = event["data"] if isinstance(event["data"], dict) else {}
            if event["type"] in {"message.committed", "part.committed"} and (event.get("message_id") or data.get("message_id")):
                message_ids.add(event.get("message_id") or data.get("message_id"))
            elif event["type"] == "request.prepared":
                system_keys.add((event.get("agent_id"), event.get("source_session_id")))
            elif event["type"] == "request.finished":
                ids.add(f"assistant:{event.get('request_id')}")
            elif event["type"] in {"run.interrupted", "recording.gap"}:
                run_ids.add(event.get("run_id"))
        base = select(TrajectoryRecord.record_id, TrajectoryRecord.start_seq, TrajectoryRecord.summary).where(
            TrajectoryRecord.trajectory_id == trajectory_id)
        for chunk in _chunks(sorted(message_ids)):
            ids.update(row.summary["record_id"] for row in (await db.execute(base.where(
                TrajectoryRecord.kind == "assistant", TrajectoryRecord.message_id.in_(chunk)))).all())
        # The reducer compares a request with the latest system record of the
        # same agent_id and source_session_id (D1).
        for agent_id in {agent_id for agent_id, _ in system_keys}:
            agent = TrajectoryRecord.agent_id.is_(None) if agent_id is None else TrajectoryRecord.agent_id == agent_id
            latest = {}
            for row in (await db.execute(base.where(TrajectoryRecord.kind == "system", agent))).all():
                key = row.summary.get("source_session_id")
                if (agent_id, key) in system_keys and (key not in latest or row.start_seq > latest[key].start_seq):
                    latest[key] = row
            ids.update(row.summary["record_id"] for row in latest.values())
        # Interruptions and gaps close every non-terminal record of the run (D2).
        if run_ids:
            rows = (await db.execute(base.where(TrajectoryRecord.kind.in_(["tool", "request", "assistant", "step"]),
                                                TrajectoryRecord.status.not_in(sorted(TERMINAL))))).all()
            ids.update(row.summary["record_id"] for row in rows if row.summary.get("run_id") in run_ids)
        return {record_key(record_id) for record_id in ids}

    async def _write_records(self, db, trajectory_id: str, prepared: list[dict], links: set[tuple[str, int]]) -> None:
        values = [{"trajectory_id": trajectory_id, "record_id": record_key(record["record_id"]), "kind": record["kind"],
                   "status": str(record["status"])[:32], "agent_id": record.get("agent_id"), "message_id": record.get("message_id"),
                   "start_seq": int(record["start_seq"]), "end_seq": int(record["end_seq"]) if record.get("end_seq") else None,
                   "applied_seq": int(record["as_of_seq"]), "projector_version": PROJECTOR_VERSION, "data": record,
                   "summary": {key: value for key, value in record.items() if key not in {"data", "blocks"}},
                   "search_doc": search_doc(record)} for record in prepared]
        for chunk in _chunks(values):
            statement = _insert(db, TrajectoryRecord).values(chunk)
            columns = {name: statement.excluded[name] for name in chunk[0] if name not in {"trajectory_id", "record_id"}}
            await db.execute(statement.on_conflict_do_update(index_elements=["trajectory_id", "record_id"], set_=columns))
        pairs = [{"trajectory_id": trajectory_id, "record_id": record_key(record_id), "seq": seq} for record_id, seq in sorted(links)]
        for chunk in _chunks(pairs):
            await db.execute(_insert(db, TrajectoryRecordEvent).values(chunk).on_conflict_do_nothing(
                index_elements=["trajectory_id", "record_id", "seq"]))

    async def _write_summary(self, db, trajectory: SessionTrajectory, events: list[dict], state: dict,
                             before: dict, through: int) -> None:
        summary = await db.get(TrajectorySessionSummary, trajectory.id)
        activity = max(_instant(event["occurred_at"]) for event in events)
        if summary is None:
            summary = TrajectorySessionSummary(trajectory_id=trajectory.id, user_id=trajectory.user_id,
                session_id=trajectory.session_id, workspace_id=trajectory.workspace_id, last_activity_at=activity,
                running_status="idle", recording_status=trajectory.recording_status, model=None, applied_seq=0,
                statistics=contribution(None))
            db.add(summary)
        statistics = dict(summary.statistics or {})
        for record_id, value in state["records"].items():
            old = before.get(record_id, contribution(None))
            for key, number in contribution(value).items():
                statistics[key] = statistics.get(key, 0) + number - old[key]
        if state["unsupported_events"]:
            statistics["unsupported_events"] = [*statistics.get("unsupported_events", []), *state["unsupported_events"]]
        if state["coverage_start"] is not None:
            statistics["coverage_start"] = state["coverage_start"]
        running_status, model, gap = _summary_rules(events, summary.running_status, summary.model)
        if gap and trajectory.recording_status not in {"paused", "deleted", "expired"}:
            trajectory.recording_status = "gap"
        current = summary.last_activity_at
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        summary.last_activity_at = activity if current is None or activity > current else current
        summary.running_status, summary.model, summary.recording_status = running_status, model, trajectory.recording_status
        summary.statistics = statistics
        summary.applied_seq = through

    async def maybe_checkpoint(self, trajectory_id: str) -> bool:
        """Checkpoint at projected_seq when it is TRAJECTORY_CHECKPOINT_INTERVAL past the latest one."""
        return await build_checkpoint(trajectory_id, interval=self.checkpoint_interval, blob_store=self.blob_store,
                                      metrics=self.metrics)


async def build_checkpoint(trajectory_id: str, *, interval: int, blob_store, metrics=None) -> bool:
    """Checkpoint the fully expanded state at projected_seq in record pages of 100.

    Pages are content-addressed blobs (unchanged pages are neither uploaded nor
    stored again); uploads precede the short locking transaction.
    """
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id)
        if trajectory is None or trajectory.deleted_at is not None or trajectory.content_expired_at is not None:
            return False
        through = trajectory.projected_seq
        if through <= 0 or through - trajectory.checkpoint_seq < interval:
            return False
        if await db.get(TrajectoryCheckpoint, (trajectory_id, through)) is not None:
            return False
        state = await expanded_state(db, trajectory, through, blob_store=blob_store)
        blobs = checkpoint_blobs(trajectory_id, state)
        present = await existing_payloads(db, trajectory_id, [blob["dedupe_key"] for blob in blobs])
    await upload_json_blobs(blob_store, [blob for blob in blobs if blob["dedupe_key"] not in present], metrics=metrics)
    async with trace_session() as db:
        locked = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.id == trajectory_id).with_for_update())
        if locked is None or locked.deleted_at is not None or locked.content_expired_at is not None:
            return False
        await store_checkpoint(db, locked, state, blobs)
    return True
