"""Private-main event replay and a durable execution-to-main projection.

Public events are invalidation receipts with stable identities, never raw
Agent payloads. Current authority is checked again on every read. Execution
writers keep their existing lock order; a separate worker mirrors committed
prefixes under the main lock and checkpoints in the same transaction.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import time

from sqlalchemy import String, cast, exists, func, select

from assistant.commands import _authority, task_locked
from assistant.history import _cursor_key
from assistant.policy import AssistantError, main_session_locked, require_membership
from assistant.transactions import begin_snapshot
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantEventProjection, AssistantTask, TaskResult
from db.models.session import Session
from session.agent_event_log import append_agent_event_locked, ensure_surface_seed_locked
from session.internal_parts import _lock_fenced, begin_session_write
from session.policy import active_membership

CURSOR_TTL_SECONDS = 7 * 86400
REPLAY_WINDOW = 10000
PAGE_BYTES = 64000
DOMAIN = b"assistant-events-v1:"
SOURCE_KINDS = {
    "assistant.task.linked": "assistant.task.changed",
    "assistant.submission.accepted": "assistant.task.changed",
    "inbox.claimed": "assistant.submission.applied",
    "inbox.canceled": "assistant.task.changed",
    "assistant.execution.completed": "assistant.task.changed",
    "assistant.control.accepted": "assistant.control.changed",
    "assistant.control.observed": "assistant.control.changed",
    "assistant.control.resumed": "assistant.control.changed",
    "assistant.control.blocked": "assistant.control.changed",
    "assistant.request.changed": "assistant.request.changed",
}
PUBLIC_KINDS = {
    **{kind: kind for kind in SOURCE_KINDS.values()},
    "inbox.accepted": "assistant.turn.accepted",
    "assistant.result.accepted": "assistant.task_result.received",
    "assistant.result.processed": "assistant.result.processed",
    "assistant.report.failed": "assistant.task.changed",
    "assistant.message.committed": "assistant.message.committed",
    "turn.finished": "assistant.message.committed",
    "assistant.decision.recorded": "assistant.decision.recorded",
}
REFERENCE_FIELDS = ("origin", "task_id", "result_id", "command_id", "submission_id",
                    "inbox_id", "item_id", "message_id", "delivery_status",
                    "task_revision", "report_attempt", "request_id", "request_kind")


@dataclass(frozen=True)
class EventReferences:
    id: str
    sequence: int
    kind: str
    payload: dict


def _reference_query(db):
    """Extract only bounded metadata in SQL, including from large manifests."""
    columns = [AgentEvent.id, AgentEvent.sequence, AgentEvent.kind]
    for key in REFERENCE_FIELDS:
        if db.get_bind().dialect.name == "postgresql":
            value = func.jsonb_extract_path_text(AgentEvent.payload, key)
        else:
            value = cast(func.json_extract(AgentEvent.payload, f"$.{key}"), String)
        # One extra character lets the validator reject oversized references.
        columns.append(func.substr(value, 1, 65).label(key))
    return select(*columns)


def _references(row):
    payload = {key: row._mapping[key] for key in REFERENCE_FIELDS}
    for key in ("task_revision", "report_attempt"):
        value = payload[key]
        payload[key] = int(value) if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19 else None
    return EventReferences(row.id, row.sequence, row.kind, payload)


def event_cursor(*, user_id, workspace_id, main_id, sequence):
    raw = json.dumps({"scope": [user_id, workspace_id, main_id], "sequence": sequence,
        "expires": int(time.time()) + CURSOR_TTL_SECONDS}, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(hmac.new(_cursor_key(), DOMAIN + raw, sha256).digest() + raw).decode().rstrip("=")


def _decode(token, scope):
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError()
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        raw, signature = decoded[32:], decoded[:32]
        value = json.loads(raw)
        if not hmac.compare_digest(signature, hmac.new(_cursor_key(), DOMAIN + raw, sha256).digest()):
            raise ValueError()
        if value["scope"] != scope or value["expires"] < time.time():
            return None
        position = value["sequence"]
        if type(position) is not int or position < 0:
            raise ValueError()
        return position
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AssistantError(400, "ASSISTANT_EVENT_CURSOR", "Invalid event cursor") from None


async def _public_event(db, event, main):
    kind = PUBLIC_KINDS.get(event.kind)
    if kind is None or (event.kind == "inbox.accepted" and event.payload.get("origin") != "human"):
        return None
    value = {"schema_version": 1, "event_id": event.id, "sequence": event.sequence,
        "kind": kind, "assistant_session_id": main.id}
    task_id = event.payload.get("task_id")
    result_id = event.payload.get("result_id")
    if result_id:
        result = await db.get(TaskResult, result_id)
        if result is None:
            return {**value, "kind": "assistant.scope.changed"}
        task_id = result.task_id
    if task_id:
        try:
            await task_locked(db, user_id=main.user_id, workspace_id=main.workspace_id,
                              main_id=main.id, task_id=task_id)
        except AssistantError:
            return {**value, "kind": "assistant.scope.changed"}
        value["task_id"] = task_id
        if result_id:
            value["result_id"] = result_id
    # An immutable answer's references can become unavailable. A notification
    # still refreshes its redacted transcript, without exposing source IDs.
    for key in ("command_id", "submission_id", "inbox_id", "request_id", "request_kind"):
        item = event.payload.get(key) or (event.payload.get("item_id") if key == "inbox_id" else None)
        if isinstance(item, str) and len(item) <= 64:
            value[key] = item
    for key in ("task_revision", "report_attempt"):
        item = event.payload.get(key)
        if type(item) is int and item > 0:
            value[key] = item
    return value


async def read_events(*, user_id, workspace_id, after, limit=100):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid event page size")
    async with get_db_session() as db:
        await begin_snapshot(db)
        await require_membership(db, user_id, workspace_id)
        main = await main_session_locked(db, user_id, workspace_id)
        if main is None:
            return {"state": "snapshot_required", "reason": "main_unavailable"}
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main.id)
        position = _decode(after, [user_id, workspace_id, main.id])
        if position is None:
            return {"state": "snapshot_required", "reason": "cursor_scope_or_expiry"}
        predicate = (AgentEvent.session_id == main.id, AgentEvent.user_id == user_id)
        high_water = int(await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0)).where(*predicate)))
        if position > high_water or high_water - position > REPLAY_WINDOW:
            return {"state": "snapshot_required", "reason": "outside_replay_window"}
        if position and not await db.scalar(select(AgentEvent.id).where(*predicate, AgentEvent.sequence == position)):
            return {"state": "snapshot_required", "reason": "event_gap"}
        # Read bounded identities first: private seed/provider payloads may be
        # enormous and must not be loaded just to advance a public cursor.
        rows = (await db.execute(select(AgentEvent.id, AgentEvent.sequence, AgentEvent.kind).where(
            *predicate, AgentEvent.sequence > position, AgentEvent.sequence <= high_water)
            .order_by(AgentEvent.sequence).limit(limit))).all()
        if [row.sequence for row in rows] != list(range(position + 1, position + 1 + len(rows))):
            return {"state": "snapshot_required", "reason": "event_gap"}
        if not rows and position < high_water:
            return {"state": "snapshot_required", "reason": "event_gap"}
        public_ids = [row.id for row in rows if row.kind in PUBLIC_KINDS]
        candidates = {row.id: _references(row) for row in (await db.execute(
            _reference_query(db).where(AgentEvent.id.in_(public_ids)))).all()}
        events, size, next_sequence = [], 0, position
        for row in rows:
            event = await _public_event(db, candidates[row.id], main) if row.id in candidates else None
            encoded_size = len(json.dumps(event, ensure_ascii=False).encode()) if event else 0
            if size + encoded_size > PAGE_BYTES:
                break
            if event:
                events.append(event)
            size += encoded_size
            next_sequence = row.sequence
        return {"state": "ready", "assistant_session_id": main.id, "events": events,
            "high_water_mark": high_water, "next_sequence": next_sequence, "has_more": next_sequence < high_water,
            "next_cursor": event_cursor(user_id=user_id, workspace_id=workspace_id, main_id=main.id, sequence=next_sequence)}


async def project_task_events(task_id, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid projection page size")
    async with get_db_session() as db:
        await begin_session_write(db)
        task = await db.get(AssistantTask, task_id)
        if task is None:
            return 0
        main = await _lock_fenced(db, task.assistant_session_id, task.user_id)
        await _authority(db, user_id=task.user_id, workspace_id=task.workspace_id, main_id=main.id)
        cursor = await db.get(AssistantEventProjection, task.id)
        if cursor is None:
            cursor = AssistantEventProjection(task_id=task.id, assistant_session_id=main.id,
                source_sequence=0, updated_at=datetime.now(timezone.utc))
            db.add(cursor)
        if cursor.assistant_session_id != main.id:
            raise AssistantError(409, "ASSISTANT_EVENT_SCOPE", "Projection source changed")
        predicate = (AgentEvent.session_id == task.execution_session_id, AgentEvent.user_id == task.user_id)
        high_water = int(await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0)).where(*predicate)))
        events = [_references(row) for row in (await db.execute(_reference_query(db).where(*predicate,
            AgentEvent.sequence > cursor.source_sequence, AgentEvent.sequence <= high_water,
            AgentEvent.kind.in_(SOURCE_KINDS)).order_by(AgentEvent.sequence).limit(limit))).all()]
        if events:
            await ensure_surface_seed_locked(db, main)
        for event in events:
            if event.kind == "inbox.claimed" and event.payload.get("delivery_status"):
                # Attachment retries reuse this lifecycle kind. They do not
                # mean another Submission has been applied.
                continue
            # References only. Neither the source payload nor current Task
            # state is copied, so replay cannot overwrite a newer snapshot.
            payload = {"task_id": task.id, "source_event_id": event.id}
            for key in ("command_id", "submission_id", "inbox_id", "result_id", "task_revision", "request_id", "request_kind"):
                item = event.payload.get(key) or (event.payload.get("item_id") if key == "inbox_id" else None)
                if (isinstance(item, str) and len(item) <= 64) or (type(item) is int and item > 0):
                    payload[key] = item
            await append_agent_event_locked(db, main, kind=SOURCE_KINDS[event.kind], payload=payload,
                idempotency_key=f"assistant-projection:{event.id}")
        cursor.source_sequence = events[-1].sequence if len(events) == limit else high_water
        cursor.updated_at = datetime.now(timezone.utc)
        return len(events)


async def recover_event_projections(*, limit=20):
    async with get_db_session() as db:
        query = select(AssistantTask.id).join(Session, Session.id == AssistantTask.assistant_session_id)
        query = query.outerjoin(AssistantEventProjection, AssistantEventProjection.task_id == AssistantTask.id).where(
            Session.is_deleted.is_(False), Session.kind == "assistant", Session.visibility == "private",
            Session.memory_policy == "assistant_isolated",
            Session.user_id == AssistantTask.user_id, Session.workspace_id == AssistantTask.workspace_id,
            active_membership(AssistantTask.user_id, AssistantTask.workspace_id),
            exists(select(AgentEvent.id).where(AgentEvent.session_id == AssistantTask.execution_session_id,
                AgentEvent.user_id == AssistantTask.user_id,
                AgentEvent.sequence > func.coalesce(AssistantEventProjection.source_sequence, 0))))
        ids = list((await db.scalars(query.order_by(AssistantEventProjection.updated_at.asc().nullsfirst(),
            AssistantTask.created_at, AssistantTask.id).limit(max(1, min(limit, 100))))).all())
    count = 0
    for task_id in ids:
        try:
            count += await project_task_events(task_id)
        except (AssistantError, LookupError):
            continue
    return count
