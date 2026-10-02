"""Transactional SQL memory authority shared by HTTP, tools and workers.

Model writes remain candidates. Human commands record immutable revisions,
exact evidence and outbox together. In-session helpers never commit.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryIndexState, MemoryOutbox, MemoryRevision, MemorySource, MemorySourceLink, MemoryTombstone
from memory.policy import MemoryAccessDenied, MemoryAccessScope, POLICY_VERSION, active_memory_predicates, resolve_access_scope

MAX_SUMMARY_CHARS = 2000
PENDING_NOTE_TYPE = "PENDING_NOTE"
USER_NOTE_TYPE = "USER_NOTE"
ALLOWED_SCOPES = {"SHORT_TERM", "LONG_TERM"}
ALLOWED_OWNERS = {"USER_CONFIRMED", "SYSTEM_INFERRED", "OPERATOR_CONFIRMED"}
ALLOWED_STATUSES = {"CANDIDATE", "ACTIVE", "EXPIRED", "DEPRECATED"}


class MemoryConflict(ValueError):
    """A command is based on a stale immutable revision."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def content_hash(summary: str) -> str:
    return sha256(re.sub(r"\s+", " ", summary).strip().encode()).hexdigest()


def _summary(value: dict | None) -> str:
    summary = (value or {}).get("summary", "")
    return summary if isinstance(summary, str) else ""


def _slim(row: UserMemory) -> dict[str, Any]:
    visible = row.deleted_at is None
    return {
        "id": row.id, "user_id": row.user_id, "workspace_id": row.workspace_id,
        "project_id": row.project_id, "summary": _summary(row.value) if visible else "",
        "scope": row.scope, "type": row.type, "status": row.status,
        "confidence": row.confidence, "value": (row.value or {}) if visible else {}, "owner": row.owner,
        "revision": row.revision, "visibility": row.visibility,
        "confirmation_status": row.confirmation_status, "confirmation_state": row.confirmation_status,
        "confirmation_actor_id": row.confirmation_actor_id, "fact_key": row.fact_key if visible else None,
        "policy_version": row.policy_version,
        **{field: _utc(getattr(row, field)).isoformat() if getattr(row, field) else None
           for field in ("created_at", "updated_at", "occurred_at", "recorded_at", "valid_from", "valid_to", "deleted_at")},
        "expires_at": _utc(row.ttl).isoformat() if row.ttl else None,
    }


def _truncate_value(value: dict | None) -> dict:
    value = dict(value or {})
    if isinstance(value.get("summary"), str):
        value["summary"] = value["summary"][:MAX_SUMMARY_CHARS]
    return value


def _not_expired(now: datetime):
    return or_(UserMemory.ttl.is_(None), UserMemory.ttl > now)


def _live(row: UserMemory) -> bool:
    return row.deleted_at is None and row.status not in {"DEPRECATED", "EXPIRED"} and (
        not row.ttl or _utc(row.ttl) > _now()
    ) and (not row.valid_to or _utc(row.valid_to) > _now())


async def _resolve_workspace_id(user_id: str, workspace_id: str | None) -> str:
    async with get_db_session() as db:
        return (await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id)).workspace_id


async def lock_memory_authority(db, *, user_id: str):
    """Serialize short actor authority mutations, never model calls.

    Session transactions take Session first, then actor before job/memory/page
    locks. Ordinary commands start here. NO KEY UPDATE allows unrelated user
    FK key-share locks. SQLite requires a no-op write to obtain its write lock.
    """
    from db.models.user import User
    conditions = (User.id == user_id, User.is_active.is_(True), User.is_deleted.is_(False))
    if db.get_bind().dialect.name == "sqlite":
        result = await db.execute(update(User).where(*conditions).values(updated_at=User.updated_at)
            .execution_options(synchronize_session=False))
        present = result.rowcount == 1
    else:
        present = bool(await db.scalar(select(User.id).where(*conditions).with_for_update(key_share=True)))
    if not present:
        raise MemoryAccessDenied("Memory identity is no longer active")


async def _lock_scope(db, access):
    await lock_memory_authority(db, user_id=access.user_id)
    current = await resolve_access_scope(db, user_id=access.user_id, workspace_id=access.workspace_id,
        project_id=access.project_id, include_all_projects=access.include_all_projects)
    if current.acl_epoch != access.acl_epoch:
        raise MemoryAccessDenied("Memory access changed before this command")


async def _command_scope(db, user_id, workspace_id, *, mutation=False):
    if mutation:
        await lock_memory_authority(db, user_id=user_id)
    return await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)


async def _row_for_command(db, access, memory_id):
    return await db.scalar(select(UserMemory).where(UserMemory.id == memory_id, *access.predicates(UserMemory)).with_for_update())


async def source_is_available(db, access: MemoryAccessScope, source: MemorySource) -> bool:
    if source.user_id != access.user_id or source.workspace_id != access.workspace_id or source.visibility != "PERSONAL":
        return False
    if source.status != "ACTIVE" or source.deleted_at:
        return False
    if source.body is not None and sha256(source.body.encode()).hexdigest() != source.content_hash:
        return False
    if source.project_id and source.project_id not in access.project_ids and source.project_id != access.project_id:
        return False
    if await db.scalar(select(MemoryTombstone.id).where(MemoryTombstone.user_id == access.user_id,
        MemoryTombstone.workspace_id == access.workspace_id,
        or_(and_(MemoryTombstone.object_kind == "source", MemoryTombstone.object_id == source.id),
            MemoryTombstone.source_hash == source.content_hash))):
        return False
    if source.session_id:
        from db.models.session import Session
        session = await db.scalar(select(Session).where(Session.id == source.session_id, Session.user_id == access.user_id,
                                                      Session.workspace_id == access.workspace_id, Session.is_deleted.is_(False)))
        if session is None or session.project_id != source.project_id:
            return False
        if source.branch_id:
            from db.models.agent_event import AgentEvent
            branch = await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == source.session_id,
                AgentEvent.user_id == access.user_id, AgentEvent.kind == "surface.messages_removed").order_by(
                AgentEvent.sequence.desc()).limit(1)) or "root"
            if branch != source.branch_id:
                return False
        if source.part_id:
            from db.models.part import Part
            part = await db.scalar(select(Part).where(Part.id == source.part_id, Part.user_id == access.user_id,
                Part.session_id == source.session_id, Part.message_id == source.message_id))
            expected_hash = (source.source_metadata or {}).get("full_content_hash") or source.content_hash
            if part is None or sha256(str((part.data or {}).get("text", "")).encode()).hexdigest() != expected_hash:
                return False
    return True


