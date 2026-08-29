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
from sqlalchemy.exc import IntegrityError

from core.identifier import ascending
from core.log import create_logger

log = create_logger("skill_runtime.receipt")

TERMINAL_EVENT_TYPES = {"job.succeeded", "job.failed", "job.cancelled"}


def _is_identifier(key: str) -> bool:
    """Identifiers address things for machines; they tell a reader nothing."""
    return key == "id" or key.endswith("_id") or key.endswith("Id")


def _summary(job) -> str:
    if job.status == "succeeded":
        result = job.result_data or {}
        parts = [
            f"{key}: {value}"
            for key, value in result.items()
            if not isinstance(value, (dict, list))
            and value is not None
            and not _is_identifier(key)
        ]
        text = " · ".join(parts)
        return text[:300]
    if job.error_message:
        return str(job.error_message)[:300]
    return ""


async def _receipt_artifacts(job) -> list[dict]:
    """The job's output files, flattened for the transcript.

    Only what a renderer needs — id, name, mime. Kept small because this is
    embedded in a message part, not fetched on demand like the live card's.
    """
    from skill_runtime import repository as repo

    try:
        rows = await repo.list_artifacts(job.id, job.user_id)
    except Exception as exc:  # a receipt must still be written without them
        log.warning(f"Could not attach artifacts to receipt for {job.id}: {exc!r}")
        return []
    return [
        {"assetId": row["assetId"], "name": row["name"], "mime": row["mime"]}
        for row in rows
        if row.get("role") == "output"
    ]


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
    # The receipt outlives the live card, so it has to carry what the job
    # produced. Without this the durable record of a paid video generation is a
    # status chip and an asset id buried in truncated summary text — the file
    # itself is only findable by going to look for it.
    artifacts = await _receipt_artifacts(job)
    part_data = {
        "type": "skill_job",
        "id": part_id,
        "jobId": job.id,
        "skillKey": job.skill_key,
        "operation": job.operation,
        "status": job.status,
        "errorCode": job.error_code,
        "summary": _summary(job),
        "artifacts": artifacts,
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
                        MessageORM.user_id == job.user_id,
                        MessageORM.client_message_id == marker,
                        MessageORM.role == "assistant",
                        MessageORM.finish == "skill_job_receipt",
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
    except IntegrityError as integrity_error:
        # A racing publisher already wrote this receipt; the partial unique
        # index on the marker is the real guard, the pre-check is a fast path.
        async with get_db_session() as db:
            duplicate = (
                await db.execute(
                    select(MessageORM.id).where(
                        MessageORM.session_id == job.session_id,
                        MessageORM.user_id == job.user_id,
                        MessageORM.client_message_id == marker,
                        MessageORM.role == "assistant",
                        MessageORM.finish == "skill_job_receipt",
                    )
                )
            ).scalar_one_or_none()
        if duplicate is None:
            # A broken FK or another constraint is not successful idempotency;
            # keep the outbox event unstamped so the failure remains visible.
            raise integrity_error
        return False
    except Exception as exc:
        # ORM/driver exceptions may embed SQL parameters, user input, or
        # backend connection details.  The exception class is sufficient for
        # the operator log; the original error still propagates with traceback.
        log.warning(
            f"Receipt for job {job.id} not written: {type(exc).__name__}"
        )
        raise

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
