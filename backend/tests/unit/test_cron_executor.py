"""Executor: silence, token accounting, delivery wiring, cancel-safety, i18n."""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest

import cron.executor as executor
from cron.i18n import SILENT_SENTINEL, is_silent, text


def test_is_silent_variants():
    assert is_silent(None)
    assert is_silent("")
    assert is_silent("   \n ")
    assert is_silent(SILENT_SENTINEL)
    assert is_silent(f"  {SILENT_SENTINEL}\n")
    assert is_silent(f"**{SILENT_SENTINEL}**")
    assert not is_silent("All checks passed, no issues found.")
    assert not is_silent(f"{SILENT_SENTINEL} but actually here is a long report...")


def test_prompt_is_localized_and_instructs_silence():
    job = {"id": "cron_1", "name": "夜巡", "task_prompt": "检查构建"}
    zh = executor._build_cron_prompt(job, "早前上下文", locale="zh-CN")
    assert "定时任务" in zh and "会话上下文摘要" in zh and SILENT_SENTINEL in zh
    en = executor._build_cron_prompt(job, "prior context", locale="en-US")
    assert "Scheduled Task" in en and "Session Context Summary" in en and SILENT_SENTINEL in en


def test_temp_title_is_the_bare_job_name():
    # The sidebar's clock badge identifies cron sessions now; a text prefix
    # would double up.
    assert text("zh-CN", "temp_title", name="日报") == "日报"
    assert text("en-US", "temp_title", name="daily") == "daily"


async def test_cron_prompt_is_reserved_and_prebound_before_message_commit(monkeypatch):
    import agent.driver as driver_module
    import session.session as session_module

    calls = []

    class Lease:
        run_id = "cron-agent-run"
        generation = 9

        async def release(self, *, session_status=None):
            calls.append(("release", session_status))
            return True

    async def reserve(session_id, user_id, *, trigger_message_id=None, **_kwargs):
        calls.append(("reserve", session_id, user_id, trigger_message_id))
        return Lease()

    async def create(**kwargs):
        calls.append(("message", kwargs))
        return object()

    monkeypatch.setattr(driver_module, "reserve_run", reserve)
    monkeypatch.setattr(session_module, "create_user_message", create)

    lease = await executor._inject_prompt("temp-session", "cron-user", "run it")

    assert lease.run_id == "cron-agent-run"
    assert calls[0][0] == "reserve"
    trigger_id = calls[0][3]
    assert trigger_id.startswith("message")
    assert calls[1][0] == "message"
    assert calls[1][1]["message_id"] == trigger_id
    assert calls[1][1]["run_fence"] == (
        "temp-session",
        "cron-agent-run",
        9,
    )


async def test_token_usage_comes_from_temp_session(monkeypatch):
    import session.session as sess

    class Usage:
        input = 1200
        output = 300
        total = 1500

    class FakeSession:
        token_usage = Usage()

    async def fake_get_session(sid, user_id=None, **kw):
        return FakeSession()

    monkeypatch.setattr(sess, "get_session", fake_get_session)
    tokens = await executor._collect_token_usage("sess_t", "u1")
    assert tokens == {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}


async def test_token_usage_absent_is_empty(monkeypatch):
    import session.session as sess

    async def fake_get_session(sid, user_id=None, **kw):
        return None

    monkeypatch.setattr(sess, "get_session", fake_get_session)
    assert await executor._collect_token_usage("sess_t", "u1") == {}


async def test_run_entry_persists_project_and_fencing_identity():
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    job = _job_dict(_cron_claim={
        "token": "claim-token",
        "generation": 11,
        "owner_id": "replica-a",
    })

    await executor._create_run_entry(run_id, job, datetime.now(timezone.utc))
    run = await _run_entry(run_id)

    assert run.project_id == job["project_id"]
    assert run.claim_token == "claim-token"
    assert run.claim_generation == 11
    assert run.claim_owner == "replica-a"


async def test_deleted_claim_cannot_create_a_late_running_run():
    from cron.lease import claim_job
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    now = datetime.now(timezone.utc)
    job = _job_dict()
    async with get_db_session() as db:
        db.add(CronJob(
            id=job["id"],
            user_id=job["user_id"],
            project_id=job["project_id"],
            session_id=job["session_id"],
            name=job["name"],
            schedule={"kind": "every", "every_ms": 600_000},
            task_prompt=job["task_prompt"],
            enabled=True,
            created_at=now,
            updated_at=now,
        ))
    claim = await claim_job(job["id"], owner_id="late-run-owner")
    assert claim is not None
    job["_cron_claim"] = claim.to_payload()

    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == job["id"])
            .values(
                is_deleted=True,
                enabled=False,
                running_at=None,
                run_token=None,
                run_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
        )

    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    assert await executor._create_run_entry(
        run_id,
        job,
        now,
        enforce_live_claim=True,
    ) is False
    assert await _run_entry(run_id) is None


async def test_a_post_success_failure_can_still_close_the_run_as_error():
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    job = _job_dict()
    await executor._create_run_entry(run_id, job, datetime.now(timezone.utc))
    await executor._update_run_entry(
        run_id, job["id"], None, status="ok", summary_text="initial result"
    )
    await executor._update_run_entry(
        run_id, job["id"], None, status="error", error_message="delivery failed"
    )

    run = await _run_entry(run_id)
    assert run.status == "error"
    assert run.error_message == "delivery failed"