async def source_body_is_available(db, access: MemoryAccessScope, source: MemorySource) -> bool:
    """A live shared source can retain provenance without exposing forgotten text.

    One utterance can support several separate facts. Forgetting one fact does
    not invalidate another confirmed summary, but its mixed original body must
    no longer be a route for bringing the forgotten fact back.
    """
    if not await source_is_available(db, access, source):
        return False
    forgotten = await db.scalar(select(MemoryTombstone.id).join(MemorySourceLink,
        MemorySourceLink.memory_id == MemoryTombstone.object_id).where(
        MemorySourceLink.source_id == source.id,
        MemorySourceLink.source_revision == source.source_revision,
        MemoryTombstone.object_kind == "memory", *access.predicates(MemoryTombstone)).limit(1))
    if forgotten is not None:
        return False
    superseded = await db.scalar(select(MemorySourceLink.id).join(UserMemory,
        UserMemory.id == MemorySourceLink.memory_id).where(
        MemorySourceLink.source_id == source.id, MemorySourceLink.source_revision == source.source_revision,
        MemorySourceLink.revision == UserMemory.revision, MemorySourceLink.relation == "SUPERSEDED",
        *access.predicates(UserMemory)).limit(1))
    return superseded is None


async def memory_sources_available(db, access: MemoryAccessScope, row: UserMemory, *, revision: int | None = None) -> bool:
    sources = (await db.scalars(select(MemorySource).join(MemorySourceLink, MemorySourceLink.source_id == MemorySource.id).where(
        MemorySourceLink.memory_id == row.id, MemorySourceLink.revision == (revision or row.revision),
        MemorySourceLink.source_revision == MemorySource.source_revision,
    ))).all()
    return all([await source_is_available(db, access, source) for source in sources])


async def read_source_in_scope(db, *, access: MemoryAccessScope, source_id: str,
                               source_revision: int | None = None) -> dict:
    """Exact tool evidence read without broadening the execution scope."""
    source = await db.scalar(select(MemorySource).where(MemorySource.id == source_id, *access.predicates(MemorySource)))
    if source is None or not await source_body_is_available(db, access, source):
        return {"available": False, "reason_code": "unavailable"}
    linked = await db.scalar(select(UserMemory.id).join(MemorySourceLink, MemorySourceLink.memory_id == UserMemory.id).where(
        MemorySourceLink.source_id == source.id, MemorySourceLink.source_revision == source.source_revision,
        MemorySourceLink.revision == UserMemory.revision, MemorySourceLink.relation == "SUPPORTS",
        *access.predicates(UserMemory), *active_memory_predicates()).limit(1))
    if not linked:
        return {"available": False, "reason_code": "unavailable"}
    if source_revision is not None and source.source_revision != source_revision:
        return {"available": False, "reason_code": "version_changed"}
    return {"available": True, "id": source.id, "source_revision": source.source_revision,
            "body": source.body, "source_kind": source.source_kind, "session_id": source.session_id,
            "message_id": source.message_id, "turn_id": source.turn_id, "content_hash": source.content_hash}


async def is_candidate_suppressed(db, access: MemoryAccessScope, *, summary: str,
                                  fact_key: str | None = None, sources: list[dict] | None = None) -> bool:
    alternatives = [MemoryTombstone.content_hash == content_hash(summary)]
    if fact_key:
        alternatives.append(MemoryTombstone.fact_key == fact_key)
    source_ids = [item.get("id") or item.get("source_id") for item in sources or []]
    source_hashes = [item.get("content_hash") or sha256(str(item.get("body", item.get("content", ""))).encode()).hexdigest()
                     for item in sources or []]
    if source_ids:
        alternatives.append(and_(MemoryTombstone.object_kind == "source", MemoryTombstone.object_id.in_(source_ids)))
    if source_hashes:
        alternatives.append(MemoryTombstone.source_hash.in_(source_hashes))
    return bool(await db.scalar(select(MemoryTombstone.id).where(
        MemoryTombstone.user_id == access.user_id, MemoryTombstone.workspace_id == access.workspace_id,
        MemoryTombstone.project_id == access.project_id if access.project_id else MemoryTombstone.project_id.is_(None),
        or_(*alternatives),
    ).limit(1)))


