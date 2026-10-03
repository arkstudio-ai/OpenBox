"""Persistent extraction scheduling and cross-process fencing.

The inbox finalization transaction writes a completion receipt first. A
separate, recoverable job references that immutable receipt; no provider call
or background task is needed for the principal response to finish.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import re
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from core.log import create_logger
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.memory import UserMemory
from db.models.memory_pipeline import MemoryExtractionCursor, MemoryExtractionJob, MemoryTurnCompletion, MemoryPipelineEnrollment
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from memory.source_time import canonical_user_occurrence

log = create_logger("memory.jobs")
PIPELINE_VERSION = "turn-extraction-v1"
MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 180
MAX_SOURCE_CHARS = 24000
MAX_SOURCES = 48
COMPLETE_STATES = frozenset({"SUCCEEDED", "CANCELLED"})


class ExtractionLeaseLost(RuntimeError):
    pass


class ExtractionSourceInvalid(ValueError):
    """Current permissions/transcript no longer match the frozen evidence."""


class ExtractionBaseRevisionChanged(RuntimeError):
    """A new user correction won while the model was extracting."""


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    owner: str
    generation: int
    attempts: int


@dataclass(frozen=True, slots=True)
class ExtractionInput:
    job_id: str
    user_id: str
    workspace_id: str
    project_id: str | None
    session_id: str
    logical_turn_id: str
    input_hash: str
    sources: tuple[dict, ...]
    existing_memories: tuple[dict, ...]
    base_revisions: tuple[tuple[str, int], ...]
    acl_hash: str


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def _now(db) -> datetime:
    value = await db.scalar(select(func.current_timestamp()))
    return _aware(value)


def extraction_enabled(user_id: str | None = None) -> bool:
    from core.config import get_config
    settings = getattr(get_config(), "memory", None)
    if user_id is not None and hasattr(settings, "enabled"):
        return settings.enabled("auto_extract", user_id)
    return bool(getattr(settings, "auto_extract", False))


def extraction_write_enabled(user_id: str) -> bool:
    from core.config import get_config
    settings = getattr(get_config(), "memory", None)
    return bool(settings and settings.enabled("auto_extract", user_id) and settings.enabled("v2_write", user_id))


def automatic_saving(user_id: str | None) -> bool:
    """The user's own words are saved after every turn; a model "remember" call adds nothing."""
    from core.config import get_config
    settings = getattr(get_config(), "memory", None)
    return bool(user_id and getattr(settings, "automatic_knowledge", False) and extraction_write_enabled(user_id))


async def _acl_hash(db, session: Session) -> str:
    """Bind the extraction input to current authority epochs, not just IDs."""
    from memory.policy import MemoryAccessDenied, resolve_access_scope
    try:
        await resolve_access_scope(db, user_id=session.user_id,
                                   workspace_id=session.workspace_id,
                                   project_id=session.project_id)
    except MemoryAccessDenied as exc:
        raise ExtractionSourceInvalid("scope_revoked") from exc
    user = await db.get(User, session.user_id)
    workspace = await db.get(Workspace, session.workspace_id)
    member = await db.get(WorkspaceMember, (session.workspace_id, session.user_id))
    project = await db.get(Project, session.project_id)
    return _hash({
        "user": [user.id, user.is_active, user.is_deleted, user.updated_at],
        "workspace": [workspace.id, workspace.is_deleted, workspace.updated_at],
        "member": [member.role, member.status, member.updated_at],
        "project": [project.id, project.user_id, project.workspace_id, project.is_deleted, project.updated_at],
    })


async def _canonical(db, session: Session) -> tuple[list[AgentEvent], dict[str, dict], str]:
    from session.agent_event_log import project_agent_events
    events = list((await db.scalars(select(AgentEvent).where(
        AgentEvent.session_id == session.id, AgentEvent.user_id == session.user_id,
    ).order_by(AgentEvent.sequence))).all())
    if not events:
        raise ExtractionSourceInvalid("canonical_evidence_missing")
    surface = project_agent_events(events)
    messages = {str(message["id"]): message for message in surface["messages"]}
    # Removal creates a new conversational branch. Compaction is a model-only
    # replacement and therefore does not invalidate the public evidence.
    branch = next((event.id for event in reversed(events)
                   if event.kind == "surface.messages_removed"), "root")
    return events, messages, branch


def _part_data(part: dict) -> dict:
    return part.get("data") if isinstance(part.get("data"), dict) else part


def _valid_success(message: dict | None) -> bool:
    if not message or message.get("role") != "assistant" or message.get("finish") != "stop":
        return False
    if message.get("error") or message.get("summary"):
        return False
    for part in message.get("parts") or []:
        data = _part_data(part)
        if data.get("type") == "tool" and data.get("status") in {"pending", "running", "waiting_input"}:
            return False
    return bool(message.get("structured") or any(
        _part_data(part).get("type") == "text" and str(_part_data(part).get("text") or "").strip()
        for part in message.get("parts") or []
    ))


