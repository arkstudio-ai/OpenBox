"""Creator-memory storage service (ported from bossip memory.service.ts).

Every function takes a mandatory ``user_id`` and scopes every query by it —
identity comes from the caller's authenticated context (ToolContext or the
HTTP auth dependency), never from model-supplied arguments.

Lifecycle:
- ``write_memory`` always creates CANDIDATE rows (promotion is a later,
  human- or rule-driven step).
- Proposals go through ``propose_note`` → ``confirm_note``/``reject_note``.
  A pending proposal has ``type=PENDING_NOTE`` which is excluded from both
  context type buckets, so an unconfirmed proposal can never leak into an
  assembled prompt.
- Deletion is soft (status DEPRECATED) to keep the "don't ask again" trace.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory

MAX_SUMMARY_CHARS = 2000
PENDING_NOTE_TYPE = "PENDING_NOTE"
USER_NOTE_TYPE = "USER_NOTE"
ALLOWED_SCOPES = {"SHORT_TERM", "LONG_TERM"}
ALLOWED_OWNERS = {"USER_CONFIRMED", "SYSTEM_INFERRED", "OPERATOR_CONFIRMED"}
ALLOWED_STATUSES = {"CANDIDATE", "ACTIVE", "EXPIRED", "DEPRECATED"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slim(row: UserMemory) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope": row.scope,
        "type": row.type,
        "status": row.status,
        "confidence": row.confidence,
        "value": row.value or {},
        "owner": row.owner,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _truncate_value(value: dict | None) -> dict:
    """Server-side summary truncation — the pydantic max_length on the tool
    args can be bypassed by writing through the raw ``value`` dict."""
    value = dict(value or {})
    summary = value.get("summary")
    if isinstance(summary, str) and len(summary) > MAX_SUMMARY_CHARS:
        value["summary"] = summary[:MAX_SUMMARY_CHARS]
    return value


def _not_expired(now: datetime):
    return (UserMemory.ttl.is_(None)) | (UserMemory.ttl > now)


async def write_memory(
    *,
    user_id: str,
    project_id: str | None = None,
    scope: str,
    type: str,
    value: dict,
    owner: str,
    confidence: int = 50,
    evidence: dict | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    if scope not in ALLOWED_SCOPES:
        raise ValueError(f"scope must be one of {sorted(ALLOWED_SCOPES)}")
    if owner not in ALLOWED_OWNERS:
        raise ValueError(f"owner must be one of {sorted(ALLOWED_OWNERS)}")
    if type in {PENDING_NOTE_TYPE, USER_NOTE_TYPE}:
        raise ValueError(
            f"{type} must go through propose_note, not write_memory"
        )
    now = _now()
    row = UserMemory(
        id=ascending("memory"),
        user_id=user_id,
        project_id=project_id,
        scope=scope,
        type=type,
        value=_truncate_value(value),
        evidence=evidence or {},
        confidence=max(0, min(100, confidence)),
        ttl=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        owner=owner,
        status="CANDIDATE",
        created_at=now,
        updated_at=now,
    )
    async with get_db_session() as db:
        db.add(row)
        await db.flush()
        return _slim(row)


async def search_memories(
    *,
    user_id: str,
    project_id: str | None = None,
    type: str | None = None,
    scope: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    now = _now()
    stmt = select(UserMemory).where(UserMemory.user_id == user_id, _not_expired(now))
    if project_id is not None:
        stmt = stmt.where(
            (UserMemory.project_id == project_id) | (UserMemory.project_id.is_(None))
        )
    if type:
        stmt = stmt.where(UserMemory.type == type)
    if scope:
        stmt = stmt.where(UserMemory.scope == scope)
    if status:
        stmt = stmt.where(UserMemory.status == status)
    stmt = stmt.order_by(
        UserMemory.confidence.desc(), UserMemory.updated_at.desc()
    ).limit(max(1, min(limit, 100)))
    async with get_db_session() as db:
        rows = (await db.execute(stmt)).scalars().all()
        return [_slim(row) for row in rows]


async def list_active_memories(
    *, user_id: str, project_id: str | None = None
) -> list[dict[str, Any]]:
    now = _now()
    stmt = select(UserMemory).where(
        UserMemory.user_id == user_id,
        UserMemory.status == "ACTIVE",
        UserMemory.scope.in_(["LONG_TERM", "SHORT_TERM"]),
        _not_expired(now),
    )
    if project_id is not None:
        stmt = stmt.where(
            (UserMemory.project_id == project_id) | (UserMemory.project_id.is_(None))
        )
    stmt = stmt.order_by(UserMemory.scope.desc(), UserMemory.confidence.desc())
    async with get_db_session() as db:
        rows = (await db.execute(stmt)).scalars().all()
        return [_slim(row) for row in rows]


async def propose_note(
    *,
    user_id: str,
    project_id: str | None = None,
    summary: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    now = _now()
    row = UserMemory(
        id=ascending("memory"),
        user_id=user_id,
        project_id=project_id,
        scope="LONG_TERM",
        type=PENDING_NOTE_TYPE,
        value=_truncate_value({"summary": summary}),
        evidence={"source": "chat", "session_id": session_id, "awaiting_confirm": True},
        confidence=30,
        owner="SYSTEM_INFERRED",
        status="CANDIDATE",
        created_at=now,
        updated_at=now,
    )
    async with get_db_session() as db:
        db.add(row)
        await db.flush()
        return _slim(row)


async def _find_pending(db, user_id: str, proposal_id: str | None) -> UserMemory | None:
    stmt = select(UserMemory).where(
        UserMemory.user_id == user_id,
        UserMemory.type == PENDING_NOTE_TYPE,
        UserMemory.status == "CANDIDATE",
    )
    if proposal_id:
        stmt = stmt.where(UserMemory.id == proposal_id)
    else:
        stmt = stmt.order_by(UserMemory.created_at.desc())
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def confirm_note(
    *, user_id: str, proposal_id: str | None = None, edited_summary: str | None = None
) -> dict[str, Any] | None:
    async with get_db_session() as db:
        row = await _find_pending(db, user_id, proposal_id)
        if row is None:
            return None
        row.type = USER_NOTE_TYPE
        row.owner = "USER_CONFIRMED"
        row.status = "ACTIVE"
        row.confidence = 90
        if edited_summary:
            row.value = _truncate_value({**(row.value or {}), "summary": edited_summary})
        evidence = dict(row.evidence or {})
        evidence["awaiting_confirm"] = False
        row.evidence = evidence
        row.updated_at = _now()
        await db.flush()
        return _slim(row)


async def reject_note(*, user_id: str, proposal_id: str | None = None) -> bool:
    async with get_db_session() as db:
        row = await _find_pending(db, user_id, proposal_id)
        if row is None:
            return False
        row.status = "DEPRECATED"
        row.updated_at = _now()
        return True


async def create_note(
    *, user_id: str, project_id: str | None = None, summary: str
) -> dict[str, Any]:
    """Directly-active user note (manual creation from a settings UI)."""
    now = _now()
    row = UserMemory(
        id=ascending("memory"),
        user_id=user_id,
        project_id=project_id,
        scope="LONG_TERM",
        type=USER_NOTE_TYPE,
        value=_truncate_value({"summary": summary}),
        evidence={"source": "manual"},
        confidence=90,
        owner="USER_CONFIRMED",
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    async with get_db_session() as db:
        db.add(row)
        await db.flush()
        return _slim(row)


async def edit_note(*, user_id: str, memory_id: str, summary: str) -> dict[str, Any] | None:
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.id == memory_id,
                    UserMemory.user_id == user_id,
                    UserMemory.type == USER_NOTE_TYPE,
                    UserMemory.status != "DEPRECATED",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.value = _truncate_value({**(row.value or {}), "summary": summary})
        row.updated_at = _now()
        await db.flush()
        return _slim(row)


async def delete_memory(*, user_id: str, memory_id: str) -> bool:
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.id == memory_id,
                    UserMemory.user_id == user_id,
                    UserMemory.status != "DEPRECATED",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.status = "DEPRECATED"
        row.updated_at = _now()
        return True


async def record_hits(memory_ids: list[str], *, user_id: str) -> None:
    """Bump hit counters, scoped to the owner.

    Every other query here is user-scoped; this one took ids alone, so a caller
    that ever passed unvalidated ids could touch another user's rows. Scoping
    it costs nothing and removes the standing hazard.
    """
    if not memory_ids:
        return
    now = _now()
    async with get_db_session() as db:
        await db.execute(
            update(UserMemory)
            .where(UserMemory.id.in_(memory_ids), UserMemory.user_id == user_id)
            .values(hit_count=UserMemory.hit_count + 1, last_hit_at=now)
        )