async def _store_source(db, access: MemoryAccessScope, data: dict) -> MemorySource:
    body = data.get("body", data.get("content", data.get("text")))
    if body is not None and not isinstance(body, str):
        raise ValueError("Source body must be text")
    if body and len(body) > 32000:
        raise ValueError("Source snapshot exceeds the bounded evidence limit")
    digest = sha256((body or "").encode()).hexdigest()
    if data.get("content_hash") and body is not None and data["content_hash"] != digest:
        raise ValueError("Source content hash does not match its immutable body")
    digest = data.get("content_hash") or digest
    version = data.get("source_revision", 1)
    if not isinstance(version, int) or version < 1:
        raise ValueError("Source revision must be a positive integer")
    source_kind = data.get("source_kind", "user_statement")
    identity = {"user_id": access.user_id, "workspace_id": access.workspace_id, "project_id": access.project_id,
                "source_kind": source_kind, "content_hash": digest, "source_revision": version,
                **{key: data.get(key) for key in ("session_id", "branch_id", "turn_id", "message_id", "part_id", "start_seq", "end_seq")}}
    source_id = data.get("id") or data.get("source_id") or "ms_" + sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:48]
    existing = await db.get(MemorySource, source_id)
    if existing:
        if existing.content_hash != digest or existing.source_revision != version or not await source_is_available(db, access, existing):
            raise MemoryAccessDenied("Source version is not available")
        return existing
    if await db.scalar(select(MemoryTombstone.id).where(MemoryTombstone.object_kind == "source", MemoryTombstone.object_id == source_id)):
        raise MemoryAccessDenied("Source version is not available")
    source = MemorySource(id=source_id, source_revision=version, user_id=access.user_id,
        workspace_id=access.workspace_id, project_id=access.project_id, source_kind=source_kind,
        content_hash=digest, body=body, status="ACTIVE", visibility="PERSONAL", acl_epoch=access.acl_epoch,
        source_metadata=dict(data.get("source_metadata") or data.get("metadata") or {}),
        occurred_at=data.get("occurred_at"), created_at=_now(),
        **{key: data.get(key) for key in ("session_id", "branch_id", "turn_id", "message_id", "part_id", "start_seq", "end_seq")})
    if source.session_id and not await source_is_available(db, access, source):
        raise MemoryAccessDenied("Source session is not available in the authorized scope")
    # A database upsert avoids a concurrent exact source insert aborting the
    # entire authority/job transaction. No existing body is ever overwritten.
    values = {column.name: getattr(source, column.name) for column in MemorySource.__table__.columns
              if getattr(source, column.name) is not None}
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    await db.execute(insert(MemorySource).values(**values).on_conflict_do_nothing(index_elements=["id"]))
    stored = await db.get(MemorySource, source_id)
    if not stored or stored.content_hash != digest or stored.source_revision != version or not await source_is_available(db, access, stored):
        raise MemoryAccessDenied("Source version is not available")
    return stored


async def _revision(db, row, *, reason, actor_user_id, sources=None, prior_revision=None, request_id=None):
    versions = {}
    if prior_revision:
        links = (await db.scalars(select(MemorySourceLink).where(MemorySourceLink.memory_id == row.id,
                                                               MemorySourceLink.revision == prior_revision))).all()
        versions.update({link.source_id: (link.source_revision,
            "SUPERSEDED" if reason in {"user_corrected", "user_confirmed_edited"} else link.relation) for link in links})
    versions.update({source.id: (source.source_revision, "SUPPORTS") for source in sources or []})
    db.add(MemoryRevision(id=ascending("memory_revision"), memory_id=row.id, revision=row.revision,
        user_id=row.user_id, workspace_id=row.workspace_id, project_id=row.project_id,
        value=dict(row.value or {}), status=row.status, confirmation_status=row.confirmation_status,
        content_hash=row.content_hash, reason=reason, actor_user_id=actor_user_id,
        source_set_hash=sha256(json.dumps(sorted(versions.items()), sort_keys=True).encode()).hexdigest(),
        request_id=request_id, valid_from=row.valid_from, valid_to=row.valid_to, created_at=_now()))
    for source_id, (source_revision, relation) in versions.items():
        db.add(MemorySourceLink(id=ascending("memory_link"), memory_id=row.id, revision=row.revision,
                               source_id=source_id, source_revision=source_revision, relation=relation))
    await db.flush()


async def enqueue_memory_outbox(db, row, operation="UPSERT"):
    if operation in {"DELETE", "REVOKE"}:
        # An already superseded, unclaimed write must not hold cleanup open
        # after rollout is disabled. Live workers retain their lease fence.
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.object_kind == "memory",
            MemoryOutbox.object_id == row.id, MemoryOutbox.operation == "UPSERT",
            MemoryOutbox.revision <= row.revision, MemoryOutbox.status.in_(("PENDING", "RETRY"))).values(
                status="CANCELLED", last_error="memory_no_longer_eligible", updated_at=_now())
            .execution_options(synchronize_session=False))
    from memory.wiki.service import invalidate_memory_dependencies
    await invalidate_memory_dependencies(db, memory_ids=[row.id], reason="memory_changed")
    # A shared original can contain the forgotten/corrected fact alongside
    # other facts. Stop Wiki derivatives that cite that mixed original too.
    links = select(MemorySourceLink.source_id).where(MemorySourceLink.memory_id == row.id)
    if not row.deleted_at:
        links = links.where(MemorySourceLink.revision == row.revision, MemorySourceLink.relation == "SUPERSEDED")
    unavailable_bodies = list((await db.scalars(links)).unique().all())
    if unavailable_bodies:
        await invalidate_memory_dependencies(db, source_ids=unavailable_bodies, reason="source_body_suppressed")
    now = _now()
    generations = list((await db.scalars(select(MemoryIndexState.index_generation).where(
        MemoryIndexState.object_kind == "memory", MemoryIndexState.object_id == row.id,
    ))).all())
    from core.config import get_config
    current_generation = get_config().memory.index_generation
    if current_generation not in generations:
        generations.append(current_generation)
    for generation in generations:
        from memory.redaction import json_hash
        event_id = "memory:" + json_hash([row.id, row.revision, operation, generation])
        if not await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.event_id == event_id)):
            db.add(MemoryOutbox(id=ascending("memory_outbox"), event_id=event_id,
                user_id=row.user_id, workspace_id=row.workspace_id, project_id=row.project_id,
                object_kind="memory", object_id=row.id, revision=row.revision, operation=operation,
                index_generation=generation, priority=100 if operation != "UPSERT" else 0,
                payload={}, status="PENDING", attempts=0, lease_generation=0,
                available_at=now, created_at=now, updated_at=now))
        state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_kind == "memory",
            MemoryIndexState.object_id == row.id, MemoryIndexState.index_generation == generation).with_for_update())
        if state is None:
            db.add(MemoryIndexState(id=ascending("memory_index"), object_kind="memory", object_id=row.id,
                index_generation=generation, user_id=row.user_id, workspace_id=row.workspace_id, project_id=row.project_id,
                desired_revision=row.revision, indexed_revision=0, chunk_ids=[], status="PENDING", updated_at=now))
        else:
            state.desired_revision = row.revision
            state.status = "DELETE_PENDING" if operation != "UPSERT" else "PENDING"
            state.updated_at = now
    await db.flush()