def _user_sources(events: list[AgentEvent], messages: dict[str, dict], *,
                  message_ids: set[str], turn_id: str, branch_id: str,
                  end_sequence: int) -> list[dict]:
    from session.agent_event_log import model_excluded_message_ids
    excluded = model_excluded_message_ids(events)
    sources = []
    for message_id in sorted(message_ids):
        message = messages.get(message_id)
        if not message or message_id in excluded or message.get("role") != "user" or message.get("agent") == "compaction":
            continue
        occurred_at = canonical_user_occurrence(events, message_id, end_sequence=end_sequence,
                                                session_id=message["session_id"])
        for part in message.get("parts") or []:
            data = _part_data(part)
            if data.get("type") != "text" or data.get("synthetic") or data.get("ignored"):
                continue
            body = str(data.get("text") or "")
            if not body.strip():
                continue
            part_id = str(part["id"])
            revisions = [int(event.sequence) for event in events
                         if event.part_id == part_id and event.sequence <= end_sequence
                         and event.kind in {"part.created", "part.updated"}]
            source_revision = max(revisions, default=1)
            sources.append({
                "source_kind": "user_statement", "session_id": message["session_id"],
                "turn_id": turn_id, "branch_id": branch_id,
                "message_id": message_id, "part_id": part_id,
                "source_revision": source_revision,
                "start_seq": min(revisions, default=1), "end_seq": source_revision,
                "content_hash": _body_hash(body),
                "occurred_at": occurred_at.isoformat() if occurred_at is not None else None,
            })
    return sources


async def _create_completion_locked(db, session: Session, *, result_message_id: str,
                                    run_id: str, run_generation: int,
                                    logical_turn_id: str | None = None,
                                    inbox_rows: list[AgentInboxItem] | None = None) -> MemoryTurnCompletion | None:
    """Freeze one actual final success while the caller owns the Session row."""
    events, messages, branch_id = await _canonical(db, session)
    if not _valid_success(messages.get(result_message_id)):
        return None
    terminal = next((event for event in reversed(events)
                     if event.kind == "turn.finished" and event.message_id == result_message_id
                     and event.run_id == run_id and event.generation == run_generation), None)
    # Recovered inbox generations retain the original immutable terminal.
    if terminal is None and inbox_rows:
        terminal = next((event for event in reversed(events)
                         if event.kind == "turn.finished" and event.message_id == result_message_id), None)
    if terminal is None or (terminal.payload or {}).get("finish") != "stop" or (terminal.payload or {}).get("error"):
        return None
    turn_id = logical_turn_id or next((row.turn_id for row in inbox_rows or [] if row.turn_id), None) or terminal.turn_id
    if not turn_id or len(turn_id) > 64:
        return None
    # Select exact run/turn provenance, never the current last User Message.
    user_ids = {str(event.message_id) for event in events
                if event.kind == "message.created" and event.run_id == terminal.run_id
                and event.generation == terminal.generation
                and event.turn_id == terminal.turn_id
                and (event.payload or {}).get("message", {}).get("role") == "user"}
    user_ids.update(row.message_id for row in inbox_rows or [] if row.message_id)
    user_ids.add(str(turn_id))
    parent_id = messages[result_message_id].get("parent_id")
    if parent_id:
        user_ids.add(str(parent_id))
    boundaries = _user_sources(events, messages, message_ids=user_ids, turn_id=str(turn_id),
                               branch_id=branch_id, end_sequence=int(terminal.sequence))
    existing = await db.scalar(select(MemoryTurnCompletion).where(
        MemoryTurnCompletion.session_id == session.id, MemoryTurnCompletion.branch_id == branch_id,
        MemoryTurnCompletion.logical_turn_id == turn_id,
        MemoryTurnCompletion.result_message_id == result_message_id,
    ))
    if existing:
        return existing
    ordinal = 1 + (await db.scalar(select(func.max(MemoryTurnCompletion.ordinal)).where(
        MemoryTurnCompletion.session_id == session.id, MemoryTurnCompletion.branch_id == branch_id,
    )) or 0)
    input_hash = _hash(boundaries)
    completion_id = _hash([session.id, branch_id, turn_id, result_message_id])
    row = MemoryTurnCompletion(
        id=completion_id, user_id=session.user_id, workspace_id=session.workspace_id,
        project_id=session.project_id, session_id=session.id, branch_id=branch_id,
        logical_turn_id=str(turn_id), run_id=run_id, run_generation=run_generation,
        result_message_id=result_message_id, ordinal=ordinal,
        start_sequence=min((source["start_seq"] for source in boundaries), default=int(terminal.sequence)),
        end_sequence=int(terminal.sequence), source_boundaries=boundaries,
        input_hash=input_hash, acl_hash=await _acl_hash(db, session),
        pipeline_version=PIPELINE_VERSION, created_at=await _now(db),
    )
    db.add(row)
    await db.flush()
    return row


async def enqueue_completion_locked(db, completion: MemoryTurnCompletion, *,
                                    pipeline_version: str | None = None) -> MemoryExtractionJob:
    version = pipeline_version or completion.pipeline_version
    existing = await db.scalar(select(MemoryExtractionJob).where(
        MemoryExtractionJob.completion_id == completion.id,
        MemoryExtractionJob.pipeline_version == version,
    ))
    if existing:
        return existing
    now = await _now(db)
    key = _hash([completion.session_id, completion.branch_id, completion.logical_turn_id,
                 completion.input_hash, completion.id, version])
    job = MemoryExtractionJob(
        id=key, completion_id=completion.id, idempotency_key=key,
        user_id=completion.user_id, workspace_id=completion.workspace_id,
        project_id=completion.project_id, session_id=completion.session_id,
        branch_id=completion.branch_id, logical_turn_id=completion.logical_turn_id,
        ordinal=completion.ordinal, input_hash=completion.input_hash,
        pipeline_version=version, state="PENDING", attempts=0,
        lease_generation=0, result_memory_ids=[], usage={}, created_at=now, updated_at=now,
    )
    db.add(job)
    cursor_key = (completion.session_id, completion.branch_id, version)
    if await db.get(MemoryExtractionCursor, cursor_key) is None:
        db.add(MemoryExtractionCursor(session_id=completion.session_id, branch_id=completion.branch_id,
                                      pipeline_version=version, completed_ordinal=0,
                                      completed_sequence=0, updated_at=now))
    await db.flush()
    return job


