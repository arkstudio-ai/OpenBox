"""``/v1/sessions/{id}/messages``: send a prompt, poll the transcript."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from api.v1.activity import session_activity
from api.v1.deps import load_session_row, require_scope
from api.v1.errors import ApiError
from api.v1.ids import assistant_turn_id, internal_id, public_id
from api.v1.public import ACTIVE_STATUSES, public_messages
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.question import QuestionCheckpoint
from db.models.session import Session as SessionRow
from session import session as session_mod

router = APIRouter(prefix="/sessions/{session_id}/messages", tags=["messages"])

TEXT_MAX_BYTES = 20 * 1024
ATTACHMENTS_MAX = 20
#: How long the send waits for the loop to materialise the user message
#: before answering with what it has. Claiming is synchronous in the common
#: case; this only covers a wake already in flight on another task.
_MATERIALIZE_WAIT_SECONDS = 3.0
_MATERIALIZE_POLL_SECONDS = 0.1


class SendMessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=65_536)
    attachments: list[str] = Field(default_factory=list)
    client_message_id: str | None = Field(default=None, max_length=64)

    @field_validator("client_message_id")
    @classmethod
    def _no_reserved_prefix(cls, value: str | None) -> str | None:
        if value and value.startswith(("sjr:", "tabort:", "ask:", "vjob:", "cron:")):
            raise ValueError("client_message_id uses a reserved prefix")
        return value

    @field_validator("attachments")
    @classmethod
    def _bounded_attachments(cls, value: list[str]) -> list[str]:
        if len(value) > ATTACHMENTS_MAX:
            raise ValueError(f"at most {ATTACHMENTS_MAX} attachments per message")
        if len(set(value)) != len(value):
            raise ValueError("attachment ids must be unique")
        if any(not item or len(item) > 64 for item in value):
            raise ValueError("attachment ids must be 1..64 characters")
        return value


async def _check_key_concurrency(identity: dict) -> None:
    """One leaked key must not run the whole workspace's agents at once."""
    if identity.get("auth_kind") != "api_key":
        return
    limit = int((identity.get("policy") or {}).get("max_concurrent_sessions") or 0)
    if limit <= 0:
        return
    async with get_db_session() as db:
        busy = await db.scalar(
            select(func.count()).select_from(SessionRow).where(
                SessionRow.api_key_id == identity["api_key_id"],
                SessionRow.status.in_(tuple(ACTIVE_STATUSES)),
                SessionRow.is_deleted.is_(False),
            )
        ) or 0
    if busy >= limit:
        raise ApiError(
            429, "CONCURRENT_LIMIT_EXCEEDED",
            f"This API key already has {busy}/{limit} sessions in progress",
            details={"used": busy, "limit": limit},
        )


async def _precheck_credits(workspace_id: str) -> None:
    from billing.service import BillingError, precheck_balance

    try:
        await precheck_balance(workspace_id)
    except BillingError as exc:
        raise ApiError(402, exc.code, str(exc)) from exc


async def _already_accepted(session_id: str, user_id: str, client_message_id: str | None) -> bool:
    if not client_message_id:
        return False
    async with get_db_session() as db:
        found = await db.scalar(
            select(AgentInboxItem.id).where(
                AgentInboxItem.user_id == user_id,
                AgentInboxItem.session_id == session_id,
                AgentInboxItem.client_id == client_message_id,
            ).limit(1)
        )
    return found is not None


async def _wait_for_user_message(item_id: str, user_id: str, session_id: str) -> str | None:
    from agent.inbox import get_inbox_item

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _MATERIALIZE_WAIT_SECONDS
    while True:
        receipt = await get_inbox_item(item_id, user_id=user_id, session_id=session_id)
        if receipt is None:
            return None
        if receipt.message_id or receipt.state in ("canceled", "settled"):
            return receipt.message_id
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(_MATERIALIZE_POLL_SECONDS)