async def create_candidate_in_session(db, *, access: MemoryAccessScope, type: str, summary: str,
    sources: list[dict] | None = None, fact_key: str | None = None, confidence: int = 50,
    evidence: dict | None = None, ttl_seconds: int | None = None, occurred_at: datetime | None = None,
    idempotency_key: str | None = None, scope: str = "LONG_TERM", value: dict | None = None) -> UserMemory | None:
    await _lock_scope(db, access)
    if scope not in ALLOWED_SCOPES or not type or len(type) > 32:
        raise ValueError("Invalid memory scope or type")
    if ttl_seconds is not None and ttl_seconds <= 0:
        raise ValueError("Memory TTL must be positive")
    summary = summary[:MAX_SUMMARY_CHARS]
    if not summary.strip():
        raise ValueError("A memory summary is required")
    if await is_candidate_suppressed(db, access, summary=summary, fact_key=fact_key, sources=sources):
        return None
    digest = content_hash(summary)
    fact_identity = sha256(f"{access.user_id}|{access.workspace_id}|{access.project_id or ''}|{fact_key}".encode()).hexdigest() if fact_key else None
    existing = await db.scalar(select(UserMemory).where(
        UserMemory.user_id == access.user_id, UserMemory.workspace_id == access.workspace_id,
        UserMemory.project_id == access.project_id if access.project_id else UserMemory.project_id.is_(None),
        UserMemory.deleted_at.is_(None), UserMemory.status.in_(("CANDIDATE", "ACTIVE")),
        or_(UserMemory.fact_identity == fact_identity if fact_identity else UserMemory.id == "",
            UserMemory.fact_key == fact_key if fact_key else UserMemory.id == "", UserMemory.content_hash == digest),
    ).limit(1))
    if existing:
        return existing  # Inference never overwrites an explicit correction.
    memory_id = "mem_" + sha256(f"{access.user_id}|{access.workspace_id}|{idempotency_key}".encode()).hexdigest()[:48] if idempotency_key else ascending("memory")
    if idempotency_key:
        previous = await db.get(UserMemory, memory_id)
        if previous:
            return previous if previous.user_id == access.user_id and previous.workspace_id == access.workspace_id else None
    now = _now()
    source_rows = [await _store_source(db, access, item) for item in sources or []]
    row = UserMemory(id=memory_id, user_id=access.user_id, workspace_id=access.workspace_id, project_id=access.project_id,
        scope=scope, type=type, value=_truncate_value(value or {"summary": summary}), evidence=evidence or {},
        owner="SYSTEM_INFERRED", confidence=max(0, min(100, confidence)), status="CANDIDATE",
        ttl=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        revision=1, visibility="PERSONAL", confirmation_status="PENDING", fact_key=fact_key,
        fact_identity=fact_identity, content_hash=digest, recorded_at=now, occurred_at=occurred_at,
        policy_version=POLICY_VERSION, acl_epoch=access.acl_epoch, created_at=now, updated_at=now)
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    values = {column.name: getattr(row, column.name) for column in UserMemory.__table__.columns
              if getattr(row, column.name) is not None}
    created = (await db.execute(insert(UserMemory).values(**values).on_conflict_do_nothing().returning(UserMemory.id))).scalar_one_or_none()
    if not created:
        return await db.scalar(select(UserMemory).where(
            *access.predicates(UserMemory), or_(UserMemory.id == memory_id,
                UserMemory.fact_identity == fact_identity if fact_identity else UserMemory.id == "")))
    row = await db.get(UserMemory, created)
    await _revision(db, row, reason="automatic_candidate", actor_user_id=None, sources=source_rows)
    await enqueue_memory_outbox(db, row)
    return row


async def _session_sources(db, access, session_id):
    if not session_id:
        return []
    from db.models.session import Session
    from db.models.message import Message
    from db.models.part import Part
    from db.models.agent_event import AgentEvent
    session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == access.user_id,
                                                   Session.workspace_id == access.workspace_id, Session.is_deleted.is_(False)))
    if not session or session.project_id != access.project_id:
        raise MemoryAccessDenied("Source session is not available in the authorized scope")
    message = await db.scalar(select(Message).where(Message.session_id == session_id, Message.user_id == access.user_id,
                                                  Message.role == "user").order_by(Message.created_at.desc(), Message.id.desc()).limit(1))
    if not message:
        return []
    parts = (await db.scalars(select(Part).where(Part.message_id == message.id, Part.user_id == access.user_id,
                                               Part.type == "text").order_by(Part.id))).all()
    branch = await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == session_id, AgentEvent.user_id == access.user_id,
        AgentEvent.kind == "surface.messages_removed").order_by(AgentEvent.sequence.desc()).limit(1)) or "root"
    sources = []
    for part in parts:
        data = part.data or {}
        body = data.get("text", "")
        if not isinstance(body, str) or not body.strip() or data.get("synthetic") or data.get("ignored"):
            continue
        version = await db.scalar(select(AgentEvent.sequence).where(AgentEvent.session_id == session_id,
            AgentEvent.user_id == access.user_id, AgentEvent.part_id == part.id,
            AgentEvent.kind.in_(("part.created", "part.updated"))).order_by(AgentEvent.sequence.desc()).limit(1)) or 1
        sources.append({"source_kind": "user_statement", "session_id": session_id, "message_id": message.id,
            "turn_id": message.id, "branch_id": branch, "part_id": part.id, "source_revision": version,
            "body": body[:32000], "source_metadata": {"span_start": 0, "span_end": min(len(body), 32000),
                                                       "full_content_hash": sha256(body.encode()).hexdigest()}})
    return sources