async def record_completion_locked(db, session: Session, *, lease, result_message_id: str,
                                   inbox_rows: list[AgentInboxItem]) -> str | None:
    """Inbox calls after validating its Driver fence, before its commit.

    Scheduling failure rolls back only the job savepoint. The completion
    receipt and Inbox success still commit and the periodic scanner repairs
    the missing job after a restart.
    """
    if not extraction_enabled(session.user_id):
        return None
    if session.parent_id:
        # A delegated task or scheduled run: its "user" messages were written by
        # the parent assistant or the scheduler, never typed by the person.
        return None
    from memory.settings import saving_paused_locked
    if await saving_paused_locked(db, session.user_id, session.id):
        return None  # The person turned saving off, for this chat or for good.
    await _enroll_locked(db, session.user_id, session.workspace_id)
    driver = await db.get(AgentDriverState, lease.session_id)
    try:
        completion = await _create_completion_locked(
            db, session, result_message_id=result_message_id,
            run_id=lease.run_id, run_generation=lease.generation,
            logical_turn_id=driver.trigger_message_id if driver else None,
            inbox_rows=inbox_rows,
        )
    except ExtractionSourceInvalid:
        return None
    if completion is None:
        return None
    try:
        async with db.begin_nested():
            await enqueue_completion_locked(db, completion)
    except Exception as exc:
        # No provider input, bodies, credential-bearing errors, or stack trace.
        log.warning("Extraction scheduling deferred completion=%s error_type=%s",
                    completion.id, type(exc).__name__)
    return completion.id


async def _enroll_locked(db, user_id: str, workspace_id: str) -> None:
    key = (user_id, workspace_id, PIPELINE_VERSION)
    if await db.get(MemoryPipelineEnrollment, key) is None:
        # A dialect upsert handles concurrent first enrollment across Sessions.
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        await db.execute(insert(MemoryPipelineEnrollment).values(
            user_id=user_id, workspace_id=workspace_id,
            pipeline_version=PIPELINE_VERSION, eligible_since=await _now(db),
        ).on_conflict_do_nothing())


async def _enroll_enabled_scopes(limit: int) -> None:
    """Persist rollout start so recovery cannot silently import old history."""
    from core.config import get_config
    allowed = getattr(getattr(get_config(), "memory", None), "allowed_user_ids", [])
    async with get_db_session() as db:
        query = select(WorkspaceMember).join(User, User.id == WorkspaceMember.user_id).join(
            Workspace, Workspace.id == WorkspaceMember.workspace_id,
        ).where(WorkspaceMember.status == "active", User.is_active.is_(True), User.is_deleted.is_(False),
                Workspace.is_deleted.is_(False), ~select(MemoryPipelineEnrollment.user_id).where(
                    MemoryPipelineEnrollment.user_id == WorkspaceMember.user_id,
                    MemoryPipelineEnrollment.workspace_id == WorkspaceMember.workspace_id,
                    MemoryPipelineEnrollment.pipeline_version == PIPELINE_VERSION,
                ).exists())
        if allowed:
            query = query.where(WorkspaceMember.user_id.in_(allowed))
        members = list((await db.scalars(query.limit(limit))).all())
        for member in members:
            await _enroll_locked(db, member.user_id, member.workspace_id)


