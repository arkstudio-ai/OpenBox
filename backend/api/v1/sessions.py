"""``/v1/sessions``: create, inspect, abort."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from api.v1.activity import VIDEO_JOB_IN_FLIGHT, session_activity
from api.v1.deps import load_session_row, require_scope
from api.v1.errors import ApiError
from api.v1.ids import public_id
from api.v1.public import ACTIVE_STATUSES, session_view
from api.v1.quality import resolve_quality, validate_quality
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.billing import UsageEvent
from db.models.session import Session as SessionRow
from db.models.video_job import VideoJob
from session import session as session_mod

router = APIRouter(prefix="/sessions", tags=["sessions"])

_METADATA_MAX_KEYS = 16
_METADATA_KEY_MAX = 64
_METADATA_VALUE_MAX = 512


class CreateSessionBody(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    quality: str | None = None
    metadata: dict[str, str] | None = None

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > _METADATA_MAX_KEYS:
            raise ValueError(f"metadata allows at most {_METADATA_MAX_KEYS} keys")
        for key, item in value.items():
            if not key or len(key) > _METADATA_KEY_MAX:
                raise ValueError(f"metadata keys must be 1..{_METADATA_KEY_MAX} characters")
            if len(item) > _METADATA_VALUE_MAX:
                raise ValueError(f"metadata values must be at most {_METADATA_VALUE_MAX} characters")
        return value


async def credits_used(session_id: str) -> Decimal | None:
    """Credits this session has consumed, per the usage ledger.

    Shadow-mode events count too: a test deployment that is not charging
    still wants to see what a run would have cost.
    """
    async with get_db_session() as db:
        total = await db.scalar(
            select(func.sum(UsageEvent.credits)).where(
                UsageEvent.session_id == session_id,
                UsageEvent.status.in_(("charged", "shadow")),
            )
        )
    if total is None:
        return None
    return total if isinstance(total, Decimal) else Decimal(str(total))


@router.post("", status_code=201)
async def create_session(
    body: CreateSessionBody | None = None,
    identity: dict = Depends(require_scope("sessions:write")),
):
    from agent.agent import default_agent_name
    from agent.model_resolve import resolve as resolve_model
    from auth.quota import check_session_quota
    from core.config import get_config

    body = body or CreateSessionBody()
    config = get_config()
    quality = validate_quality(body.quality)
    video_model, video_resolution = resolve_quality(quality, config)
    await check_session_quota(identity["user_id"], config)
    model, _ = resolve_model("", config, context="v1 session")
    session = await session_mod.create_session(
        model=model,
        agent=default_agent_name(),
        title=body.title,
        user_id=identity["user_id"],
        workspace_id=identity["workspace_id"],
    )
    async with get_db_session() as db:
        await db.execute(
            update(SessionRow)
            .where(SessionRow.id == session.id)
            .values({
                SessionRow.quality: quality,
                SessionRow.metadata_: dict(body.metadata or {}),
                SessionRow.api_key_id: identity.get("api_key_id"),
                SessionRow.video_model: video_model,
                SessionRow.video_resolution: video_resolution,
            })
        )
    row = await load_session_row(public_id(session.id), identity, write=False)
    return session_view(row, Decimal(0))


def _encode_cursor(created_at: datetime, session_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), session_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, session_id = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(created), str(session_id)
    except Exception as exc:
        raise ApiError(400, "INVALID_REQUEST", "cursor is not valid") from exc


@router.get("")
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, description="next_cursor from the previous page"),
    identity: dict = Depends(require_scope("sessions:read")),
):
    """The caller's sessions in this workspace, newest first.

    Only top-level conversations: task children and cron transcripts are
    internal. ``credits_used`` and the busy flag are computed for the page
    in two grouped queries rather than per row.
    """
    conditions = [
        SessionRow.workspace_id == identity["workspace_id"],
        SessionRow.user_id == identity["user_id"],
        SessionRow.is_deleted.is_(False),
        SessionRow.parent_id.is_(None),
        SessionRow.kind == "normal",
    ]
    if cursor:
        created_at, last_id = _decode_cursor(cursor)
        conditions.append(
            (SessionRow.created_at < created_at)
            | ((SessionRow.created_at == created_at) & (SessionRow.id < last_id))
        )
    async with get_db_session() as db:
        rows = (await db.scalars(
            select(SessionRow).where(*conditions)
            .order_by(SessionRow.created_at.desc(), SessionRow.id.desc())
            .limit(limit + 1)
        )).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        ids = [row.id for row in rows]
        credits: dict[str, Decimal] = {}
        busy: set[str] = set()
        if ids:
            for session_id, total in (await db.execute(
                select(UsageEvent.session_id, func.sum(UsageEvent.credits))
                .where(UsageEvent.session_id.in_(ids), UsageEvent.status.in_(("charged", "shadow")))
                .group_by(UsageEvent.session_id)
            )).all():
                credits[session_id] = total if isinstance(total, Decimal) else Decimal(str(total))
            busy.update((await db.scalars(
                select(VideoJob.session_id).where(
                    VideoJob.session_id.in_(ids), VideoJob.status.in_(tuple(VIDEO_JOB_IN_FLIGHT))
                ).distinct()
            )).all())
            busy.update((await db.scalars(
                select(AgentInboxItem.session_id).where(
                    AgentInboxItem.session_id.in_(ids), AgentInboxItem.state == "accepted",
                    AgentInboxItem.client_id.like("vjob:%"),
                ).distinct()
            )).all())
    data = [session_view(row, credits.get(row.id), busy=row.id in busy) for row in rows]
    next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return {"data": data, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    identity: dict = Depends(require_scope("sessions:read")),
):
    row = await load_session_row(session_id, identity, write=False)
    activity = await session_activity(row.id)
    return session_view(row, await credits_used(row.id), busy=activity.busy)


@router.post("/{session_id}/abort")
async def abort_session(
    session_id: str,
    identity: dict = Depends(require_scope("sessions:write")),
):
    """Stop the turn in flight. ``aborted=false`` means nothing was running."""
    from agent.driver import get_driver_state
    from agent.inbox import cancel_inbox_items
    from session.abort import abort_session_turn

    row = await load_session_row(session_id, identity, write=True)
    canceled = await cancel_inbox_items(session_id=row.id, user_id=row.user_id, reason="user_stop")
    state = await get_driver_state(row.id)
    if state is not None and state.phase != "idle" and state.run_id:
        marked = await abort_session_turn(
            row.id, row.user_id, reason="user_stop", was_active=True,
            expected_run_id=state.run_id, expected_generation=state.generation,
        )
    else:
        marked = await abort_session_turn(
            row.id, row.user_id, reason="user_stop",
            was_active=row.status in ACTIVE_STATUSES,
        )
    return {"ok": True, "aborted": bool(marked or canceled)}