async def write_memory(*, user_id, workspace_id=None, project_id=None, scope, type, value, owner,
                       confidence=50, evidence=None, ttl_seconds=None):
    if owner not in ALLOWED_OWNERS:
        raise ValueError(f"owner must be one of {sorted(ALLOWED_OWNERS)}")
    if type in {PENDING_NOTE_TYPE, USER_NOTE_TYPE}:
        raise ValueError(f"{type} must go through propose_note, not write_memory")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        row = await create_candidate_in_session(db, access=access, type=type, summary=_summary(value),
            sources=await _session_sources(db, access, (evidence or {}).get("session_id")),
            scope=scope, value=value, confidence=confidence, evidence=evidence, ttl_seconds=ttl_seconds)
        return _slim(row) if row else {"status": "SUPPRESSED", "confirmation_status": "REJECTED", "value": {}}


async def search_memories(*, user_id, workspace_id=None, project_id=None, type=None, scope=None,
                         status=None, confirmation_status=None, limit=20, include_candidates=False,
                         include_all_projects=False):
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
                                          project_id=project_id, include_all_projects=include_all_projects)
        stmt = select(UserMemory).where(*access.predicates(UserMemory))
        if status:
            stmt = stmt.where(UserMemory.status == status)
            if status == "ACTIVE":
                stmt = stmt.where(*active_memory_predicates())
            elif status == "CANDIDATE":
                stmt = stmt.where(UserMemory.deleted_at.is_(None), _not_expired(_now()))
        elif include_candidates:
            stmt = stmt.where(UserMemory.deleted_at.is_(None), UserMemory.status.in_(("ACTIVE", "CANDIDATE")), _not_expired(_now()))
        else:
            stmt = stmt.where(*active_memory_predicates())
        if type:
            stmt = stmt.where(UserMemory.type == type)
        if scope:
            stmt = stmt.where(UserMemory.scope == scope)
        if confirmation_status:
            stmt = stmt.where(UserMemory.confirmation_status == confirmation_status)
        rows = (await db.scalars(stmt.order_by(UserMemory.confidence.desc(), UserMemory.updated_at.desc()).limit(max(1, min(limit, 100))))).all()
        return [_slim(row) for row in rows if row.deleted_at or await memory_sources_available(db, access, row)]


async def list_active_memories(*, user_id, workspace_id=None, project_id=None):
    return await search_memories(user_id=user_id, workspace_id=workspace_id, project_id=project_id, status="ACTIVE", limit=100)


async def propose_note(*, user_id, workspace_id=None, project_id=None, summary, session_id=None):
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        row = await create_candidate_in_session(db, access=access, type=PENDING_NOTE_TYPE, summary=summary,
            sources=await _session_sources(db, access, session_id), confidence=30,
            evidence={"source": "chat", "session_id": session_id, "awaiting_confirm": True})
        if row is None:
            raise ValueError("This fact was rejected or forgotten; it will not be proposed again")
        return _slim(row)


async def _assert_command(db, row, expected_revision, request_id):
    if request_id and await db.scalar(select(MemoryRevision.id).where(MemoryRevision.memory_id == row.id,
                                                                   MemoryRevision.request_id == request_id)):
        return False
    if expected_revision is not None and row.revision != expected_revision:
        raise MemoryConflict("Memory changed; reload its current revision before submitting this command")
    return True


async def _cas(db, row, values):
    old_revision = row.revision
    result = await db.execute(update(UserMemory).where(UserMemory.id == row.id, UserMemory.user_id == row.user_id,
        UserMemory.workspace_id == row.workspace_id, UserMemory.revision == old_revision).values(
        **values, revision=old_revision + 1, updated_at=_now()).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise MemoryConflict("Memory changed while this command was being committed")
    await db.refresh(row)
    return old_revision


async def confirm_note_in_session(db, *, access, proposal_id, edited_summary=None, expected_revision=None, request_id=None):
    await _lock_scope(db, access)
    row = await _row_for_command(db, access, proposal_id)
    if row is None:
        return None
    if not await _assert_command(db, row, expected_revision, request_id):
        return row
    if row.status != "CANDIDATE" or not _live(row):
        return None
    if not await memory_sources_available(db, access, row):
        raise MemoryConflict("Proposal sources are no longer available; request fresh confirmation")
    if await db.scalar(select(MemoryTombstone.id).where(MemoryTombstone.object_kind == "memory", MemoryTombstone.object_id == row.id)):
        return None
    summary = edited_summary if edited_summary is not None else _summary(row.value)
    if not summary.strip():
        raise ValueError("A confirmed memory requires a summary")
    direct_scope = MemoryAccessScope(access.user_id, access.workspace_id, row.project_id, project_ids=access.project_ids, acl_epoch=access.acl_epoch)
    source_id = "ms_" + sha256(f"confirmation|{row.id}|{row.revision}|{summary[:MAX_SUMMARY_CHARS]}".encode()).hexdigest()[:48]
    source = await _store_source(db, direct_scope, {"id": source_id, "source_kind": "user_confirmation", "body": summary[:MAX_SUMMARY_CHARS],
                                                   "source_metadata": {"proposal_id": row.id, "base_revision": row.revision}})
    changed = summary[:MAX_SUMMARY_CHARS] != _summary(row.value)
    prior = await _cas(db, row, {"value": _truncate_value({**row.value, "summary": summary}),
        "type": USER_NOTE_TYPE if row.type == PENDING_NOTE_TYPE else row.type,
        "owner": "USER_CONFIRMED", "status": "ACTIVE", "confirmation_status": "CONFIRMED",
        "confirmation_actor_id": access.user_id, "confidence": 90, "content_hash": content_hash(summary[:MAX_SUMMARY_CHARS]),
        "valid_from": _now(), "evidence": {**(row.evidence or {}), "awaiting_confirm": False}})
    await _revision(db, row, reason="user_confirmed_edited" if changed else "user_confirmed", actor_user_id=access.user_id,
                    sources=[source], prior_revision=prior, request_id=request_id)
    await enqueue_memory_outbox(db, row)
    return row


async def confirm_note(*, user_id, workspace_id=None, proposal_id=None, edited_summary=None, expected_revision=None, request_id=None):
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id, mutation=True)
        if proposal_id is None:
            proposal_id = await db.scalar(select(UserMemory.id).where(*access.predicates(UserMemory),
                UserMemory.status == "CANDIDATE", UserMemory.type == PENDING_NOTE_TYPE, _not_expired(_now()),
                UserMemory.deleted_at.is_(None)).order_by(UserMemory.created_at.desc()).limit(1))
        if not proposal_id:
            return None
        row = await confirm_note_in_session(db, access=access, proposal_id=proposal_id, edited_summary=edited_summary,
                                          expected_revision=expected_revision, request_id=request_id)
        return _slim(row) if row else None