async def recover_extraction_jobs(*, limit: int = 100, include_inbox: bool = True) -> int:
    """Replay durable receipts and exact settled Inbox outcomes, bounded.

    This never guesses from a newest transcript Message. An Inbox result is a
    durable kernel settlement, and the canonical terminal and current branch
    must still prove a complete successful logical turn.
    """
    if not extraction_enabled():
        return 0
    await _enroll_enabled_scopes(limit)
    from core.config import get_config
    allowed = getattr(getattr(get_config(), "memory", None), "allowed_user_ids", [])
    repaired = 0
    async with get_db_session() as db:
        missing_query = select(MemoryTurnCompletion).where(
            MemoryTurnCompletion.pipeline_version == PIPELINE_VERSION,
            ~select(MemoryExtractionJob.id).where(
                MemoryExtractionJob.completion_id == MemoryTurnCompletion.id,
                MemoryExtractionJob.pipeline_version == MemoryTurnCompletion.pipeline_version,
            ).exists(),
        )
        if allowed:
            missing_query = missing_query.where(MemoryTurnCompletion.user_id.in_(allowed))
        missing = list((await db.scalars(missing_query.order_by(
            MemoryTurnCompletion.created_at, MemoryTurnCompletion.id).limit(limit))).all())
    for receipt in missing:
        async with get_db_session() as db:
            await db.execute(select(Session.id).where(Session.id == receipt.session_id).with_for_update())
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(update(Session).where(Session.id == receipt.session_id).values(updated_at=Session.updated_at))
            before = await db.scalar(select(MemoryExtractionJob.id).where(
                MemoryExtractionJob.completion_id == receipt.id,
                MemoryExtractionJob.pipeline_version == receipt.pipeline_version,
            ))
            await enqueue_completion_locked(db, receipt)
            repaired += int(before is None)
    if not include_inbox:
        return repaired
    async with get_db_session() as db:
        items_query = select(AgentInboxItem).join(Session, Session.id == AgentInboxItem.session_id).where(
            AgentInboxItem.state == "settled", AgentInboxItem.outcome.in_(("succeeded", "recovered")),
            AgentInboxItem.result_message_id.is_not(None), AgentInboxItem.turn_id.is_not(None),
            select(MemoryPipelineEnrollment.user_id).where(
                MemoryPipelineEnrollment.user_id == AgentInboxItem.user_id,
                MemoryPipelineEnrollment.workspace_id == Session.workspace_id,
                MemoryPipelineEnrollment.pipeline_version == PIPELINE_VERSION,
                MemoryPipelineEnrollment.eligible_since <= AgentInboxItem.settled_at,
            ).exists(),
            ~select(MemoryTurnCompletion.id).where(
                MemoryTurnCompletion.session_id == AgentInboxItem.session_id,
                MemoryTurnCompletion.logical_turn_id == AgentInboxItem.turn_id,
                MemoryTurnCompletion.result_message_id == AgentInboxItem.result_message_id,
            ).exists(),
        )
        if allowed:
            items_query = items_query.where(AgentInboxItem.user_id.in_(allowed))
        items = list((await db.scalars(items_query.order_by(AgentInboxItem.settled_at, AgentInboxItem.id).limit(limit))).all())
    for item in items:
        async with get_db_session() as db:
            session = await db.scalar(select(Session).where(
                Session.id == item.session_id, Session.user_id == item.user_id,
                Session.is_deleted.is_(False),
            ).with_for_update())
            if not session:
                continue
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(update(Session).where(Session.id == session.id).values(updated_at=Session.updated_at))
            rows = list((await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == item.session_id, AgentInboxItem.user_id == item.user_id,
                AgentInboxItem.turn_id == item.turn_id, AgentInboxItem.state == "settled",
                AgentInboxItem.result_message_id == item.result_message_id,
            ))).all())
            try:
                receipt = await _create_completion_locked(
                    db, session, result_message_id=item.result_message_id, run_id=item.run_id,
                    run_generation=item.generation, logical_turn_id=item.turn_id, inbox_rows=rows,
                )
            except ExtractionSourceInvalid:
                continue
            if receipt:
                await enqueue_completion_locked(db, receipt)
                repaired += 1
    return repaired


def _lease_conditions(lease: JobLease):
    return (
        MemoryExtractionJob.id == lease.job_id, MemoryExtractionJob.state == "RUNNING",
        MemoryExtractionJob.lease_owner == lease.owner,
        MemoryExtractionJob.lease_generation == lease.generation,
        MemoryExtractionJob.lease_until > func.current_timestamp(),
    )


async def claim_job(owner: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    max_attempts: int = MAX_ATTEMPTS,
                    allowed_user_ids: list[str] | None = None,
                    pipeline_version: str = PIPELINE_VERSION) -> JobLease | None:
    if not owner or lease_seconds < 1 or max_attempts < 1:
        raise ValueError("Invalid extraction lease")
    async with get_db_session() as db:
        now = await _now(db)
        expired = update(MemoryExtractionJob).where(
            MemoryExtractionJob.state == "RUNNING", MemoryExtractionJob.lease_until <= now,
            MemoryExtractionJob.attempts >= max_attempts,
            MemoryExtractionJob.pipeline_version == pipeline_version,
        )
        if allowed_user_ids:
            expired = expired.where(MemoryExtractionJob.user_id.in_(allowed_user_ids))
        await db.execute(expired.values(state="DEAD", lease_owner=None, lease_until=None,
                                       last_error="lease_attempts_exhausted", updated_at=now))
        due = or_(
            and_(MemoryExtractionJob.state.in_(("PENDING", "RETRY")),
                 or_(MemoryExtractionJob.next_attempt_at.is_(None), MemoryExtractionJob.next_attempt_at <= now)),
            and_(MemoryExtractionJob.state == "RUNNING", MemoryExtractionJob.lease_until <= now),
        )
        query = select(MemoryExtractionJob).where(due, MemoryExtractionJob.attempts < max_attempts,
                                                 MemoryExtractionJob.pipeline_version == pipeline_version)
        if allowed_user_ids:
            query = query.where(MemoryExtractionJob.user_id.in_(allowed_user_ids))
        rows = list((await db.scalars(query.order_by(MemoryExtractionJob.created_at, MemoryExtractionJob.ordinal, MemoryExtractionJob.id)
          .limit(8))).all())
        for row in rows:
            generation = row.lease_generation + 1
            result = await db.execute(update(MemoryExtractionJob).where(
                MemoryExtractionJob.id == row.id, due,
                MemoryExtractionJob.lease_generation == row.lease_generation,
                MemoryExtractionJob.attempts < max_attempts,
            ).values(state="RUNNING", lease_owner=owner[:160], lease_generation=generation,
                     lease_until=now + timedelta(seconds=lease_seconds), attempts=MemoryExtractionJob.attempts + 1,
                     next_attempt_at=None, updated_at=now).execution_options(synchronize_session=False))
            if result.rowcount == 1:
                return JobLease(row.id, owner[:160], generation, row.attempts + 1)
    return None