@router.post("", status_code=202)
async def send_message(
    session_id: str,
    body: SendMessageBody,
    identity: dict = Depends(require_scope("sessions:write")),
):
    """Accept one prompt for an idle session and start the turn.

    Busy sessions answer ``409`` rather than preempting the running turn:
    a machine caller that wants to replace work aborts first. The prompt
    goes through the durable inbox, so ``client_message_id`` replays return
    the original ids without running again.
    """
    from agent.inbox import (
        InboxAttachmentError,
        InboxIdempotencyConflict,
        accept_inbox_item,
        wake_inbox_session,
    )
    from agent.model_resolve import resolve as resolve_model
    from core.config import get_config

    if len(body.text.encode("utf-8")) > TEXT_MAX_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"text must be at most {TEXT_MAX_BYTES} bytes")
    row = await load_session_row(session_id, identity, write=True)
    # A replay of an accepted prompt is answered from the inbox whatever the
    # session is doing: the caller is retrying a timed-out request, not
    # queueing new work. Only genuinely new input meets the busy, balance and
    # concurrency gates.
    if not await _already_accepted(row.id, row.user_id, body.client_message_id):
        if row.status in ACTIVE_STATUSES or (await session_activity(row.id)).busy:
            raise ApiError(409, "SESSION_BUSY", "session is busy, wait for idle or abort it first")
        await _precheck_credits(row.workspace_id)
        await _check_key_concurrency(identity)

    model, _ = resolve_model(row.model, get_config(), context=f"v1 session {row.id}")
    attachments = [internal_id(item, "asset") for item in body.attachments]
    try:
        receipt = await accept_inbox_item(
            session_id=row.id,
            user_id=row.user_id,
            delivery="followup",
            prompt=body.text,
            attachments=attachments,
            client_id=body.client_message_id,
            agent=row.agent or None,
            model=model,
            video_model=row.video_model or None,
            video_resolution=row.video_resolution or None,
            variant=row.variant,
            output_format=None,
        )
    except InboxIdempotencyConflict as exc:
        raise ApiError(409, "DUPLICATE_CLIENT_MESSAGE_ID", str(exc)) from exc
    except InboxAttachmentError as exc:
        raise ApiError(400, "INVALID_REQUEST", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "INVALID_REQUEST", str(exc)) from exc

    if receipt.state == "accepted":
        await wake_inbox_session(row.id, row.user_id)
    user_message_id = receipt.message_id or await _wait_for_user_message(
        receipt.id, row.user_id, row.id,
    )
    return JSONResponse(status_code=202, content={
        "session_id": public_id(row.id),
        "user_message_id": public_id(user_message_id),
        "assistant_message_id": assistant_turn_id(user_message_id) if user_message_id else None,
    })


def _presigner():
    from core.oss import OssNotConfigured, get_oss

    try:
        oss = get_oss()
    except OssNotConfigured:
        return None
    return lambda key, name: oss.presign_get(key, expires_sec=24 * 3600, download_name=name)


@router.get("")
async def list_messages(
    session_id: str,
    after: str | None = Query(default=None, description="message id; returns it and everything newer"),
    limit: int = Query(default=50, ge=1, le=200),
    identity: dict = Depends(require_scope("sessions:read")),
):
    row = await load_session_row(session_id, identity, write=False)
    messages = await session_mod.get_messages(row.id, user_id=row.user_id)
    activity = await session_activity(row.id)
    async with get_db_session() as db:
        checkpoints = {
            item.id: item for item in (await db.scalars(
                select(QuestionCheckpoint).where(QuestionCheckpoint.session_id == row.id)
            )).all()
        }
    items = public_messages(
        messages,
        session_status_value=row.status,
        checkpoints=checkpoints,
        presign=_presigner(),
        work_pending=activity.busy,
        aborted_user_message_ids=activity.aborted_user_message_ids,
    )
    if after:
        start = next((i for i, item in enumerate(items) if item["id"] == after), None)
        if start is None:
            raise ApiError(404, "NOT_FOUND", "Message not found in this session")
        items = items[start:]
    return {"data": items[:limit], "has_more": len(items) > limit}