async def _tombstone(db, row, scope="MEMORY"):
    if not await db.scalar(select(MemoryTombstone.id).where(MemoryTombstone.object_kind == "memory", MemoryTombstone.object_id == row.id)):
        db.add(MemoryTombstone(id=ascending("memory_tombstone"), object_kind="memory", object_id=row.id,
            revision=row.revision, user_id=row.user_id, workspace_id=row.workspace_id, project_id=row.project_id,
            content_hash=row.content_hash or content_hash(_summary(row.value)), fact_key=row.fact_key,
            scope=scope, purge_status="PENDING", deleted_at=_now()))
    await db.flush()


async def reject_note_in_session(db, *, access, proposal_id, expected_revision=None, request_id=None):
    await _lock_scope(db, access)
    row = await _row_for_command(db, access, proposal_id)
    if row is None:
        return False
    if not await _assert_command(db, row, expected_revision, request_id):
        return True
    if row.status != "CANDIDATE" or not _live(row):
        return False
    prior = await _cas(db, row, {"status": "DEPRECATED", "confirmation_status": "REJECTED", "deleted_at": _now(),
                               "valid_to": _now(), "fact_identity": None})
    await _revision(db, row, reason="user_rejected", actor_user_id=access.user_id, prior_revision=prior, request_id=request_id)
    await _tombstone(db, row, scope="FACT")
    await enqueue_memory_outbox(db, row, "DELETE")
    return True


async def reject_note(*, user_id, workspace_id=None, proposal_id=None, expected_revision=None, request_id=None):
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id, mutation=True)
        if proposal_id is None:
            proposal_id = await db.scalar(select(UserMemory.id).where(*access.predicates(UserMemory),
                UserMemory.status == "CANDIDATE", UserMemory.type == PENDING_NOTE_TYPE).order_by(UserMemory.created_at.desc()).limit(1))
        return await reject_note_in_session(db, access=access, proposal_id=proposal_id,
                                           expected_revision=expected_revision, request_id=request_id) if proposal_id else False


async def create_note(*, user_id, workspace_id=None, project_id=None, summary, request_id=None, fact_key=None):
    if not summary.strip():
        raise ValueError("A memory summary is required")
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        memory_id = "mem_" + sha256(f"manual|{user_id}|{access.workspace_id}|{request_id}".encode()).hexdigest()[:48] if request_id else ascending("memory")
        old = await db.get(UserMemory, memory_id)
        if old:
            if old.project_id != project_id or _summary(old.value) != summary[:MAX_SUMMARY_CHARS]:
                raise MemoryConflict("This request ID was already used for a different memory")
            return _slim(old)
        now = _now()
        summary = summary[:MAX_SUMMARY_CHARS]
        source_id = "ms_" + sha256(f"manual|{memory_id}|{summary}".encode()).hexdigest()[:48]
        source = await _store_source(db, access, {"id": source_id, "source_kind": "manual", "body": summary})
        row = UserMemory(id=memory_id, user_id=user_id, workspace_id=access.workspace_id, project_id=project_id,
            scope="LONG_TERM", type=USER_NOTE_TYPE, value={"summary": summary}, evidence={"source": "manual"},
            confidence=90, owner="USER_CONFIRMED", status="ACTIVE", revision=1, visibility="PERSONAL",
            confirmation_status="CONFIRMED", confirmation_actor_id=user_id, fact_key=fact_key,
            fact_identity=sha256(f"{user_id}|{access.workspace_id}|{project_id or ''}|{fact_key}".encode()).hexdigest() if fact_key else None,
            content_hash=content_hash(summary), recorded_at=now, valid_from=now, policy_version=POLICY_VERSION,
            acl_epoch=access.acl_epoch, created_at=now, updated_at=now)
        insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
        values = {column.name: getattr(row, column.name) for column in UserMemory.__table__.columns
                  if getattr(row, column.name) is not None}
        created = (await db.execute(insert(UserMemory).values(**values).on_conflict_do_nothing().returning(UserMemory.id))).scalar_one_or_none()
        if not created:
            existing = await db.scalar(select(UserMemory).where(*access.predicates(UserMemory),
                or_(UserMemory.id == memory_id, UserMemory.fact_identity == row.fact_identity if row.fact_identity else UserMemory.id == "")))
            if existing is None or _summary(existing.value) != summary:
                raise MemoryConflict("This fact or request ID already exists; use a versioned correction")
            return _slim(existing)
        row = await db.get(UserMemory, created)
        await _revision(db, row, reason="manual_created", actor_user_id=user_id, sources=[source], request_id=request_id)
        await enqueue_memory_outbox(db, row)
        return _slim(row)