async def renew_job(lease: JobLease, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
    async with get_db_session() as db:
        now = await _now(db)
        result = await db.execute(update(MemoryExtractionJob).where(*_lease_conditions(lease)).values(
            lease_until=now + timedelta(seconds=lease_seconds), updated_at=now,
        ))
        return result.rowcount == 1


async def _advance_cursor_locked(db, job: MemoryExtractionJob, now: datetime) -> None:
    cursor = await db.get(MemoryExtractionCursor, (job.session_id, job.branch_id, job.pipeline_version), with_for_update=True)
    if cursor is None:
        return
    # Every completion ordinal has to have a successfully settled job. A DEAD
    # or RETRY boundary remains a visible hole even if later work succeeded.
    while True:
        next_job = await db.scalar(select(MemoryExtractionJob).where(
            MemoryExtractionJob.session_id == job.session_id, MemoryExtractionJob.branch_id == job.branch_id,
            MemoryExtractionJob.pipeline_version == job.pipeline_version,
            MemoryExtractionJob.ordinal == cursor.completed_ordinal + 1,
        ))
        if next_job is None or next_job.state not in COMPLETE_STATES:
            break
        receipt = await db.get(MemoryTurnCompletion, next_job.completion_id)
        cursor.completed_ordinal = next_job.ordinal
        cursor.completed_sequence = receipt.end_sequence
    cursor.updated_at = now


async def fail_job(lease: JobLease, reason: str, *, permanent: bool = False,
                   cancelled: bool = False, max_attempts: int = MAX_ATTEMPTS,
                   usage: dict | None = None) -> bool:
    # Callers pass small codes only; arbitrary provider errors never persist.
    safe_reason = "".join(ch for ch in reason[:128] if ch.isalnum() or ch in "_-:") or "extraction_error"
    async with get_db_session() as db:
        job = await db.get(MemoryExtractionJob, lease.job_id)
        if job is None:
            return False
        await db.execute(select(Session.id).where(Session.id == job.session_id).with_for_update())
        now = await _now(db)
        state = "CANCELLED" if cancelled else "DEAD" if permanent or lease.attempts >= max_attempts else "RETRY"
        delay = min(3600, 5 * 2 ** max(0, lease.attempts - 1)) * random.uniform(0.8, 1.2)
        result = await db.execute(update(MemoryExtractionJob).where(*_lease_conditions(lease)).values(
            state=state, last_error=safe_reason, lease_owner=None, lease_until=None,
            next_attempt_at=now + timedelta(seconds=delay) if state == "RETRY" else None,
            completed_at=now if state == "CANCELLED" else None, updated_at=now,
            usage=usage if usage is not None else MemoryExtractionJob.usage,
        ).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            return False
        job.state = state
        if cancelled:
            await _advance_cursor_locked(db, job, now)
        return True


async def _validate_sources_locked(db, job: MemoryExtractionJob, receipt: MemoryTurnCompletion) -> tuple[list[dict], str]:
    session = await db.get(Session, job.session_id)
    if (session is None or session.is_deleted or session.user_id != job.user_id
            or session.workspace_id != job.workspace_id or session.project_id != job.project_id):
        raise ExtractionSourceInvalid("session_unavailable")
    if session.parent_id:
        raise ExtractionSourceInvalid("delegated_session")
    acl_hash = await _acl_hash(db, session)
    if acl_hash != receipt.acl_hash:
        raise ExtractionSourceInvalid("source_acl_changed")
    events, messages, _branch = await _canonical(db, session)
    # A later regenerate or dismissal starts a new branch without touching this
    # turn; only removing this turn's own messages abandons it (checked below).
    if not _valid_success(messages.get(receipt.result_message_id)):
        raise ExtractionSourceInvalid("branch_abandoned")
    if _hash(receipt.source_boundaries) != job.input_hash:
        raise ExtractionSourceInvalid("input_boundary_changed")
    sources = []
    total_chars = 0
    for boundary in receipt.source_boundaries:
        message = messages.get(boundary["message_id"])
        part = next((part for part in (message or {}).get("parts") or [] if part["id"] == boundary["part_id"]), None)
        sql_message = await db.get(Message, boundary["message_id"])
        sql_part = await db.get(Part, boundary["part_id"])
        if (message is None or part is None or sql_message is None or sql_part is None
                or sql_message.user_id != job.user_id or sql_message.session_id != job.session_id
                or sql_part.user_id != job.user_id or sql_part.message_id != sql_message.id
                or sql_part.session_id != job.session_id or sql_message.role != "user"
                or sql_part.type != "text" or (sql_part.data or {}).get("synthetic")
                or (sql_part.data or {}).get("ignored")):
            raise ExtractionSourceInvalid("source_unavailable")
        data = _part_data(part)
        body = str(data.get("text") or "")
        if (data.get("synthetic") or data.get("ignored") or _body_hash(body) != boundary["content_hash"]
                or _body_hash(str((sql_part.data or {}).get("text") or "")) != boundary["content_hash"]):
            raise ExtractionSourceInvalid("source_revision_changed")
        current_revision = max((int(event.sequence) for event in events if event.part_id == sql_part.id
                                and event.kind in {"part.created", "part.updated"}), default=1)
        if current_revision != boundary["source_revision"]:
            raise ExtractionSourceInvalid("source_revision_changed")
        occurred_at = canonical_user_occurrence(events, boundary["message_id"],
                                                end_sequence=receipt.end_sequence,
                                                session_id=job.session_id)
        original_time = occurred_at.isoformat() if occurred_at is not None else None
        if "occurred_at" in boundary and boundary["occurred_at"] != original_time:
            raise ExtractionSourceInvalid("source_occurrence_changed")
        from db.models.memory_v2 import MemoryTombstone
        if await db.scalar(select(MemoryTombstone.id).where(
            MemoryTombstone.user_id == job.user_id, MemoryTombstone.workspace_id == job.workspace_id,
            MemoryTombstone.project_id == job.project_id if job.project_id else MemoryTombstone.project_id.is_(None),
            MemoryTombstone.source_hash == boundary["content_hash"],
        )):
            # A source-copy forget retains original chat, but that transcript
            # is no longer authorized for automatic provider ingestion.
            raise ExtractionSourceInvalid("source_forgotten")
        total_chars += len(body)
        if total_chars > MAX_SOURCE_CHARS or len(sources) >= MAX_SOURCES:
            # Never silently truncate a frozen interval. It remains a dead
            # letter for explicit bounded replay rather than a skipped cursor.
            raise OverflowError("source_budget_exceeded")
        # Older receipt hashes omit the field. Derive it from the same fully
        # checked revision without modifying their immutable boundaries/hash.
        # Keep JSON timestamps in receipts and typed UTC datetimes for SQL.
        sources.append({**deepcopy(boundary), "body": body, "occurred_at": occurred_at,
                        "source_metadata": {"pipeline_version": job.pipeline_version}})
    return sources, acl_hash


async def _check_frozen_locked(db, job: MemoryExtractionJob, frozen: ExtractionInput):
    """The frozen input still matches current authority: switch, pause, sources, access, memories."""
    from memory.policy import resolve_access_scope
    from memory.settings import saving_paused_locked
    if not extraction_write_enabled(job.user_id):
        raise ExtractionSourceInvalid("extraction_write_disabled")
    if await saving_paused_locked(db, job.user_id, job.session_id):
        raise ExtractionSourceInvalid("memory_paused")
    receipt = await db.get(MemoryTurnCompletion, job.completion_id)
    sources, acl_hash = await _validate_sources_locked(db, job, receipt)
    if frozen.job_id != job.id or frozen.input_hash != job.input_hash or frozen.acl_hash != acl_hash:
        raise ExtractionSourceInvalid("input_or_acl_changed")
    access = await resolve_access_scope(db, user_id=job.user_id, workspace_id=job.workspace_id,
                                      project_id=job.project_id)
    current_bases = tuple((str(id), int(revision)) for id, revision in (await db.execute(
        select(UserMemory.id, UserMemory.revision).where(*access.predicates(UserMemory)).order_by(UserMemory.id)
    )).all())
    if current_bases != frozen.base_revisions:
        raise ExtractionBaseRevisionChanged("memory_base_revision_changed")
    return sources, access


async def recheck_extraction_input(lease: JobLease, frozen: ExtractionInput) -> None:
    """Run before every further model call of a job, not only at commit.

    A forget, a deleted or paused chat, or lost access while one call was
    pending stops the job before more of the turn (or the memories it is
    compared with) is sent to a provider.
    """
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryExtractionJob).where(*_lease_conditions(lease)))
        if job is None:
            raise ExtractionLeaseLost("job_lease_lost")
        await _check_frozen_locked(db, job, frozen)


