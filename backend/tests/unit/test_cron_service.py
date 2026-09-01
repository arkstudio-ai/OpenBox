"""CronService facade: validated writes, stagger on creation, status liveness."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

import cron.service as service_mod
from cron.service import CronService, _job_to_dict, _run_to_dict, _utc_iso
from cron.types import CronJobCreate, CronScheduleCron, CronScheduleEvery


class FakeSession:
    def __init__(self, sid):
        self.id = sid
        self.model = "m"


@pytest.fixture
def owned_session(monkeypatch):
    import session.session as sess
    import project.workspace as ws

    async def fake_get_session(sid, user_id=None, **kw):
        return FakeSession(sid)

    async def fake_get_project(pid, user_id):
        return object()

    monkeypatch.setattr(sess, "get_session", fake_get_session)
    monkeypatch.setattr(ws, "get_project", fake_get_project)


@pytest.fixture
def svc(monkeypatch):
    s = CronService()
    # arm_timer touches the event loop scheduling machinery; keep it inert here
    monkeypatch.setattr(service_mod, "arm_timer", lambda state: None)
    return s


def _create(**overrides) -> CronJobCreate:
    base = dict(
        project_id="proj_" + uuid.uuid4().hex[:8],
        session_id="sess_" + uuid.uuid4().hex[:8],
        name="daily",
        schedule=CronScheduleCron(expr="0 9 * * *", tz="UTC"),
        task_prompt="report",
    )
    base.update(overrides)
    return CronJobCreate(**base)


def test_cron_api_timestamps_are_explicit_utc() -> None:
    naive = datetime(2026, 8, 31, 12, 6, 28)
    aware = naive.replace(tzinfo=timezone.utc)

    assert _utc_iso(naive) == "2026-08-31T12:06:28Z"
    assert _utc_iso(aware) == "2026-08-31T12:06:28Z"
    assert _utc_iso(None) is None

    job = SimpleNamespace(
        id="cron_test",
        user_id="u",
        project_id="p",
        session_id=None,
        name="test",
        description="",
        enabled=True,
        schedule={},
        task_prompt="test",
        agent="build",
        model=None,
        timeout_seconds=30,
        delivery={},
        delete_after_run=False,
        next_run_at=naive,
        last_run_at=naive,
        last_status="ok",
        last_error=None,
        last_duration_ms=1,
        consecutive_errors=0,
        total_runs=1,
        total_successes=1,
        total_failures=0,
        running_at=None,
        created_at=naive,
        updated_at=naive,
    )
    run = SimpleNamespace(
        id="run_test",
        job_id=job.id,
        temp_session_id=None,
        status="ok",
        error_message=None,
        task_prompt="test",
        summary_text="done",
        injected=False,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        duration_ms=1,
        started_at=naive,
        ended_at=naive,
    )

    assert _job_to_dict(job)["last_run_at"].endswith("Z")
    assert _run_to_dict(run)["started_at"].endswith("Z")


async def test_add_applies_validation(svc, owned_session):
    with pytest.raises(ValueError, match="more often"):
        await svc.add("u_" + uuid.uuid4().hex[:6], _create(schedule=CronScheduleEvery(every_ms=1000)))


async def test_add_staggers_top_of_hour_jobs(svc, owned_session):
    from cron.schedule import stagger_ms_for

    result = await svc.add("u_" + uuid.uuid4().hex[:6], _create())
    assert result["next_run_at"] is not None
    from datetime import datetime

    next_run = datetime.fromisoformat(result["next_run_at"])
    offset_s = (stagger_ms_for(result["id"]) // 1000)
    # Fires at 09:00 UTC plus the deterministic per-job offset (< 5 min)
    assert next_run.hour == 9
    assert next_run.minute == offset_s // 60
    assert 0 <= next_run.minute < 5


async def test_pause_and_resume_roundtrip(svc, owned_session):
    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(user_id, _create(schedule=CronScheduleEvery(every_ms=600_000)))

    assert await svc.pause_all(user_id) == 1
    job = await svc.get_job(created["id"], user_id)
    assert job["enabled"] is False and job["next_run_at"] is None

    assert await svc.resume_all(user_id) == 1
    job = await svc.get_job(created["id"], user_id)
    assert job["enabled"] is True and job["next_run_at"] is not None


async def test_status_reports_liveness(svc):
    status = await svc.status()
    assert status["running"] is False
    assert status["healthy"] is False
    assert "last_tick_at" in status and "next_run_at" in status

    svc._started = True
    svc._state.last_tick_at_ms = __import__("time").time() * 1000
    svc._outbox_worker.readiness = lambda: {
        "running": True,
        "heartbeat_fresh": True,
        "ready": True,
    }
    status = await svc.status()
    assert status["healthy"] is True

    svc._state.last_tick_at_ms = 0
    readiness = svc.readiness_status()
    assert readiness["started"] is True
    assert readiness["heartbeat_fresh"] is False
    assert readiness["ready"] is False


async def test_start_publishes_started_only_after_every_stage(monkeypatch):
    import cron.recovery as recovery_mod
    import cron.schema as schema_mod
    import cron.outbox as outbox_mod

    service = CronService()
    order: list[str] = []

    async def schema():
        assert service._started is False
        order.append("schema")

    async def recovery():
        assert service._started is False
        order.append("recovery")

    async def recompute():
        assert service._started is False
        order.append("recompute")

    async def outbox_start():
        assert service._started is False
        order.append("outbox")

    async def materialize():
        order.append("materialize")
        return 0

    async def outbox_stop():
        return None

    def arm(_state):
        assert service._started is False
        order.append("arm")

    monkeypatch.setattr(schema_mod, "ensure_desktop_cron_lease_schema", schema)
    monkeypatch.setattr(recovery_mod, "recover_on_startup", recovery)
    monkeypatch.setattr(service, "_recompute_all", recompute)
    monkeypatch.setattr(service._outbox_worker, "start", outbox_start)
    monkeypatch.setattr(service._outbox_worker, "stop", outbox_stop)
    monkeypatch.setattr(
        service._outbox_worker,
        "readiness",
        lambda: {"running": True, "heartbeat_fresh": True, "ready": True},
    )
    monkeypatch.setattr(service_mod, "arm_timer", arm)
    monkeypatch.setattr(
        outbox_mod,
        "materialize_legacy_pending_session_deliveries",
        materialize,
    )

    await service.start()

    assert order == [
        "schema", "recovery", "recompute", "materialize", "outbox", "arm"
    ]
    assert service._started is True
    assert service.readiness_status()["ready"] is True


@pytest.mark.parametrize(
    "failing_step",
    ["schema", "recovery", "recompute", "materialize", "outbox", "arm"],
)
async def test_start_failure_resets_state_and_remains_retryable(
    monkeypatch,
    failing_step,
):
    import cron.recovery as recovery_mod
    import cron.schema as schema_mod
    import cron.outbox as outbox_mod

    service = CronService()
    fail = True
    stopped: list[bool] = []

    def maybe_fail(step: str):
        if fail and failing_step == step:
            raise RuntimeError(f"{step} failed")

    async def schema():
        maybe_fail("schema")

    async def recovery():
        maybe_fail("recovery")

    async def recompute():
        maybe_fail("recompute")

    async def outbox_start():
        maybe_fail("outbox")

    async def materialize():
        maybe_fail("materialize")
        return 0

    async def outbox_stop():
        return None

    def arm(_state):
        maybe_fail("arm")

    monkeypatch.setattr(schema_mod, "ensure_desktop_cron_lease_schema", schema)
    monkeypatch.setattr(recovery_mod, "recover_on_startup", recovery)
    monkeypatch.setattr(service, "_recompute_all", recompute)
    monkeypatch.setattr(service._outbox_worker, "start", outbox_start)
    monkeypatch.setattr(service._outbox_worker, "stop", outbox_stop)
    monkeypatch.setattr(
        service._outbox_worker,
        "readiness",
        lambda: {"running": True, "heartbeat_fresh": True, "ready": True},
    )
    monkeypatch.setattr(service_mod, "arm_timer", arm)
    monkeypatch.setattr(
        outbox_mod,
        "materialize_legacy_pending_session_deliveries",
        materialize,
    )
    monkeypatch.setattr(service_mod, "stop_timer", lambda _state: stopped.append(True))

    with pytest.raises(RuntimeError, match=failing_step):
        await service.start()

    assert stopped == [True]
    assert service._started is False
    assert service._state.last_tick_at_ms is None
    assert service.readiness_status()["ready"] is False

    fail = False
    await service.start()
    assert service._started is True


async def test_runs_include_temp_session_link(svc, owned_session):
    from datetime import datetime, timezone
    from db.base import get_db_session
    from db.models.cron import CronRun

    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(user_id, _create(schedule=CronScheduleEvery(every_ms=600_000)))
    async with get_db_session() as db:
        db.add(CronRun(
            id="cron_run_" + uuid.uuid4().hex[:8],
            job_id=created["id"],
            user_id=user_id,
            session_id="s",
            temp_session_id="sess_temp_42",
            status="ok",
            started_at=datetime.now(timezone.utc),
        ))

    runs = await svc.list_runs(created["id"], user_id)
    assert runs and runs[0]["temp_session_id"] == "sess_temp_42"


async def test_concurrent_manual_runs_share_the_atomic_claim(svc, owned_session):
    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(
        user_id,
        _create(schedule=CronScheduleEvery(every_ms=600_000)),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def execute(job):
        calls.append(job)
        entered.set()
        await release.wait()
        return {"status": "ok"}

    svc.set_executor(execute)
    first = await svc.run(created["id"], user_id)
    second = await svc.run(created["id"], user_id)
    assert first == {"ok": True, "status": "triggered"}
    assert second == {"ok": False, "reason": "already-running"}

    await asyncio.wait_for(entered.wait(), timeout=1)
    assert len(calls) == 1
    assert calls[0]["project_id"] is not None
    assert calls[0].get("_cron_claim")
    release.set()

    # Do not leave the service's background task alive beyond the test.
    for _ in range(50):
        job = await svc.get_job(created["id"], user_id)
        if not job["running"]:
            break
        await asyncio.sleep(0.01)
    assert job["running"] is False


async def test_manual_run_can_atomically_take_over_an_expired_lease(svc, owned_session):
    from datetime import datetime, timedelta, timezone
    from cron.lease import claim_job
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(
        user_id,
        _create(schedule=CronScheduleEvery(every_ms=600_000)),
    )
    expired = await claim_job(
        created["id"], user_id=user_id, require_enabled=False,
    )
    assert expired is not None
    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == created["id"])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    completed = asyncio.Event()

    async def execute(job):
        completed.set()
        return {"status": "ok"}

    svc.set_executor(execute)
    assert await svc.run(created["id"], user_id) == {
        "ok": True,
        "status": "triggered",
    }
    await asyncio.wait_for(completed.wait(), timeout=1)
    for _ in range(50):
        job = await svc.get_job(created["id"], user_id)
        if not job["running"]:
            break
        await asyncio.sleep(0.01)
    assert job["running"] is False


async def test_remove_closes_exact_run_and_fences_late_heartbeat_and_settlement(
    svc,
    owned_session,
):
    from datetime import datetime, timezone

    from cron.lease import claim_job, renew_lease
    from cron.timer import _apply_job_result
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronJob, CronRun
    from sqlalchemy import func, select

    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(
        user_id,
        _create(schedule=CronScheduleEvery(every_ms=600_000)),
    )
    claim = await claim_job(
        created["id"],
        user_id=user_id,
        require_enabled=False,
        owner_id="delete-owner",
    )
    assert claim is not None
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    unrelated_run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add_all([
            CronRun(
                id=run_id,
                job_id=created["id"],
                user_id=user_id,
                status="running",
                claim_token=claim.token,
                claim_generation=claim.generation,
                claim_owner=claim.owner_id,
                started_at=datetime.now(timezone.utc),
            ),
            CronRun(
                id=unrelated_run_id,
                job_id=created["id"],
                user_id=user_id,
                status="running",
                claim_token="different-token",
                claim_generation=claim.generation + 1,
                claim_owner="different-owner",
                started_at=datetime.now(timezone.utc),
            ),
        ])

    assert await svc.remove(created["id"], user_id) == {"ok": True}
    assert await renew_lease(claim) is None
    assert await _apply_job_result(
        svc._state,
        created["id"],
        {
            "status": "ok",
            "run_id": run_id,
            "duration_ms": 1,
            "ended_at": datetime.now(timezone.utc),
        },
        claim=claim,
    ) is False

    async with get_db_session() as db:
        job = (
            await db.execute(select(CronJob).where(CronJob.id == created["id"]))
        ).scalar_one()
        runs = {
            row.id: row
            for row in (
                await db.execute(
                    select(CronRun).where(
                        CronRun.id.in_([run_id, unrelated_run_id])
                    )
                )
            ).scalars()
        }
        deliveries = await db.scalar(
            select(func.count())
            .select_from(CronDeliveryOutbox)
            .where(CronDeliveryOutbox.run_id == run_id)
        )

    assert job.is_deleted is True and job.enabled is False
    assert job.run_token is None and job.running_at is None
    assert runs[run_id].status == "canceled"
    assert "deleted" in (runs[run_id].error_message or "").lower()
    assert runs[run_id].ended_at is not None
    assert runs[unrelated_run_id].status == "running"
    assert deliveries == 0


async def test_remove_racing_heartbeat_always_leaves_claim_revoked(
    svc,
    owned_session,
):
    from datetime import datetime, timezone

    from cron.lease import claim_job, renew_lease
    from db.base import get_db_session
    from db.models.cron import CronJob, CronRun
    from sqlalchemy import select

    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(
        user_id,
        _create(schedule=CronScheduleEvery(every_ms=600_000)),
    )
    claim = await claim_job(
        created["id"],
        user_id=user_id,
        require_enabled=False,
        owner_id="heartbeat-race-owner",
    )
    assert claim is not None
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id,
            job_id=created["id"],
            user_id=user_id,
            status="running",
            claim_token=claim.token,
            claim_generation=claim.generation,
            claim_owner=claim.owner_id,
            started_at=datetime.now(timezone.utc),
        ))

    await asyncio.gather(
        renew_lease(claim),
        svc.remove(created["id"], user_id),
    )

    assert await renew_lease(claim) is None
    async with get_db_session() as db:
        job = (
            await db.execute(select(CronJob).where(CronJob.id == created["id"]))
        ).scalar_one()
        run = (
            await db.execute(select(CronRun).where(CronRun.id == run_id))
        ).scalar_one()
    assert job.is_deleted is True and job.run_token is None
    assert run.status == "canceled"


async def test_remove_and_exact_settlement_race_has_no_running_orphan(
    svc,
    owned_session,
):
    from datetime import datetime, timezone

    from cron.lease import claim_job
    from cron.timer import _apply_job_result
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronJob, CronRun
    from sqlalchemy import func, select

    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(
        user_id,
        _create(schedule=CronScheduleEvery(every_ms=600_000)),
    )
    claim = await claim_job(
        created["id"],
        user_id=user_id,
        require_enabled=False,
        owner_id="settle-delete-race",
    )
    assert claim is not None
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id,
            job_id=created["id"],
            user_id=user_id,
            session_id=None,
            status="running",
            claim_token=claim.token,
            claim_generation=claim.generation,
            claim_owner=claim.owner_id,
            started_at=datetime.now(timezone.utc),
        ))

    remove_result, applied = await asyncio.gather(
        svc.remove(created["id"], user_id),
        _apply_job_result(
            svc._state,
            created["id"],
            {
                "status": "ok",
                "run_id": run_id,
                "summary_text": "done",
                "duration_ms": 1,
                "ended_at": datetime.now(timezone.utc),
            },
            claim=claim,
        ),
    )
    assert remove_result == {"ok": True}

    async with get_db_session() as db:
        job = await db.scalar(
            select(CronJob).where(CronJob.id == created["id"])
        )
        run = await db.scalar(select(CronRun).where(CronRun.id == run_id))
        deliveries = await db.scalar(
            select(func.count())
            .select_from(CronDeliveryOutbox)
            .where(CronDeliveryOutbox.run_id == run_id)
        )
    assert job.is_deleted is True and job.run_token is None
    assert run.status in {"canceled", "ok"}
    assert run.status != "running"
    if run.status == "canceled":
        assert applied is False and deliveries == 0
    else:
        # Settlement acquired the row lock first and committed before deletion;
        # those already-committed receipts remain valid.
        assert applied is True and deliveries > 0


async def test_long_running_job_keeps_scheduler_readiness_fresh(monkeypatch):
    import cron.reaper as reaper_mod
    import cron.timer as timer_mod
    import cron.types as types_mod
    import cron.warmup as warmup_mod
    import video.job_recovery as video_recovery_mod

    service = CronService()
    service._started = True
    service._outbox_worker.readiness = lambda: {
        "running": True,
        "heartbeat_fresh": True,
        "dispatch_healthy": True,
        "ready": True,
    }
    entered = asyncio.Event()
    release = asyncio.Event()

    async def collect(_state):
        return [{"id": "long-job"}]

    async def execute(_state, _jobs):
        entered.set()
        await release.wait()
        return []

    async def noop():
        return None

    monkeypatch.setattr(timer_mod, "_collect_runnable_jobs", collect)
    monkeypatch.setattr(timer_mod, "_execute_jobs_concurrent", execute)
    monkeypatch.setattr(timer_mod, "arm_timer", lambda _state: None)
    monkeypatch.setattr(timer_mod, "TIMER_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(types_mod, "MAX_TIMER_DELAY_MS", 10)
    monkeypatch.setattr(reaper_mod, "sweep_if_due", noop)
    monkeypatch.setattr(warmup_mod, "check_warmup", noop)
    monkeypatch.setattr(warmup_mod, "update_keepalive_users", noop)
    monkeypatch.setattr(video_recovery_mod, "sweep_if_due", noop)

    task = asyncio.create_task(timer_mod.on_timer(service._state))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.06)
        readiness = service.readiness_status()
        assert readiness["heartbeat_fresh"] is True
        assert readiness["ready"] is True
    finally:
        release.set()
        await task
        service._started = False
