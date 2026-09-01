"""Durable post-settlement delivery for Cron side effects.

The scheduler writes these rows in the same transaction that settles the exact
Cron claim.  Consumers use database-backed claim leases; no process scans a
pending result and marks it unconditionally.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import socket
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from core.identifier import ascending
from core.log import create_logger


log = create_logger("cron.outbox")

DELIVERY_LEASE_TTL_SECONDS = 90
DELIVERY_HEARTBEAT_SECONDS = 20
DELIVERY_POLL_SECONDS = 2
DELIVERY_BATCH_SIZE = 32
DELIVERY_MAX_ATTEMPTS = 12
DELIVERY_LAST_ERROR_MAX_CHARS = 4000
OUTBOX_WORKER_PULSE_SECONDS = 1
OUTBOX_WORKER_STALE_SECONDS = 10
OUTBOX_MAX_CONSECUTIVE_DISPATCH_ERRORS = 3
DELIVERY_OWNER_ID = (
    f"{socket.gethostname()}-{os.getpid()}-cron-outbox-{secrets.token_hex(4)}"
)


class DeliveryLeaseLost(RuntimeError):
    """Another worker may now own this delivery row."""


@dataclass(frozen=True)
class DeliveryClaim:
    delivery_id: str
    run_id: str
    job_id: str
    user_id: str
    project_id: str | None
    session_id: str | None
    kind: str
    payload: dict
    attempts: int
    token: str
    owner_id: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class DeliveryOutcome:
    success: bool
    error: str | None = None
    terminal: bool = False
    retry_delay_seconds: int | None = None


def stable_delivery_id(run_id: str, kind: str) -> str:
    digest = hashlib.sha256(f"{run_id}:{kind}".encode("utf-8")).hexdigest()
    return f"cron-delivery-{digest[:48]}"


def _bounded_error(error: str | None) -> str:
    value = str(error or "Cron delivery failed")
    return value[:DELIVERY_LAST_ERROR_MAX_CHARS]


def build_delivery_rows(job, run, result: dict, settled_at: datetime) -> list:
    """Build immutable delivery snapshots for one already-fenced settlement."""
    from cron.i18n import is_silent, text
    from cron.runlog import build_log_entry, log_filename
    from db.models.cron import CronDeliveryOutbox

    status = str(result.get("status") or "error")
    summary_text = result.get("summary_text")
    error = result.get("error") or result.get("error_message")
    body = summary_text if status == "ok" else error
    duration_ms = int(result.get("duration_ms") or 0)
    tokens = result.get("tokens") or {}
    total_tokens = int(
        result.get("total_tokens")
        or tokens.get("total_tokens")
        or 0
    )
    locale = str(result.get("locale") or "zh-CN")
    silent = bool(result.get("silent")) if "silent" in result else (
        status == "ok" and is_silent(summary_text)
    )
    occurred_at = (
        result.get("ended_at")
        if isinstance(result.get("ended_at"), datetime)
        else settled_at
    )
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    common = {
        "schema_version": 1,
        "run_id": run.id,
        "job_id": job.id,
        "user_id": job.user_id,
        "project_id": job.project_id,
        "session_id": job.session_id,
        "job_name": job.name,
        "task_prompt": run.task_prompt or job.task_prompt,
        "status": status,
        "summary_text": summary_text,
        "error": error,
        "duration_ms": duration_ms,
        "silent": silent,
        "locale": locale,
        "occurred_at": occurred_at.isoformat(),
    }

    rows: list[CronDeliveryOutbox] = []

    def add(kind: str, payload: dict) -> None:
        rows.append(CronDeliveryOutbox(
            id=stable_delivery_id(run.id, kind),
            run_id=run.id,
            job_id=job.id,
            user_id=job.user_id,
            project_id=job.project_id,
            session_id=job.session_id,
            kind=kind,
            payload=payload,
            state="pending",
            attempts=0,
            available_at=settled_at,
            created_at=settled_at,
            updated_at=settled_at,
        ))

    # Completion/failure notification is itself a committed delivery item.
    add("event", dict(common))

    if run.temp_session_id:
        log_job = {
            "id": job.id,
            "name": job.name,
            "schedule": job.schedule or {},
        }
        add("runlog", {
            **common,
            "temp_session_id": run.temp_session_id,
            "job": log_job,
            "filename": log_filename(log_job, occurred_at),
            "entry": build_log_entry(
                log_job,
                status,
                body,
                duration_ms,
                total_tokens,
                silent,
                locale,
                now=occurred_at,
            ),
        })

    if status == "ok" and not silent and job.session_id:
        stamp = occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        add("session", {
            **common,
            "user_message_id": ascending("message"),
            "user_part_id": ascending("part"),
            "assistant_message_id": ascending("message"),
            "assistant_part_id": ascending("part"),
            "user_text": (
                f"[{text(locale, 'scheduled_task')}: {job.name} | "
                f"job_id: {job.id} | {stamp}]\n"
                f"{run.task_prompt or job.task_prompt}"
            ),
            "result_text": summary_text or "",
        })

    delivery = job.delivery or {}
    if (
        isinstance(delivery, dict)
        and delivery.get("mode") == "webhook"
        and (status != "ok" or not silent)
    ):
        add("webhook", {**common, "delivery": dict(delivery)})

    return rows


async def materialize_legacy_pending_session_deliveries() -> int:
    """Bridge pre-outbox ``CronRun.injected=false`` rows exactly once.

    Old versions had already attempted runlog/webhook/event delivery, so only
    the still-explicit pending session injection is materialized.  Deleted
    jobs and silent/page results are consumed without creating new effects.
    """
    from core.config import get_config
    from cron.i18n import is_silent, text
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronJob, CronRun
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    locale = get_config().cron_default_locale or "zh-CN"
    created = 0
    async with get_db_session() as db:
        query = (
            select(CronRun, CronJob)
            .join(CronJob, CronJob.id == CronRun.job_id)
            .where(
                CronRun.status == "ok",
                CronRun.injected == False,  # noqa: E712
                ~select(CronDeliveryOutbox.id).where(
                    CronDeliveryOutbox.run_id == CronRun.id,
                    CronDeliveryOutbox.kind == "session",
                ).exists(),
            )
            .order_by(CronRun.started_at.asc())
        )
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True, of=CronRun)
        rows = (await db.execute(query)).all()
        for run, job in rows:
            if (
                job.is_deleted
                or not run.session_id
                or is_silent(run.summary_text)
            ):
                run.injected = True
                run.injected_at = now
                continue

            occurred_at = run.ended_at or run.started_at or now
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            delivery_id = stable_delivery_id(run.id, "session")
            stamp = occurred_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            payload = {
                "run_id": run.id,
                "job_id": job.id,
                "user_id": run.user_id,
                "project_id": run.project_id,
                "session_id": run.session_id,
                "job_name": job.name,
                "task_prompt": run.task_prompt or job.task_prompt,
                "status": "ok",
                "summary_text": run.summary_text,
                "error": None,
                "duration_ms": int(run.duration_ms or 0),
                "silent": False,
                "locale": locale,
                "occurred_at": occurred_at.isoformat(),
                "user_message_id": ascending("message"),
                "user_part_id": ascending("part"),
                "assistant_message_id": ascending("message"),
                "assistant_part_id": ascending("part"),
                "user_text": (
                    f"[{text(locale, 'scheduled_task')}: {job.name} | "
                    f"job_id: {job.id} | {stamp}]\n"
                    f"{run.task_prompt or job.task_prompt}"
                ),
                "result_text": run.summary_text or "",
                "schema_version": 1,
                "legacy_materialized": True,
            }
            db.add(CronDeliveryOutbox(
                id=delivery_id,
                run_id=run.id,
                job_id=job.id,
                user_id=run.user_id,
                project_id=run.project_id,
                session_id=run.session_id,
                kind="session",
                payload=payload,
                state="pending",
                attempts=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            ))
            created += 1
    return created


def _database_now(db):
    from cron.lease import _database_now as cron_database_now

    return cron_database_now(db)


def _database_after(db, seconds: int):
    from sqlalchemy import func, text

    seconds = max(0, int(seconds))
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return func.clock_timestamp() + text(f"INTERVAL '{seconds} seconds'")
    if dialect == "sqlite":
        return func.datetime("now", f"+{seconds} seconds")
    return _database_now(db) + timedelta(seconds=seconds)


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _claimable_clause(row, database_now):
    from sqlalchemy import and_, or_

    return or_(
        row.state == "pending",
        and_(
            row.state == "processing",
            row.claim_expires_at.isnot(None),
            row.claim_expires_at < database_now,
        ),
    )


async def claim_delivery(
    *,
    owner_id: str = DELIVERY_OWNER_ID,
    kinds: Iterable[str] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> DeliveryClaim | None:
    """Atomically claim the oldest due delivery row."""
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import select, update

    token = secrets.token_hex(24)
    async with get_db_session() as db:
        database_now = _database_now(db)
        predicates = [
            CronDeliveryOutbox.available_at <= database_now,
            _claimable_clause(CronDeliveryOutbox, database_now),
        ]
        if kinds is not None:
            selected_kinds = tuple(kinds)
            if not selected_kinds:
                return None
            predicates.append(CronDeliveryOutbox.kind.in_(selected_kinds))
        if session_id is not None:
            predicates.append(CronDeliveryOutbox.session_id == session_id)
        if user_id is not None:
            predicates.append(CronDeliveryOutbox.user_id == user_id)

        query = (
            select(CronDeliveryOutbox.id)
            .where(*predicates)
            .order_by(
                CronDeliveryOutbox.available_at.asc(),
                CronDeliveryOutbox.created_at.asc(),
                CronDeliveryOutbox.id.asc(),
            )
            .limit(1)
        )
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        delivery_id = (await db.execute(query)).scalar_one_or_none()
        if delivery_id is None:
            return None

        claimed = await db.execute(
            update(CronDeliveryOutbox)
            .where(
                CronDeliveryOutbox.id == delivery_id,
                CronDeliveryOutbox.available_at <= database_now,
                _claimable_clause(CronDeliveryOutbox, database_now),
            )
            .values(
                state="processing",
                attempts=CronDeliveryOutbox.attempts + 1,
                claim_token=token,
                claim_owner=owner_id,
                claim_expires_at=_database_after(
                    db, DELIVERY_LEASE_TTL_SECONDS
                ),
                updated_at=database_now,
            )
        )
        if claimed.rowcount != 1:
            return None
        row = (await db.execute(
            select(CronDeliveryOutbox).where(
                CronDeliveryOutbox.id == delivery_id,
                CronDeliveryOutbox.claim_token == token,
                CronDeliveryOutbox.claim_owner == owner_id,
            )
        )).scalar_one()
        claim = DeliveryClaim(
            delivery_id=row.id,
            run_id=row.run_id,
            job_id=row.job_id,
            user_id=row.user_id,
            project_id=row.project_id,
            session_id=row.session_id,
            kind=row.kind,
            payload=dict(row.payload or {}),
            attempts=int(row.attempts or 0),
            token=token,
            owner_id=owner_id,
            lease_expires_at=_aware_utc(row.claim_expires_at),
        )
    return claim


async def renew_delivery(claim: DeliveryClaim) -> datetime | None:
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import update

    async with get_db_session() as db:
        database_now = _database_now(db)
        result = await db.execute(
            update(CronDeliveryOutbox)
            .where(
                CronDeliveryOutbox.id == claim.delivery_id,
                CronDeliveryOutbox.state == "processing",
                CronDeliveryOutbox.claim_token == claim.token,
                CronDeliveryOutbox.claim_owner == claim.owner_id,
                CronDeliveryOutbox.claim_expires_at.isnot(None),
                CronDeliveryOutbox.claim_expires_at >= database_now,
            )
            .values(
                claim_expires_at=_database_after(
                    db, DELIVERY_LEASE_TTL_SECONDS
                ),
                updated_at=database_now,
            )
            .returning(CronDeliveryOutbox.claim_expires_at)
        )
        expires_at = result.scalar_one_or_none()
    return _aware_utc(expires_at) if expires_at is not None else None


async def _heartbeat_delivery(
    claim: DeliveryClaim,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    # The absolute lease timestamp comes from the database clock. Comparing it
    # with this replica's wall clock would make a fast host cancel healthy work
    # and a slow host run past its fence. Each successful DB renewal grants one
    # TTL measured locally only as elapsed monotonic time.
    deadline = time.monotonic() + DELIVERY_LEASE_TTL_SECONDS
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            lost.set()
            return
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=min(DELIVERY_HEARTBEAT_SECONDS, remaining),
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            renewed = await renew_delivery(claim)
        except Exception as exc:
            log.warning(
                "Cron delivery heartbeat failed delivery=%s error_type=%s",
                claim.delivery_id,
                type(exc).__name__,
            )
            if time.monotonic() >= deadline:
                lost.set()
                return
            continue
        if renewed is None:
            lost.set()
            return
        deadline = time.monotonic() + DELIVERY_LEASE_TTL_SECONDS


async def _run_with_delivery_lease(claim: DeliveryClaim, work):
    renewed = await renew_delivery(claim)
    if renewed is None:
        raise DeliveryLeaseLost("Cron delivery lease expired before dispatch")
    active = DeliveryClaim(
        **{**claim.__dict__, "lease_expires_at": renewed}
    )
    stop = asyncio.Event()
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_delivery(active, stop, lost))
    delivery = asyncio.create_task(work())
    lost_wait = asyncio.create_task(lost.wait())
    try:
        done, _ = await asyncio.wait(
            {delivery, lost_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        if lost_wait in done and lost.is_set():
            delivery.cancel()
            with contextlib.suppress(BaseException):
                await delivery
            raise DeliveryLeaseLost("Cron delivery lease was lost")
        return await delivery
    finally:
        stop.set()
        lost_wait.cancel()
        heartbeat.cancel()
        with contextlib.suppress(BaseException):
            await lost_wait
        with contextlib.suppress(BaseException):
            await heartbeat


async def _mark_delivered(
    claim: DeliveryClaim,
    *,
    note: str | None = None,
) -> bool:
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from sqlalchemy import update

    async with get_db_session() as db:
        database_now = _database_now(db)
        result = await db.execute(
            update(CronDeliveryOutbox)
            .where(
                CronDeliveryOutbox.id == claim.delivery_id,
                CronDeliveryOutbox.state == "processing",
                CronDeliveryOutbox.claim_token == claim.token,
                CronDeliveryOutbox.claim_owner == claim.owner_id,
                CronDeliveryOutbox.claim_expires_at.isnot(None),
                CronDeliveryOutbox.claim_expires_at >= database_now,
            )
            .values(
                state="delivered",
                claim_token=None,
                claim_owner=None,
                claim_expires_at=None,
                delivered_at=database_now,
                last_error=_bounded_error(note) if note else None,
                updated_at=database_now,
            )
        )
        if result.rowcount != 1:
            return False
        if claim.kind == "session":
            await db.execute(
                update(CronRun)
                .where(CronRun.id == claim.run_id)
                .values(injected=True, injected_at=database_now)
            )
    return True


async def _mark_dead_letter(claim: DeliveryClaim, error: str) -> bool:
    """Finish a poison delivery while retaining its bounded audit receipt."""
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import update

    async with get_db_session() as db:
        database_now = _database_now(db)
        result = await db.execute(
            update(CronDeliveryOutbox)
            .where(
                CronDeliveryOutbox.id == claim.delivery_id,
                CronDeliveryOutbox.state == "processing",
                CronDeliveryOutbox.claim_token == claim.token,
                CronDeliveryOutbox.claim_owner == claim.owner_id,
                CronDeliveryOutbox.claim_expires_at.isnot(None),
                CronDeliveryOutbox.claim_expires_at >= database_now,
            )
            .values(
                state="dead_letter",
                claim_token=None,
                claim_owner=None,
                claim_expires_at=None,
                last_error=_bounded_error(error),
                updated_at=database_now,
            )
        )
    return result.rowcount == 1


async def _release_delivery(
    claim: DeliveryClaim,
    error: str,
    *,
    delay_seconds: int | None = None,
) -> bool:
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import update

    if delay_seconds is None:
        delay_seconds = min(3600, 2 ** min(max(1, claim.attempts), 11))
    async with get_db_session() as db:
        database_now = _database_now(db)
        result = await db.execute(
            update(CronDeliveryOutbox)
            .where(
                CronDeliveryOutbox.id == claim.delivery_id,
                CronDeliveryOutbox.state == "processing",
                CronDeliveryOutbox.claim_token == claim.token,
                CronDeliveryOutbox.claim_owner == claim.owner_id,
            )
            .values(
                state="pending",
                claim_token=None,
                claim_owner=None,
                claim_expires_at=None,
                available_at=_database_after(db, delay_seconds),
                last_error=_bounded_error(error),
                updated_at=database_now,
            )
        )
    return result.rowcount == 1


async def _deliver_session(
    claim: DeliveryClaim,
    *,
    run_fence: tuple[str, str, int] | None,
) -> DeliveryOutcome:
    from bus import bus
    from bus.events import CRON_JOB_INJECTED
    from session.session import (
        CronInjectionDeferred,
        inject_cron_message_pair_once,
    )

    payload = claim.payload
    delivery_fence = run_fence
    compaction_lease = None
    try:
        if await _session_delivery_needs_compaction(
            claim.session_id or payload["session_id"],
            claim.user_id,
            payload.get("job_name") or "",
            payload.get("result_text") or "",
        ):
            if delivery_fence is None:
                try:
                    from agent.driver import reserve_run

                    compaction_lease = await reserve_run(
                        claim.session_id or payload["session_id"],
                        claim.user_id,
                        initial_phase="finalizing",
                    )
                except Exception as exc:
                    from agent.driver import (
                        DriverBusyError,
                        DriverRecoveryRequiredError,
                    )

                    if isinstance(
                        exc, (DriverBusyError, DriverRecoveryRequiredError)
                    ):
                        raise CronInjectionDeferred(str(exc)) from exc
                    raise
                delivery_fence = (
                    claim.session_id or payload["session_id"],
                    compaction_lease.run_id,
                    compaction_lease.generation,
                )
            await _compact_before_session_delivery(
                claim.session_id or payload["session_id"],
                claim.user_id,
                run_fence=delivery_fence,
            )

        created_at = datetime.fromisoformat(payload["occurred_at"])
        await inject_cron_message_pair_once(
            claim.session_id or payload["session_id"],
            claim.user_id,
            delivery_id=claim.delivery_id,
            user_message_id=payload["user_message_id"],
            user_part_id=payload["user_part_id"],
            assistant_message_id=payload["assistant_message_id"],
            assistant_part_id=payload["assistant_part_id"],
            user_text=payload["user_text"],
            result_text=payload["result_text"],
            created_at=created_at,
            run_fence=delivery_fence,
        )
    except CronInjectionDeferred as exc:
        return DeliveryOutcome(
            success=False, error=str(exc), retry_delay_seconds=2
        )
    except LookupError as exc:
        # A deleted notify conversation cannot ever accept this result.  Keep
        # the terminal reason on the outbox row instead of retrying forever.
        return DeliveryOutcome(success=False, error=str(exc), terminal=True)
    finally:
        if compaction_lease is not None:
            try:
                await compaction_lease.release(session_status="idle")
            except Exception as exc:
                log.warning(
                    "Cron compaction lease release failed delivery=%s error_type=%s",
                    claim.delivery_id,
                    type(exc).__name__,
                )

    event = {
        "userId": claim.user_id,
        "sessionId": claim.session_id,
        "jobId": claim.job_id,
        "runId": claim.run_id,
        "jobName": payload.get("job_name"),
        "deliveryId": claim.delivery_id,
    }
    if run_fence is not None:
        event["generation"] = run_fence[2]
    await bus.publish_confirmed(CRON_JOB_INJECTED, event)
    return DeliveryOutcome(success=True)


async def _session_delivery_needs_compaction(
    session_id: str,
    user_id: str,
    job_name: str,
    result_text: str,
) -> bool:
    """Preserve the legacy 90% pre-injection overflow guard."""
    from agent.compaction import get_model_context_limit
    from session.session import get_session

    session = await get_session(session_id, user_id=user_id)
    if not session or not session.token_usage:
        return False
    inject_tokens = (len(job_name) + len(result_text)) // 4 + 100
    current_context = session.token_usage.context or 0
    limit = session.token_usage.limit or get_model_context_limit(
        session.model or ""
    )
    return current_context + inject_tokens > limit * 0.9


async def _compact_before_session_delivery(
    session_id: str,
    user_id: str,
    *,
    run_fence: tuple[str, str, int],
) -> None:
    """Compact under an exact Agent lease before the atomic Cron pair."""
    try:
        from agent.compaction import create_compaction, process_compaction
        from core.config import get_config
        from session.session import get_messages, get_session

        session = await get_session(session_id, user_id=user_id)
        messages = await get_messages(session_id, user_id=user_id)
        request = await create_compaction(
            session_id,
            auto=True,
            user_id=user_id,
            messages=messages,
            model_id=(session.model if session else ""),
            run_fence=run_fence,
        )
        if request is None:
            return
        messages = await get_messages(session_id, user_id=user_id)
        model_id = (session.model if session else "") or get_config().model
        await process_compaction(
            session_id,
            messages,
            model_id,
            auto=True,
            user_id=user_id,
            run_fence=run_fence,
        )
    except Exception as exc:
        from agent.driver import LeaseLostError

        if isinstance(exc, LeaseLostError):
            raise
        log.warning(
            "Pre-injection Cron compaction failed session=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )


async def _deliver_runlog(claim: DeliveryClaim) -> DeliveryOutcome:
    from cron.runlog import append_run_log

    payload = claim.payload
    ok = await append_run_log(
        payload["temp_session_id"],
        claim.user_id,
        payload["job"],
        payload["entry"],
        payload.get("locale") or "zh-CN",
        delivery_id=claim.delivery_id,
        filename_override=payload.get("filename"),
    )
    return DeliveryOutcome(
        success=ok,
        error=None if ok else "workspace runlog append failed",
    )


async def _deliver_webhook(claim: DeliveryClaim) -> DeliveryOutcome:
    from cron.delivery import dispatch_delivery

    payload = claim.payload
    result = await dispatch_delivery(
        payload["delivery"],
        job_name=payload.get("job_name") or "unnamed",
        job_id=claim.job_id,
        status=payload.get("status") or "error",
        summary_text=(
            payload.get("summary_text")
            if payload.get("status") == "ok"
            else payload.get("error")
        ),
        duration_ms=int(payload.get("duration_ms") or 0),
        delivery_id=claim.delivery_id,
        occurred_at=payload.get("occurred_at"),
    )
    return DeliveryOutcome(
        success=result.success,
        error=result.error,
        terminal=result.terminal,
        retry_delay_seconds=result.retry_delay_seconds,
    )


async def _deliver_event(claim: DeliveryClaim) -> DeliveryOutcome:
    from bus import bus
    from bus.events import CRON_JOB_COMPLETED, CRON_JOB_FAILED

    payload = claim.payload
    status = payload.get("status") or "error"
    event = {
        "userId": claim.user_id,
        "jobId": claim.job_id,
        "sessionId": claim.session_id,
        "jobName": payload.get("job_name"),
        "runId": claim.run_id,
        "durationMs": int(payload.get("duration_ms") or 0),
        "deliveryId": claim.delivery_id,
    }
    if status == "error":
        event["error"] = payload.get("error")
        await bus.publish_confirmed(CRON_JOB_FAILED, event)
    else:
        event["silent"] = bool(payload.get("silent"))
        await bus.publish_confirmed(CRON_JOB_COMPLETED, event)
    return DeliveryOutcome(success=True)


async def _deliver_claim(
    claim: DeliveryClaim,
    *,
    run_fence: tuple[str, str, int] | None,
) -> DeliveryOutcome:
    if claim.kind == "session":
        return await _deliver_session(claim, run_fence=run_fence)
    if claim.kind == "runlog":
        return await _deliver_runlog(claim)
    if claim.kind == "webhook":
        return await _deliver_webhook(claim)
    if claim.kind == "event":
        return await _deliver_event(claim)
    return DeliveryOutcome(
        success=False,
        terminal=True,
        error=f"unknown Cron delivery kind: {claim.kind}",
    )


async def process_one_delivery(
    *,
    owner_id: str = DELIVERY_OWNER_ID,
    kinds: Iterable[str] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    run_fence: tuple[str, str, int] | None = None,
) -> bool:
    """Claim and attempt one row; return False only when no row was due."""
    claimed, _delivered = await _process_one_delivery(
        owner_id=owner_id,
        kinds=kinds,
        session_id=session_id,
        user_id=user_id,
        run_fence=run_fence,
    )
    return claimed


async def _process_one_delivery(
    *,
    owner_id: str = DELIVERY_OWNER_ID,
    kinds: Iterable[str] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    run_fence: tuple[str, str, int] | None = None,
) -> tuple[bool, bool]:
    claim = await claim_delivery(
        owner_id=owner_id,
        kinds=kinds,
        session_id=session_id,
        user_id=user_id,
    )
    if claim is None:
        return False, False
    try:
        outcome = await _run_with_delivery_lease(
            claim,
            lambda: _deliver_claim(claim, run_fence=run_fence),
        )
    except asyncio.CancelledError:
        # Process shutdown is not evidence that the downstream effect is
        # poison. Return the row promptly for another replica/startup retry.
        await _release_delivery(claim, "Cron delivery worker stopped", delay_seconds=0)
        raise
    except BaseException as exc:
        error = str(exc) or type(exc).__name__
        if claim.attempts >= DELIVERY_MAX_ATTEMPTS:
            await _mark_dead_letter(
                claim,
                f"Attempt limit {DELIVERY_MAX_ATTEMPTS} reached: {error}",
            )
        else:
            await _release_delivery(claim, error)
        raise

    if outcome.success:
        marked = await _mark_delivered(
            claim,
        )
        if not marked:
            log.warning(
                "Cron delivery side effect completed after lease loss delivery=%s",
                claim.delivery_id,
            )
        completed = marked
    elif outcome.terminal or claim.attempts >= DELIVERY_MAX_ATTEMPTS:
        reason = outcome.error or "Cron delivery reached its attempt limit"
        if not outcome.terminal:
            reason = (
                f"Attempt limit {DELIVERY_MAX_ATTEMPTS} reached: {reason}"
            )
        completed = await _mark_dead_letter(claim, reason)
        if not completed:
            log.warning(
                "Cron dead-letter mark lost its lease delivery=%s",
                claim.delivery_id,
            )
    else:
        await _release_delivery(
            claim,
            outcome.error or "Cron delivery failed",
            delay_seconds=outcome.retry_delay_seconds,
        )
        completed = False
    return True, completed


async def drain_deliveries(
    *,
    owner_id: str = DELIVERY_OWNER_ID,
    kinds: Iterable[str] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    run_fence: tuple[str, str, int] | None = None,
    limit: int = DELIVERY_BATCH_SIZE,
) -> int:
    delivered = 0
    for _ in range(max(0, limit)):
        claimed, completed = await _process_one_delivery(
            owner_id=owner_id,
            kinds=kinds,
            session_id=session_id,
            user_id=user_id,
            run_fence=run_fence,
        )
        if not claimed:
            break
        if completed:
            delivered += 1
    return delivered


_workers: weakref.WeakSet["OutboxWorker"] = weakref.WeakSet()


def notify_outbox_workers() -> None:
    for worker in tuple(_workers):
        worker.wake()


class OutboxWorker:
    """One process-local dispatcher; database claims coordinate replicas."""

    def __init__(self, *, owner_id: str = DELIVERY_OWNER_ID):
        self.owner_id = owner_id
        self._task: asyncio.Task | None = None
        self._pulse_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.last_heartbeat_monotonic: float | None = None
        self.consecutive_dispatch_errors = 0

    async def start(self) -> None:
        if (
            self._task is not None
            and not self._task.done()
            and self._pulse_task is not None
            and not self._pulse_task.done()
        ):
            return
        if self._task is not None or self._pulse_task is not None:
            await self.stop()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.last_heartbeat_monotonic = time.monotonic()
        self.consecutive_dispatch_errors = 0
        self._task = asyncio.create_task(self._run())
        self._pulse_task = asyncio.create_task(self._pulse())
        _workers.add(self)
        await asyncio.sleep(0)
        if self._task.done():
            self._pulse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pulse_task
            await self._task

    async def stop(self) -> None:
        task = self._task
        pulse_task = self._pulse_task
        if task is None and pulse_task is None:
            return
        self._stop.set()
        self._wake.set()
        for running_task in (task, pulse_task):
            if running_task is not None:
                running_task.cancel()
        for running_task in (task, pulse_task):
            if running_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await running_task
        self._task = None
        self._pulse_task = None
        self.last_heartbeat_monotonic = None
        _workers.discard(self)

    def wake(self) -> None:
        self._wake.set()

    def readiness(self) -> dict:
        running = bool(
            self._task is not None
            and not self._task.done()
            and self._pulse_task is not None
            and not self._pulse_task.done()
        )
        fresh = bool(
            self.last_heartbeat_monotonic is not None
            and time.monotonic() - self.last_heartbeat_monotonic
            < OUTBOX_WORKER_STALE_SECONDS
        )
        dispatch_healthy = (
            self.consecutive_dispatch_errors
            < OUTBOX_MAX_CONSECUTIVE_DISPATCH_ERRORS
        )
        return {
            "running": running,
            "heartbeat_fresh": fresh,
            "dispatch_healthy": dispatch_healthy,
            "ready": running and fresh and dispatch_healthy,
        }

    async def _pulse(self) -> None:
        """Process-local liveness pulse independent of slow side effects."""
        while not self._stop.is_set():
            self.last_heartbeat_monotonic = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=OUTBOX_WORKER_PULSE_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            self.last_heartbeat_monotonic = time.monotonic()
            try:
                attempted = await drain_deliveries(owner_id=self.owner_id)
                self.consecutive_dispatch_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.consecutive_dispatch_errors += 1
                log.error(
                    "Cron outbox dispatch failed error_type=%s",
                    type(exc).__name__,
                )
                attempted = 0
            self.last_heartbeat_monotonic = time.monotonic()
            if attempted >= DELIVERY_BATCH_SIZE:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=DELIVERY_POLL_SECONDS
                )
            except asyncio.TimeoutError:
                pass