async def read_extraction_input(lease: JobLease) -> ExtractionInput:
    from core.config import get_config
    from memory.policy import active_memory_predicates, resolve_access_scope
    from memory.service import memory_sources_available
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryExtractionJob).where(*_lease_conditions(lease)))
        if job is None:
            raise ExtractionLeaseLost("job_lease_lost")
        if not extraction_write_enabled(job.user_id):
            raise ExtractionSourceInvalid("extraction_write_disabled")
        from memory.settings import saving_paused_locked
        if await saving_paused_locked(db, job.user_id, job.session_id):
            raise ExtractionSourceInvalid("memory_paused")
        receipt = await db.get(MemoryTurnCompletion, job.completion_id)
        sources, acl_hash = await _validate_sources_locked(db, job, receipt)
        access = await resolve_access_scope(db, user_id=job.user_id, workspace_id=job.workspace_id,
                                          project_id=job.project_id)
        memories = list((await db.scalars(select(UserMemory).where(*access.predicates(UserMemory))
                                         .order_by(UserMemory.id))).all())
        base_revisions = tuple((memory.id, int(memory.revision)) for memory in memories)
        admitted = set((await db.scalars(select(UserMemory.id).where(
            *access.predicates(UserMemory), *active_memory_predicates()))).all())
        visible = [memory for memory in memories if memory.id in admitted
                   and await memory_sources_available(db, access, memory)]
        if not get_config().memory.automatic_knowledge:
            visible.extend(memory for memory in memories if memory.status == "CANDIDATE" and memory.deleted_at is None)
        existing = tuple({"id": memory.id, "revision": memory.revision,
                          "project_id": memory.project_id,
                          "asserted_at": _aware(memory.occurred_at if memory.owner == "SYSTEM_VERIFIED"
                              and memory.occurred_at else memory.valid_from or memory.created_at).isoformat(),
                          "fact_key": memory.fact_key, "summary": (memory.value or {}).get("summary", ""),
                          "status": memory.status, "confirmation_status": memory.confirmation_status}
                         for memory in visible)[:100]
        return ExtractionInput(job.id, job.user_id, job.workspace_id, job.project_id, job.session_id,
                               job.logical_turn_id, job.input_hash, tuple(sources), existing, base_revisions, acl_hash)


