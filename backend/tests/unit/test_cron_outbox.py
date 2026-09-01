"""Durable Cron settlement, claim, retry, and local idempotency semantics."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from cron.lease import claim_job
from cron.outbox import OutboxWorker, process_one_delivery, stable_delivery_id
from cron.timer import TimerState, _apply_job_result


NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


async def _identity(*, with_session: bool = True):
    from db.base import get_db_session
    from db.models.project import Project
    from db.models.session import Session
    from db.models.user import User

    suffix = uuid.uuid4().hex[:10]
    user_id = f"u_{suffix}"
    project_id = f"proj_{suffix}"
    session_id = f"sess_{suffix}" if with_session else None
    now = NOW()
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            role="user",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="project",
            slug=project_id,
            created_at=now,
            updated_at=now,
        ))
        if session_id:
            db.add(Session(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                title="main",
                status="idle",
                kind="normal",
                token_usage={},
                created_at=now,
                updated_at=now,
            ))
    return user_id, project_id, session_id


async def _claimed_run(
    *,
    with_session: bool = True,
    delivery: dict | None = None,
):
    from db.base import get_db_session
    from db.models.cron import CronJob, CronRun

    user_id, project_id, session_id = await _identity(
        with_session=with_session
    )
    suffix = uuid.uuid4().hex[:10]
    job_id = f"cron_{suffix}"
    run_id = f"cron_run_{suffix}"
    now = NOW()
    async with get_db_session() as db:
        db.add(CronJob(
            id=job_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            name="daily",
            enabled=True,
            schedule={
                "kind": "every",
                "every_ms": 600_000,
                "anchor_ms": int(now.timestamp() * 1000),
            },
            task_prompt="report",
            delivery=delivery or {},
            delete_after_run=False,
            max_retries=3,
            next_run_at=now - timedelta(minutes=1),
            created_at=now,
            updated_at=now,
        ))
    claim = await claim_job(job_id, owner_id=f"worker-{suffix}")
    assert claim is not None
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id,
            job_id=job_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            temp_session_id=f"sess_temp_{suffix}",
            claim_token=claim.token,
            claim_generation=claim.generation,
            claim_owner=claim.owner_id,
            status="running",
            task_prompt="report",
            started_at=now,
        ))
    return claim, run_id, user_id, project_id, session_id


async def _outbox_rows(run_id: str):
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import select

    async with get_db_session() as db:
        return list((await db.execute(
            select(CronDeliveryOutbox)
            .where(CronDeliveryOutbox.run_id == run_id)
            .order_by(CronDeliveryOutbox.kind)
        )).scalars().all())


async def test_exact_settlement_finalizes_run_and_creates_effect_matrix():
    claim, run_id, *_ = await _claimed_run(
        delivery={
            "mode": "webhook",
            "webhook_url": "https://8.8.8.8/hook",
        }
    )
    result = {
        "status": "ok",
        "summary_text": "report ready",
        "run_id": run_id,
        "duration_ms": 123,
        "tokens": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
        "silent": False,
        "locale": "en-US",
        "ended_at": NOW(),
    }

    assert await _apply_job_result(
        TimerState(), claim.job_id, result, claim=claim
    ) is True

    from db.base import get_db_session
    from db.models.cron import CronJob, CronRun
    from sqlalchemy import select

    async with get_db_session() as db:
        job = (await db.execute(
            select(CronJob).where(CronJob.id == claim.job_id)
        )).scalar_one()
        run = (await db.execute(
            select(CronRun).where(CronRun.id == run_id)
        )).scalar_one()
    assert job.run_token is None and job.total_runs == 1
    assert run.status == "ok" and run.total_tokens == 15
    assert run.injected is False
    rows = await _outbox_rows(run_id)
    assert {row.kind for row in rows} == {
        "event", "runlog", "session", "webhook"
    }
    assert len({row.id for row in rows}) == 4
    assert all(row.id == stable_delivery_id(run_id, row.kind) for row in rows)


async def test_silent_and_page_jobs_keep_original_delivery_semantics():
    silent_claim, silent_run, *_ = await _claimed_run(
        delivery={
            "mode": "webhook",
            "webhook_url": "https://8.8.8.8/hook",
        }
    )
    assert await _apply_job_result(
        TimerState(),
        silent_claim.job_id,
        {
            "status": "ok",
            "summary_text": "NO_REPLY",
            "run_id": silent_run,
            "silent": True,
            "duration_ms": 1,
            "ended_at": NOW(),
        },
        claim=silent_claim,
    )
    assert {row.kind for row in await _outbox_rows(silent_run)} == {
        "event", "runlog"
    }

    page_claim, page_run, *_ = await _claimed_run(with_session=False)
    assert await _apply_job_result(
        TimerState(),
        page_claim.job_id,
        {
            "status": "ok",
            "summary_text": "page report",
            "run_id": page_run,
            "silent": False,
            "duration_ms": 1,
            "ended_at": NOW(),
        },
        claim=page_claim,
    )
    assert {row.kind for row in await _outbox_rows(page_run)} == {
        "event", "runlog"
    }

    failed_claim, failed_run, *_ = await _claimed_run(
        delivery={
            "mode": "webhook",
            "webhook_url": "https://8.8.8.8/hook",
        }
    )
    assert await _apply_job_result(
        TimerState(),
        failed_claim.job_id,
        {
            "status": "error",
            "error": "agent failed",
            "run_id": failed_run,
            "duration_ms": 2,
            "ended_at": NOW(),
        },
        claim=failed_claim,
    )
    assert {row.kind for row in await _outbox_rows(failed_run)} == {
        "event", "runlog", "webhook"
    }


async def test_deleted_or_stale_claim_cannot_create_outbox():
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    claim, run_id, *_ = await _claimed_run()
    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == claim.job_id)
            .values(is_deleted=True, enabled=False)
        )
    assert await _apply_job_result(
        TimerState(),
        claim.job_id,
        {"status": "ok", "run_id": run_id, "duration_ms": 1},
        claim=claim,
    ) is False
    assert await _outbox_rows(run_id) == []

    stale_claim, stale_run, *_ = await _claimed_run()
    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == stale_claim.job_id)
            .values(lease_expires_at=NOW() - timedelta(seconds=1))
        )
    replacement = await claim_job(
        stale_claim.job_id, owner_id="replacement-worker"
    )
    assert replacement is not None
    assert replacement.generation == stale_claim.generation + 1
    assert await _apply_job_result(
        TimerState(),
        stale_claim.job_id,
        {"status": "ok", "run_id": stale_run, "duration_ms": 1},
        claim=stale_claim,
    ) is False
    assert await _outbox_rows(stale_run) == []


async def test_two_workers_have_one_claim_winner(monkeypatch):
    from cron import outbox
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import delete

    delivery_id = stable_delivery_id(f"run_{uuid.uuid4().hex}", "event")
    now = NOW()
    async with get_db_session() as db:
        await db.execute(delete(CronDeliveryOutbox))
        db.add(CronDeliveryOutbox(
            id=delivery_id,
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            user_id=f"u_{uuid.uuid4().hex[:12]}",
            kind="event",
            payload={"status": "ok"},
            state="pending",
            attempts=0,
            available_at=now - timedelta(seconds=2),
            created_at=now,
            updated_at=now,
        ))

    calls = 0

    async def delivered(_claim):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return outbox.DeliveryOutcome(success=True)

    monkeypatch.setattr(outbox, "_deliver_event", delivered)
    outcomes = await asyncio.gather(
        process_one_delivery(owner_id="replica-a"),
        process_one_delivery(owner_id="replica-b"),
    )
    assert sorted(outcomes) == [False, True]
    assert calls == 1


async def test_crash_after_session_commit_retries_without_duplicate_messages(
    monkeypatch,
):
    from cron import outbox
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from db.models.message import Message
    from db.models.part import Part
    from sqlalchemy import func, select, update

    user_id, project_id, session_id = await _identity()
    suffix = uuid.uuid4().hex[:10]
    run_id = f"cron_run_{suffix}"
    job_id = f"cron_{suffix}"
    delivery_id = stable_delivery_id(run_id, "session")
    now = NOW()
    payload = {
        "session_id": session_id,
        "job_name": "daily",
        "occurred_at": now.isoformat(),
        "user_message_id": f"message_{uuid.uuid4().hex[:26]}",
        "user_part_id": f"part_{uuid.uuid4().hex[:26]}",
        "assistant_message_id": f"message_{uuid.uuid4().hex[:26]}",
        "assistant_part_id": f"part_{uuid.uuid4().hex[:26]}",
        "user_text": "scheduled report",
        "result_text": "done",
    }
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id,
            job_id=job_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            status="ok",
            summary_text="done",
            injected=False,
            started_at=now,
            ended_at=now,
        ))
        db.add(CronDeliveryOutbox(
            id=delivery_id,
            run_id=run_id,
            job_id=job_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            kind="session",
            payload=payload,
            state="pending",
            attempts=0,
            available_at=now - timedelta(seconds=2),
            created_at=now,
            updated_at=now,
        ))

    real_mark = outbox._mark_delivered

    async def crash_before_mark(*_args, **_kwargs):
        return False

    monkeypatch.setattr(outbox, "_mark_delivered", crash_before_mark)
    assert await process_one_delivery(
        owner_id="replica-crash",
        kinds=("session",),
        session_id=session_id,
        user_id=user_id,
    ) is True

    async with get_db_session() as db:
        assert (await db.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id == session_id
            )
        )).scalar_one() == 2
        assert (await db.execute(
            select(func.count()).select_from(Part).where(
                Part.session_id == session_id
            )
        )).scalar_one() == 2
        await db.execute(
            update(CronDeliveryOutbox)
            .where(CronDeliveryOutbox.id == delivery_id)
            .values(
                state="pending",
                claim_token=None,
                claim_owner=None,
                claim_expires_at=None,
                available_at=NOW() - timedelta(seconds=2),
            )
        )

    monkeypatch.setattr(outbox, "_mark_delivered", real_mark)
    assert await process_one_delivery(
        owner_id="replica-retry",
        kinds=("session",),
        session_id=session_id,
        user_id=user_id,
    ) is True
    async with get_db_session() as db:
        assert (await db.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id == session_id
            )
        )).scalar_one() == 2
        run = (await db.execute(
            select(CronRun).where(CronRun.id == run_id)
        )).scalar_one()
        row = (await db.execute(
            select(CronDeliveryOutbox).where(
                CronDeliveryOutbox.id == delivery_id
            )
        )).scalar_one()
    assert run.injected is True
    assert row.state == "delivered"


async def test_worker_retries_pending_rows_on_startup(monkeypatch):
    from cron import outbox
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import delete, select

    now = NOW()
    delivery_id = stable_delivery_id(f"run_{uuid.uuid4().hex}", "event")
    async with get_db_session() as db:
        await db.execute(delete(CronDeliveryOutbox))
        db.add(CronDeliveryOutbox(
            id=delivery_id,
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            user_id=f"u_{uuid.uuid4().hex[:12]}",
            kind="event",
            payload={"status": "ok"},
            state="pending",
            attempts=0,
            available_at=now - timedelta(seconds=2),
            created_at=now,
            updated_at=now,
        ))

    async def delivered(_claim):
        return outbox.DeliveryOutcome(success=True)

    monkeypatch.setattr(outbox, "_deliver_event", delivered)
    worker = OutboxWorker(owner_id="startup-replica")
    await worker.start()
    try:
        # The suite's in-memory SQLite uses one physical connection; let the
        # worker finish its first transaction before polling from another
        # AsyncSession (file-backed desktop SQLite has separate connections).
        await asyncio.sleep(0.1)
        for _ in range(100):
            async with get_db_session() as db:
                state = (await db.execute(
                    select(CronDeliveryOutbox.state).where(
                        CronDeliveryOutbox.id == delivery_id
                    )
                )).scalar_one()
            if state == "delivered":
                break
            await asyncio.sleep(0.01)
        assert state == "delivered"
        assert worker.readiness()["ready"] is True
    finally:
        await worker.stop()


async def test_unrecoverable_runlog_reaches_bounded_dead_letter(monkeypatch):
    from cron import outbox
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import delete, select

    now = NOW()
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    delivery_id = stable_delivery_id(run_id, "runlog")
    async with get_db_session() as db:
        await db.execute(delete(CronDeliveryOutbox))
        db.add(CronDeliveryOutbox(
            id=delivery_id,
            run_id=run_id,
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            user_id=f"u_{uuid.uuid4().hex[:12]}",
            kind="runlog",
            payload={},
            state="pending",
            attempts=0,
            available_at=now - timedelta(seconds=2),
            created_at=now,
            updated_at=now,
        ))

    monkeypatch.setattr(outbox, "DELIVERY_MAX_ATTEMPTS", 2)

    async def permanently_broken(_claim):
        return outbox.DeliveryOutcome(
            success=False,
            error="unrecoverable:" + ("x" * 10_000),
            retry_delay_seconds=0,
        )

    monkeypatch.setattr(outbox, "_deliver_runlog", permanently_broken)
    assert await process_one_delivery(owner_id="dead-letter-1") is True
    assert await process_one_delivery(owner_id="dead-letter-2") is True
    assert await process_one_delivery(owner_id="dead-letter-3") is False

    async with get_db_session() as db:
        row = (
            await db.execute(
                select(CronDeliveryOutbox).where(
                    CronDeliveryOutbox.id == delivery_id
                )
            )
        ).scalar_one()
    assert row.state == "dead_letter"
    assert row.attempts == 2
    assert "Attempt limit 2 reached" in (row.last_error or "")
    assert len(row.last_error or "") <= outbox.DELIVERY_LAST_ERROR_MAX_CHARS
    assert row.claim_token is None and row.claim_expires_at is None


async def test_delivery_heartbeat_uses_monotonic_elapsed_time(monkeypatch):
    from cron import outbox

    renewals = 0

    async def renew(_claim):
        nonlocal renewals
        renewals += 1
        # Deliberately far behind this process's wall clock. This is a valid DB
        # timestamp when the database host clock differs from the worker.
        return NOW() - timedelta(days=30)

    monkeypatch.setattr(outbox, "renew_delivery", renew)
    monkeypatch.setattr(outbox, "DELIVERY_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(outbox, "DELIVERY_LEASE_TTL_SECONDS", 0.05)
    claim = outbox.DeliveryClaim(
        delivery_id="delivery-monotonic",
        run_id="run-monotonic",
        job_id="job-monotonic",
        user_id="user-monotonic",
        project_id=None,
        session_id=None,
        kind="event",
        payload={},
        attempts=1,
        token="token-monotonic",
        owner_id="owner-monotonic",
        lease_expires_at=NOW() - timedelta(days=30),
    )

    async def slow_but_healthy_work():
        await asyncio.sleep(0.03)
        return "done"

    assert await outbox._run_with_delivery_lease(
        claim, slow_but_healthy_work
    ) == "done"
    assert renewals >= 2


async def test_worker_readiness_stays_fresh_during_long_webhook(monkeypatch):
    from cron import outbox
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from sqlalchemy import delete

    now = NOW()
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    async with get_db_session() as db:
        await db.execute(delete(CronDeliveryOutbox))
        db.add(CronDeliveryOutbox(
            id=stable_delivery_id(run_id, "webhook"),
            run_id=run_id,
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            user_id=f"u_{uuid.uuid4().hex[:12]}",
            kind="webhook",
            payload={},
            state="pending",
            attempts=0,
            available_at=now - timedelta(seconds=2),
            created_at=now,
            updated_at=now,
        ))

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_webhook(_claim):
        entered.set()
        await release.wait()
        return outbox.DeliveryOutcome(success=True)

    monkeypatch.setattr(outbox, "_deliver_webhook", slow_webhook)
    monkeypatch.setattr(outbox, "OUTBOX_WORKER_PULSE_SECONDS", 0.005)
    monkeypatch.setattr(outbox, "OUTBOX_WORKER_STALE_SECONDS", 0.03)
    worker = OutboxWorker(owner_id="slow-webhook-worker")
    await worker.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.06)
        readiness = worker.readiness()
        assert readiness["running"] is True
        assert readiness["heartbeat_fresh"] is True
        assert readiness["ready"] is True
    finally:
        release.set()
        await worker.stop()


async def test_repeated_dispatcher_errors_fail_then_recover_readiness(monkeypatch):
    from cron import outbox

    fail = True
    calls = 0

    async def drain(**_kwargs):
        nonlocal calls
        calls += 1
        if fail:
            raise RuntimeError("database unavailable")
        return 0

    monkeypatch.setattr(outbox, "drain_deliveries", drain)
    monkeypatch.setattr(outbox, "DELIVERY_POLL_SECONDS", 0.005)
    monkeypatch.setattr(outbox, "OUTBOX_WORKER_PULSE_SECONDS", 0.005)
    monkeypatch.setattr(outbox, "OUTBOX_WORKER_STALE_SECONDS", 0.05)
    monkeypatch.setattr(outbox, "OUTBOX_MAX_CONSECUTIVE_DISPATCH_ERRORS", 2)
    worker = OutboxWorker(owner_id="dispatcher-readiness-worker")
    await worker.start()
    try:
        for _ in range(100):
            if worker.consecutive_dispatch_errors >= 2:
                break
            await asyncio.sleep(0.005)
        assert worker.readiness()["ready"] is False

        fail = False
        worker.wake()
        for _ in range(100):
            if worker.consecutive_dispatch_errors == 0 and calls >= 3:
                break
            await asyncio.sleep(0.005)
        assert worker.readiness()["ready"] is True
    finally:
        await worker.stop()