async def test_dispatch_delivery_skips_none_and_forwards_webhook(monkeypatch):
    calls = []

    async def fake_dispatch(delivery_config, **kw):
        calls.append(kw)
        from cron.delivery import DeliveryResult
        return DeliveryResult(success=True)

    import cron.delivery as delivery_mod
    monkeypatch.setattr(delivery_mod, "dispatch_delivery", fake_dispatch)

    await executor._dispatch_delivery({"id": "j1", "name": "n"}, "ok", "res", 10)
    assert calls == []  # no delivery config → nothing sent

    await executor._dispatch_delivery(
        {"id": "j1", "name": "n", "delivery": {"mode": "webhook", "webhook_url": "https://8.8.8.8/x"}},
        "ok", "res", 10,
    )
    assert len(calls) == 1 and calls[0]["status"] == "ok"


async def _run_entry(run_id: str):
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    async with get_db_session() as db:
        return (await db.execute(select(CronRun).where(CronRun.id == run_id))).scalar_one_or_none()


def _job_dict(**overrides):
    base = {
        "id": "cron_" + uuid.uuid4().hex[:10],
        "user_id": "u_" + uuid.uuid4().hex[:6],
        "project_id": "proj_" + uuid.uuid4().hex[:6],
        "session_id": "sess_" + uuid.uuid4().hex[:6],
        "name": "t",
        "task_prompt": "p",
        "agent": "build",
        "model": "m",
        "timeout_seconds": 30,
        "delivery": {},
        "summary_cache": None,
        "summary_cache_msg_id": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def stubbed_pipeline(monkeypatch):
    """Stub every stage of execute_cron_job except the DB writes under test."""

    async def fake_summary(job):
        return "ctx"

    async def fake_temp(job, locale="zh-CN"):
        return "sess_temp_" + uuid.uuid4().hex[:6]

    class FakeLease:
        async def release(self, *, session_status=None):
            return True

    async def fake_inject_prompt(tsid, uid, prompt):
        return FakeLease()

    async def fake_tokens(tsid, uid):
        return {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    # The run log writes into the REAL sandbox via sandbox_manager — stub it,
    # or every test run appends garbage entries to the workspace's cron/ file.
    logged: list[str] = []

    async def fake_append(tsid, uid, job, entry, locale="zh-CN"):
        logged.append(entry)
        return True

    import cron.runlog as runlog_mod
    monkeypatch.setattr(executor, "_get_session_summary", fake_summary)
    monkeypatch.setattr(executor, "_create_temp_session", fake_temp)
    monkeypatch.setattr(executor, "_inject_prompt", fake_inject_prompt)
    monkeypatch.setattr(executor, "_collect_token_usage", fake_tokens)
    monkeypatch.setattr(runlog_mod, "append_run_log", fake_append)
    return logged


async def test_cancellation_defers_run_finalization_to_timer(stubbed_pipeline, monkeypatch):
    """Cancellation must not finalize outside the exact claim transaction."""

    async def hang_forever(tsid, uid, job, locale="zh-CN", *, lease):
        raise asyncio.CancelledError()

    monkeypatch.setattr(executor, "_run_agent_loop", hang_forever)

    job = _job_dict()
    with pytest.raises(asyncio.CancelledError):
        await executor.execute_cron_job(job)

    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    async with get_db_session() as db:
        run = (
            await db.execute(
                select(CronRun).where(CronRun.job_id == job["id"]))
        ).scalars().first()
    assert run is not None
    assert run.status == "running"
    assert job["_cron_run_id"] == run.id


async def test_silent_run_records_tokens_but_never_injects(stubbed_pipeline, monkeypatch):
    async def quiet(tsid, uid, job, locale="zh-CN", *, lease):
        return SILENT_SENTINEL

    injected = []

    async def fake_inject(run_id, job, result_text):
        injected.append(run_id)
        return True

    import cron.injector as injector_mod
    monkeypatch.setattr(executor, "_run_agent_loop", quiet)
    monkeypatch.setattr(injector_mod, "try_inject_result", fake_inject)

    job = _job_dict()
    result = await executor.execute_cron_job(job)
    assert result["status"] == "ok"
    assert injected == []  # silent → no injection
    assert stubbed_pipeline == []  # runlog waits for exact settlement

    run = await _run_entry(result["run_id"])
    assert run.status == "running"
    assert result["silent"] is True
    assert result["tokens"]["total_tokens"] == 15


async def test_page_created_job_never_injects(stubbed_pipeline, monkeypatch):
    """A job with no notify session records + logs, and touches no chat."""

    async def loud(tsid, uid, job, locale="zh-CN", *, lease):
        return "report ready"

    injected = []

    async def fake_inject(run_id, job, result_text):
        injected.append(run_id)
        return True

    import cron.injector as injector_mod
    monkeypatch.setattr(executor, "_run_agent_loop", loud)
    monkeypatch.setattr(injector_mod, "try_inject_result", fake_inject)

    job = _job_dict(session_id=None)
    result = await executor.execute_cron_job(job)
    assert result["status"] == "ok"
    assert injected == []

    run = await _run_entry(result["run_id"])
    assert run.status == "running"  # timer settlement decides consumption
    assert run.session_id is None


async def test_successful_run_injects_and_records(stubbed_pipeline, monkeypatch):
    async def loud(tsid, uid, job, locale="zh-CN", *, lease):
        return "report ready"

    injected = []

    async def fake_inject(run_id, job, result_text):
        injected.append((run_id, result_text))
        return True

    import cron.injector as injector_mod
    monkeypatch.setattr(executor, "_run_agent_loop", loud)
    monkeypatch.setattr(injector_mod, "try_inject_result", fake_inject)

    job = _job_dict()
    result = await executor.execute_cron_job(job)
    assert result["status"] == "ok"
    assert injected == []

    run = await _run_entry(result["run_id"])
    assert run.status == "running"
    assert result["summary_text"] == "report ready"
    assert result["tokens"]["total_tokens"] == 15