async def commit_extraction(lease: JobLease, frozen: ExtractionInput, proposals: list[dict], *, usage: dict | None = None,
                            grounding: dict | None = None, reconciliation: dict | None = None) -> list[str]:
    from core.config import get_config
    from wiki_compiler.hashing import canonical_hash
    from memory.policy import MemoryAccessDenied
    from memory.service import create_candidate_in_session, lock_memory_authority
    async with get_db_session() as db:
        # Match transcript writer lock order. The UPDATE below also makes this
        # a real short SQLite write transaction before any validation/write.
        session = await db.scalar(select(Session).where(Session.id == frozen.session_id).with_for_update())
        try:
            await lock_memory_authority(db, user_id=frozen.user_id)
        except MemoryAccessDenied as exc:
            raise ExtractionSourceInvalid("scope_revoked") from exc
        fenced = await db.execute(update(MemoryExtractionJob).where(*_lease_conditions(lease)).values(
            updated_at=MemoryExtractionJob.updated_at,
        ).execution_options(synchronize_session=False))
        if fenced.rowcount != 1:
            raise ExtractionLeaseLost("job_lease_lost")
        job = await db.get(MemoryExtractionJob, lease.job_id)
        sources, access = await _check_frozen_locked(db, job, frozen)
        memory_ids = []
        consumed, separate = set(), set()
        if get_config().memory.automatic_knowledge and grounding:
            from memory.reconciliation import apply_reconciliation
            memory_ids, consumed, separate = await apply_reconciliation(
                db, access, frozen, proposals, grounding, reconciliation)
        for index, proposal in enumerate(proposals):
            if index in consumed:
                continue
            selected = [sources[source_index] for source_index in proposal["source_indexes"]]
            candidate = {
                "access": access, "type": proposal["type"], "summary": proposal["summary"],
                "confidence": proposal.get("confidence", 50), "sources": selected,
                "evidence": {"origin": "auto_extraction", "job_id": job.id,
                             "pipeline_version": job.pipeline_version,
                             "quotes": proposal["quotes"], "input_hash": job.input_hash},
                "idempotency_key": f"{job.id}:{index}",
            }
            row = await create_candidate_in_session(db, fact_key=proposal.get("fact_key"), **candidate)
            if row is not None and row.id not in memory_ids:
                if (get_config().memory.automatic_knowledge and grounding
                        and grounding.get("input_hash") == frozen.input_hash
                        and canonical_hash(proposal) in grounding.get("supported", [])):
                    from memory.grounding import admit_verified_memory
                    from memory.service import content_hash
                    if row.status == "ACTIVE" and row.content_hash != content_hash(proposal["summary"]):
                        if index not in separate:
                            # Returning the old fact is not a successful correction.
                            # Fail closed and retain the retryable completion boundary.
                            from memory.providers.common import MemoryProviderError
                            raise MemoryProviderError("memory_revision_required")
                        # A different fact that only shares the existing memory's
                        # topic key: keep both, each with its own identity.
                        row = await create_candidate_in_session(db, fact_key=(
                            f"{proposal['fact_key']}:{content_hash(proposal['summary'])[:12]}"), **candidate)
                    if row is not None:
                        await admit_verified_memory(db, access, row, job_id=job.id, proposal=proposal, sources=selected)
                if row is not None:
                    memory_ids.append(row.id)
        now = await _now(db)
        # The job row remains write-locked from the first CAS; takeover cannot
        # commit between authority writes and the final generation check.
        finished = await db.execute(update(MemoryExtractionJob).where(*_lease_conditions(lease)).values(
            state="SUCCEEDED", result_memory_ids=memory_ids, usage=usage or {},
            lease_owner=None, lease_until=None, next_attempt_at=None, last_error=None,
            completed_at=now, updated_at=now,
        ).execution_options(synchronize_session=False))
        if finished.rowcount != 1:
            raise ExtractionLeaseLost("job_lease_expired_at_commit")
        job.state = "SUCCEEDED"
        await _advance_cursor_locked(db, job, now)
        return memory_ids