async def edit_note(*, user_id, workspace_id=None, memory_id, summary, expected_revision=None, request_id=None):
    if not summary.strip():
        raise ValueError("A memory summary is required")
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id, mutation=True)
        row = await _row_for_command(db, access, memory_id)
        if row is None:
            return None
        if not await _assert_command(db, row, expected_revision, request_id):
            return _slim(row)
        if row.status != "ACTIVE" or row.confirmation_status != "CONFIRMED" or not _live(row):
            return None
        direct_scope = MemoryAccessScope(access.user_id, access.workspace_id, row.project_id, project_ids=access.project_ids, acl_epoch=access.acl_epoch)
        summary = summary[:MAX_SUMMARY_CHARS]
        source_id = "ms_" + sha256(f"correction|{row.id}|{row.revision}|{summary}".encode()).hexdigest()[:48]
        source = await _store_source(db, direct_scope, {"id": source_id, "source_kind": "user_correction", "body": summary,
            "source_metadata": {"memory_id": row.id, "base_revision": row.revision}})
        old_hash, old_revision = row.content_hash, row.revision
        prior = await _cas(db, row, {"value": _truncate_value({**(row.value or {}), "summary": summary}),
            "owner": "USER_CONFIRMED", "confirmation_status": "CONFIRMED", "confirmation_actor_id": user_id,
            "content_hash": content_hash(summary), "valid_from": _now()})
        if old_hash != row.content_hash:
            db.add(MemoryTombstone(id=ascending("memory_tombstone"), object_kind="superseded", object_id=f"{row.id}:{old_revision}",
                revision=row.revision, user_id=row.user_id, workspace_id=row.workspace_id, project_id=row.project_id,
                content_hash=old_hash, scope="FACT", purge_status="SUCCEEDED", deleted_at=_now()))
        await _revision(db, row, reason="user_corrected", actor_user_id=user_id, sources=[source], prior_revision=prior, request_id=request_id)
        await enqueue_memory_outbox(db, row)
        return _slim(row)


async def _forget_in_session(db, access, row, *, expected_revision=None, request_id=None, reason="user_forgotten"):
    if not await _assert_command(db, row, expected_revision, request_id):
        return True
    if row.deleted_at or row.status == "DEPRECATED":
        return False
    prior = await _cas(db, row, {"status": "DEPRECATED", "deleted_at": _now(), "valid_to": _now(), "fact_identity": None})
    await _revision(db, row, reason=reason, actor_user_id=access.user_id, prior_revision=prior, request_id=request_id)
    await _tombstone(db, row, scope="FACT")
    await enqueue_memory_outbox(db, row, "DELETE")
    return True


async def delete_memory(*, user_id, workspace_id=None, memory_id, expected_revision=None, request_id=None):
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id, mutation=True)
        row = await _row_for_command(db, access, memory_id)
        return await _forget_in_session(db, access, row, expected_revision=expected_revision, request_id=request_id) if row else False


async def forget_memory(*, user_id, workspace_id=None, memory_id, expected_revision=None, request_id=None,
                        mode="memory", source_ids=None):
    if mode not in {"memory", "sources"}:
        raise ValueError("Forget mode must be memory or sources")
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id, mutation=True)
        row = await _row_for_command(db, access, memory_id)
        if row is None:
            return None
        if not await _assert_command(db, row, expected_revision, request_id):
            return {"ok": True, "status": "stopped_cleanup_pending", "memory_ids": [row.id], "source_ids": [], "original_chat_deleted": False}
        selected = []
        if mode == "sources":
            if not source_ids or len(source_ids) > 100:
                raise ValueError("Select the exact source IDs to clear (1–100)")
            linked = (await db.scalars(select(MemorySource).join(MemorySourceLink, MemorySourceLink.source_id == MemorySource.id).where(
                MemorySourceLink.memory_id == row.id, *access.predicates(MemorySource)))).unique().all()
            selected = [source for source in linked if source.id in set(source_ids)]
            if set(source_ids) != {source.id for source in selected}:
                raise MemoryAccessDenied("Selected sources are not available on this memory")
        affected = {row.id: row}
        for source in selected:
            dependants = (await db.scalars(select(UserMemory).join(MemorySourceLink, MemorySourceLink.memory_id == UserMemory.id).where(
                MemorySourceLink.source_id == source.id, *access.predicates(UserMemory)))).unique().all()
            affected.update({memory.id: memory for memory in dependants})
        for memory in affected.values():
            await _forget_in_session(db, access, memory, expected_revision=expected_revision if memory.id == row.id else None,
                request_id=request_id if memory.id == row.id else None, reason="source_forgotten" if selected else "user_forgotten")
        for source in selected:
            if source.deleted_at:
                continue
            source.status, source.deleted_at, source.body = "DELETED", _now(), None
            source.source_metadata = {}
            source.acl_epoch += 1
            from memory.wiki.service import invalidate_memory_dependencies
            await invalidate_memory_dependencies(db, source_ids=[source.id], reason="source_forgotten")
            db.add(MemoryTombstone(id=ascending("source_tombstone"), object_kind="source", object_id=source.id,
                revision=source.source_revision, user_id=source.user_id, workspace_id=source.workspace_id, project_id=source.project_id,
                source_hash=source.content_hash, scope="SOURCE", purge_status="PENDING", deleted_at=_now()))
            await db.execute(update(MemoryRevision).where(MemoryRevision.memory_id.in_(list(affected))).values(value={}))
            await db.execute(update(UserMemory).where(UserMemory.id.in_(list(affected))).values(value={}, evidence={}))
            now = _now()
            from core.config import get_config
            from memory.redaction import json_hash
            generations = set((await db.scalars(select(MemoryIndexState.index_generation).where(
                MemoryIndexState.object_kind == "source", MemoryIndexState.object_id == source.id))).all())
            generations.add(get_config().memory.index_generation)
            for generation in generations:
                event_id = "source:" + json_hash([source.id, source.source_revision, "DELETE", generation])
                if not await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.event_id == event_id)):
                    db.add(MemoryOutbox(id=ascending("memory_outbox"), event_id=event_id, user_id=source.user_id,
                        workspace_id=source.workspace_id, project_id=source.project_id, object_kind="source", object_id=source.id,
                        revision=source.source_revision, operation="DELETE", index_generation=generation, priority=100,
                        status="PENDING", attempts=0, lease_generation=0, available_at=now, payload={}, created_at=now, updated_at=now))
        await db.flush()
        return {"ok": True, "status": "stopped_cleanup_pending", "memory_ids": list(affected),
                "source_ids": [source.id for source in selected], "original_chat_deleted": False}


