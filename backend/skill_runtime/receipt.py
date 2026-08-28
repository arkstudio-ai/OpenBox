"""Terminal-job chat receipts (§8.3).

A finished background job must leave a durable trace in its session's
timeline — the dock hides old cards, and an exported transcript without the
outcome is incomplete. The receipt is a structured assistant message written
by the platform (zero LLM tokens); whether the agent additionally narrates is
a separate, off-by-default product switch.

Idempotency rides client_message_id: one receipt per job, ever.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from core.identifier import ascending
from core.log import create_logger

log = create_logger("skill_runtime.receipt")

TERMINAL_EVENT_TYPES = {"job.succeeded", "job.failed", "job.cancelled"}


def _summary(job) -> str:
    if job.status == "succeeded":
        result = job.result_data or {}
        parts = [
            f"{key}: {value}"
            for key, value in result.items()
            if not isinstance(value, (dict, list)) and value is not None
        ]
        text = " · ".join(parts)
        return text[:300]
    if job.error_message:
        return str(job.error_message)[:300]
    return ""


async def write_receipt(job) -> bool:
    """Insert the receipt message for a terminal job. Returns True when a new
    receipt was written."""
    from core.config import get_config

    if not get_config().skill_job_chat_receipt:
        return False
    if not job.session_id:
        return False

    from db.base import get_db_session
    from db.models.message import Message as MessageORM
    from db.models.part import Part as PartORM
    from db.models.session import Session as SessionORM

    marker = f"sjr:{job.id}"
    now = datetime.now(timezone.utc)
    message_id = ascending("message")
    part_id = ascending("part")
    part_data = {
        "type": "skill_job",
        "id": part_id,
        "jobId": job.id,
        "skillKey": job.skill_key,
        "operation": job.operation,
        "status": job.status,
        "errorCode": job.error_code,
        "summary": _summary(job),
    }

    try:
        async with get_db_session() as db:
            session_row = (
                await db.execute(
                    select(SessionORM.id).where(
                        SessionORM.id == job.session_id,
                        SessionORM.user_id == job.user_id,
                        SessionORM.is_deleted == False,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            if session_row is None:
                return False
            existing = (
                await db.execute(
                    select(MessageORM.id).where(
                        MessageORM.session_id == job.session_id,
                        MessageORM.client_message_id == marker,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False
            db.add(
                MessageORM(
                    id=message_id,
                    session_id=job.session_id,
                    user_id=job.user_id,
                    role="assistant",
                    client_message_id=marker,
                    finish="skill_job_receipt",
                    created_at=now,
                )
            )
            db.add(
                PartORM(
                    id=part_id,
                    message_id=message_id,
                    session_id=job.session_id,
                    user_id=job.user_id,
                    type="skill_job",
                    data=part_data,
                    created_at=now,
                )
            )
    except Exception as e:
        # A racing publisher or a deleted session must not fail the outbox.
        log.warning(f"Receipt for job {job.id} not written: {e}")
        return False

    from bus import bus
    from bus.events import MESSAGE_CREATED

    bus.publish(
        MESSAGE_CREATED,
        {
            "userId": job.user_id,
            "sessionId": job.session_id,
            "message": {
                "id": message_id,
                "session_id": job.session_id,
                "role": "assistant",
                "parts": [part_data],
                "created_at": now.isoformat(),
                "finish": "skill_job_receipt",
            },
        },
    )
    log.info(f"Wrote chat receipt for job {job.id} ({job.status})")
    return True