async def processing_status(*, user_id: str, workspace_id: str | None, project_id: str | None = None,
                            limit: int = 20) -> dict:
    """What is still being saved, and which turns could not be, in the person's own words."""
    from db.models.memory_v2 import MemoryTombstone
    from memory.policy import resolve_access_scope
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id,
                                            include_all_projects=project_id is None)
        mine = [MemoryExtractionJob.user_id == user_id, MemoryExtractionJob.workspace_id == access.workspace_id]
        if project_id:
            mine.append(MemoryExtractionJob.project_id == project_id)
        pending = await db.scalar(select(func.count(MemoryExtractionJob.id)).where(
            *mine, MemoryExtractionJob.state.in_(("PENDING", "RUNNING", "RETRY"))))
        failed = []
        for job in (await db.scalars(select(MemoryExtractionJob).where(*mine, MemoryExtractionJob.state == "DEAD")
                                     .order_by(MemoryExtractionJob.updated_at.desc()).limit(limit))).all():
            session = await db.get(Session, job.session_id)
            receipt = await db.get(MemoryTurnCompletion, job.completion_id)
            if session is None or session.is_deleted or session.user_id != user_id or receipt is None:
                continue
            said = []
            for boundary in receipt.source_boundaries or []:
                part = await db.get(Part, boundary.get("part_id"))
                if part is not None and part.user_id == user_id and part.session_id == job.session_id:
                    said.append(str((part.data or {}).get("text") or "").strip())
            said = [text for text in said if text]
            # Nothing left to retry from, or words the person asked memory to clear.
            if not said or await db.scalar(select(MemoryTombstone.id).where(
                    MemoryTombstone.user_id == user_id, MemoryTombstone.object_kind == "source",
                    MemoryTombstone.source_hash.in_([_body_hash(text) for text in said])).limit(1)):
                continue
            # The person's own words, without numbers memory never keeps.
            from memory.redaction import mask_sensitive
            excerpt = re.sub(r"\[(?:[a-z ]+ )?redacted\]", "•••", mask_sensitive(said[0]))[:120]
            failed.append({"id": job.id, "session_id": job.session_id, "session_title": session.title,
                           "excerpt": excerpt, "failed_at": _aware(job.updated_at).isoformat()})
        return {"pending": int(pending or 0), "failed": failed}


async def dismiss_job(job_id: str, *, user_id: str, workspace_id: str) -> bool:
    """The person chose not to retry a turn that could not be saved."""
    async with get_db_session() as db:
        now = await _now(db)
        result = await db.execute(update(MemoryExtractionJob).where(
            MemoryExtractionJob.id == job_id, MemoryExtractionJob.user_id == user_id,
            MemoryExtractionJob.workspace_id == workspace_id, MemoryExtractionJob.state == "DEAD",
        ).values(state="CANCELLED", last_error="user_dismissed", lease_generation=MemoryExtractionJob.lease_generation + 1,
                 lease_owner=None, lease_until=None, next_attempt_at=None, updated_at=now))
        return result.rowcount == 1


async def replay_job(job_id: str, *, user_id: str, workspace_id: str) -> bool:
    """Explicit, scoped replay keeps the same idempotent business boundary."""
    from memory.policy import resolve_access_scope
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryExtractionJob).where(
            MemoryExtractionJob.id == job_id, MemoryExtractionJob.user_id == user_id,
            MemoryExtractionJob.workspace_id == workspace_id,
        ))
        if job is None:
            return False
        await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=job.project_id)
        now = await _now(db)
        result = await db.execute(update(MemoryExtractionJob).where(
            MemoryExtractionJob.id == job_id, MemoryExtractionJob.state.in_(("DEAD", "RETRY")),
        ).values(state="PENDING", attempts=0, lease_generation=MemoryExtractionJob.lease_generation + 1,
                 lease_owner=None, lease_until=None, next_attempt_at=None, last_error=None, updated_at=now))
        return result.rowcount == 1


async def backfill_dry_run(*, user_id: str, workspace_id: str, project_id: str | None,
                           start_at: datetime, end_at: datetime, limit: int = 50) -> list[dict]:
    """Read-only, bounded preview; it never scans all legacy chat or calls LLM."""
    from memory.policy import resolve_access_scope
    if not 1 <= limit <= 500 or not timedelta(0) < _aware(end_at) - _aware(start_at) <= timedelta(days=31):
        raise ValueError("Backfill needs a date range within 31 days and limit 1..500")
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        rows = list((await db.scalars(select(MemoryTurnCompletion).join(
            Session, Session.id == MemoryTurnCompletion.session_id).where(
            *access.predicates(MemoryTurnCompletion), MemoryTurnCompletion.created_at >= start_at,
            MemoryTurnCompletion.created_at < end_at, Session.user_id == user_id,
            Session.workspace_id == access.workspace_id, Session.project_id == MemoryTurnCompletion.project_id,
            Session.is_deleted.is_(False),
        ).order_by(MemoryTurnCompletion.created_at, MemoryTurnCompletion.id).limit(limit))).all())
        valid = []
        for row in rows:
            # A detached, never-added ORM object carries only the frozen
            # validator fields. Previewing does not create or claim a job.
            boundary = MemoryExtractionJob(session_id=row.session_id, user_id=row.user_id,
                workspace_id=row.workspace_id, project_id=row.project_id, input_hash=row.input_hash,
                pipeline_version=row.pipeline_version)
            try:
                await _validate_sources_locked(db, boundary, row)
            except (ExtractionSourceInvalid, OverflowError):
                continue
            valid.append(row)
        return [{"completion_id": row.id, "session_id": row.session_id,
                 "logical_turn_id": row.logical_turn_id, "branch_id": row.branch_id,
                 "source_count": len(row.source_boundaries), "input_hash": row.input_hash,
                 "created_at": row.created_at.isoformat()} for row in valid]