async def get_history(*, user_id, workspace_id=None, memory_id):
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id)
        row = await _row_for_command(db, access, memory_id)
        if row is None:
            return None
        versions = (await db.scalars(select(MemoryRevision).where(MemoryRevision.memory_id == memory_id,
            *access.predicates(MemoryRevision)).order_by(MemoryRevision.revision.desc()))).all()
        items = []
        for version in versions:
            available = row.deleted_at is None and await memory_sources_available(db, access, row, revision=version.revision)
            items.append({"revision": version.revision, "summary": _summary(version.value) if available else None,
                "value": version.value if available else {}, "status": version.status, "confirmation_status": version.confirmation_status,
                "reason": version.reason, "actor_user_id": version.actor_user_id, "created_at": _utc(version.created_at).isoformat(),
                "body_available": available})
        return items


async def get_sources(*, user_id, workspace_id=None, memory_id):
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id)
        row = await _row_for_command(db, access, memory_id)
        if row is None:
            return None
        sources = (await db.scalars(select(MemorySource).join(MemorySourceLink, MemorySourceLink.source_id == MemorySource.id).where(
            MemorySourceLink.memory_id == memory_id, *access.predicates(MemorySource)).order_by(MemorySource.created_at))).unique().all()
        items = []
        for source in sources:
            available = row.deleted_at is None and await source_body_is_available(db, access, source)
            items.append({"id": source.id, "source_revision": source.source_revision, "revision": source.source_revision,
                "source_kind": source.source_kind, "session_id": source.session_id if available else None,
                "turn_id": source.turn_id if available else None, "message_id": source.message_id if available else None,
                "body": source.body if available else None, "content_hash": source.content_hash if available else None,
                "body_available": available, "status": source.status, "created_at": _utc(source.created_at).isoformat()})
        return items


async def cleanup_status(*, user_id, workspace_id=None, memory_id):
    async with get_db_session() as db:
        access = await _command_scope(db, user_id, workspace_id)
        row = await _row_for_command(db, access, memory_id)
        if row is None:
            return None
        tombstone = await db.scalar(select(MemoryTombstone).where(MemoryTombstone.object_kind == "memory", MemoryTombstone.object_id == row.id))
        targets = [("memory", row.id)]
        linked_sources = list((await db.scalars(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == row.id,
                *access.predicates(MemorySource)))).unique().all())
        retained_shared = 0
        for source in linked_sources:
            source_id = source.id
            shared = await db.scalar(select(UserMemory.id).join(MemorySourceLink, MemorySourceLink.memory_id == UserMemory.id).where(
                MemorySourceLink.source_id == source_id, MemorySourceLink.revision == UserMemory.revision,
                MemorySourceLink.relation == "SUPPORTS", UserMemory.id != row.id,
                *access.predicates(UserMemory), *active_memory_predicates()).limit(1))
            if shared:
                retained_shared += 1
            if not shared or not await source_body_is_available(db, access, source):
                targets.append(("source", source_id))
        from db.models.memory_wiki import MemoryWikiDependency, MemoryWikiPage
        pages = list((await db.scalars(select(MemoryWikiPage.id).join(MemoryWikiDependency,
            MemoryWikiDependency.page_id == MemoryWikiPage.id).where(
                MemoryWikiDependency.object_kind == "memory", MemoryWikiDependency.object_id == row.id,
                *access.predicates(MemoryWikiPage)))).unique().all())
        targets.extend(("wiki", page_id) for page_id in pages)
        target_clauses = [and_(MemoryIndexState.object_kind == kind, MemoryIndexState.object_id == object_id)
                          for kind, object_id in targets]
        states = (await db.scalars(select(MemoryIndexState).where(or_(*target_clauses),
            *access.predicates(MemoryIndexState)))).all()
        outstanding = (await db.scalars(select(MemoryOutbox).where(
            *access.predicates(MemoryOutbox), MemoryOutbox.status.in_(("PENDING", "RUNNING", "RETRY", "DEAD")),
            or_(*[and_(MemoryOutbox.object_kind == kind, MemoryOutbox.object_id == object_id)
                  for kind, object_id in targets])))).all()
        component_complete = bool(tombstone and tombstone.purge_status == "SUCCEEDED")
        derived_pending = bool(outstanding or any(state.status not in {"DELETED", "INELIGIBLE"} for state in states))
        complete = component_complete and not derived_pending
        return {"memory_id": row.id, "status": "active" if not tombstone else "cleaned" if complete else "stopped_cleanup_pending",
                "stopped": bool(tombstone), "purge_status": "SUCCEEDED" if complete else "PENDING" if tombstone else None,
                "object_purge_status": tombstone.purge_status if tombstone else None,
                "pending_components": len(outstanding), "retained_shared_sources": retained_shared,
                "indexes": [{"kind": state.object_kind, "generation": state.index_generation,
                             "desired_revision": state.desired_revision, "indexed_revision": state.indexed_revision,
                             "status": state.status} for state in states]}


async def record_hits(memory_ids, *, user_id, workspace_id=None, project_id=None):
    if not memory_ids:
        return
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        await db.execute(update(UserMemory).where(UserMemory.id.in_(memory_ids), *access.predicates(UserMemory),
            *active_memory_predicates()).values(hit_count=UserMemory.hit_count + 1, last_hit_at=_now()))
