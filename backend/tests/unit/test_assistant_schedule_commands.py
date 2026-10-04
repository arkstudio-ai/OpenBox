"""Real SQL schedule admission, recovery, authority and result boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.control import accept_control_command
from assistant.policy import AssistantError
from assistant.results import deliver_task_result
from assistant.schedule_commands import create_schedule, run_schedule, update_schedule
from assistant.schedule_runs import _dispatch_one
from assistant.scheduling import TaskSchedulingHeld
from core.config import get_config
from cron.schedule import as_aware_utc
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.cron import CronJob, CronRun
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from models.message import TextPart
from session.session import create_assistant_message, create_session, save_part, update_message_info
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn


@pytest.fixture(autouse=True)
def controlled_wakes(monkeypatch):
    monkeypatch.setattr("assistant.schedule_commands.schedule_inbox_wake", lambda *args: None)
    monkeypatch.setattr("assistant.schedule_runs.schedule_inbox_wake", lambda *args: None)
    monkeypatch.setattr("cron.service.arm_timer", lambda *args: None)
    # PostgreSQL keeps previous cases' evidence; these cases control admission
    # explicitly, so unrelated retained test runs must not consume the budget.
    monkeypatch.setattr(get_config(), "cron_max_concurrent_jobs", 100000)
    monkeypatch.setattr(get_config(), "cron_max_concurrent_per_user", 100000)


async def setup_schedule(*, enabled=False):
    owner, _, workspace, main, _ = await setup_task()
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    args = dict(**scope, idempotency_key="schedule-create", project_id=main.project_id,
        name="Synthetic schedule", instructions="Return SCHEDULE_PRIVATE_RESULT_7348 only.",
        schedule={"kind": "every", "every_ms": 600000}, enabled=enabled)
    return scope, args


def run_args(scope, created, key="schedule-run"):
    return dict(**scope, idempotency_key=key, job_id=created["job_id"], expected_revision=created["schedule_revision"])


async def finish_run(scope, receipt, *, settle=True):
    lease = await reserve_run(receipt["execution_session_id"], scope["user_id"])
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(lease.session_id, batch.messages[0].id,
        model_id="test/model", agent="build", user_id=lease.user_id, run_fence=(lease.session_id, lease.run_id, lease.generation))
    await save_part(TextPart(session_id=lease.session_id, message_id=message.id,
        text="SCHEDULE_PRIVATE_RESULT_7348"), is_new=True, user_id=lease.user_id, run_fence=(lease.session_id, lease.run_id, lease.generation))
    message.finish = "stop"
    await update_message_info(message, user_id=lease.user_id, run_fence=(lease.session_id, lease.run_id, lease.generation))
    if settle:
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    return lease, message


async def test_create_retry_survives_restart_without_renormalizing_clock(monkeypatch):
    scope, args = await setup_schedule(enabled=True)
    first, second = await asyncio.gather(create_schedule(**args), create_schedule(**args))
    assert first == second
    async with get_db_session() as db:
        url = db.get_bind().url
        job = await db.get(CronJob, first["job_id"])
        assert job.assistant_session_id == scope["main_id"] and job.assistant_command_id == first["command_id"]
        assert job.schedule["anchor_ms"] > 0 and job.total_runs == 0
        assert as_aware_utc(job.next_run_at).isoformat() == first["next_run_at"]
        assert await db.scalar(select(func.count()).select_from(CronJob).where(CronJob.user_id == scope["user_id"])) == 1
    await close_engine()
    init_engine(url)
    monkeypatch.setattr(get_config(), "cron_max_jobs_per_user", 0)
    assert await create_schedule(**args) == first
    with pytest.raises(AssistantError) as conflict:
        await create_schedule(**{**args, "instructions": "different"})
    assert conflict.value.code == "ASSISTANT_COMMAND_CONFLICT"


async def test_update_race_cas_and_replay_keep_original_receipt():
    scope, args = await setup_schedule()
    created = await create_schedule(**args)
    requests = [dict(**scope, job_id=created["job_id"], expected_revision=1,
        idempotency_key=f"edit-{index}", patch={"name": f"Name {index}"}) for index in range(2)]
    values = await asyncio.gather(*(update_schedule(**request) for request in requests), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in values) == 1
    assert [value.code for value in values if isinstance(value, AssistantError)] == ["ASSISTANT_SCHEDULE_REVISION_CONFLICT"]
    winner = next(i for i, value in enumerate(values) if isinstance(value, dict))
    await update_schedule(**{**requests[winner], "idempotency_key": "edit-next", "expected_revision": 2,
                            "patch": {"instructions": "Updated future instructions"}})
    assert await update_schedule(**requests[winner]) == values[winner]


async def test_manual_run_retry_is_one_atomic_private_task_and_recovery_keeps_identity():
    scope, args = await setup_schedule()
    created = await create_schedule(**args)
    request = run_args(scope, created)
    first, second = await asyncio.gather(run_schedule(**request), run_schedule(**request))
    assert first == second and len({first[key] for key in ("job_id", "cron_run_id", "task_id", "execution_session_id")}) == 4
    async with get_db_session() as db:
        url = db.get_bind().url
        run = await db.get(CronRun, first["cron_run_id"])
        execution = await db.get(Session, first["execution_session_id"])
        accepted = await db.get(AgentInboxItem, first["inbox_id"])
        assert execution.kind == "normal" and execution.visibility == "private"
        assert execution.memory_policy == "assistant_isolated" and execution.parent_id is None
        assert accepted.origin == "assistant_delegation" and accepted.origin_ref["cron_run_id"] == run.id
        assert run.assistant_task_id == first["task_id"] and run.assistant_submission_id == first["submission_id"]
        assert run.injected and run.temp_session_id is None
        assert run.task_prompt is run.summary_text is run.context_summary is None
        assert await db.scalar(select(func.count()).select_from(CronRun).where(CronRun.job_id == created["job_id"])) == 1
    await close_engine()
    init_engine(url)
    assert await run_schedule(**request) == first
    with pytest.raises(AssistantError) as overlap:
        await run_schedule(**{**request, "idempotency_key": "second-run"})
    assert overlap.value.code == "ASSISTANT_SCHEDULE_RUNNING"


async def test_wake_failure_after_commit_keeps_receipt_and_replays_without_second_wake(monkeypatch):
    scope, args = await setup_schedule()
    created = await create_schedule(**args)
    def fail(*args):
        raise RuntimeError("simulated post-commit response loss")
    monkeypatch.setattr("assistant.schedule_commands.schedule_inbox_wake", fail)
    request = run_args(scope, created)
    accepted = await run_schedule(**request)
    # The same crashing hook cannot be reached by a durable replay.
    recovered = await run_schedule(**request)
    assert recovered == accepted
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(CronRun).where(CronRun.job_id == created["job_id"])) == 1
        assert (await db.get(AgentInboxItem, recovered["inbox_id"])).state == "accepted"


async def test_legacy_and_private_creates_share_locked_quota(monkeypatch):
    scope, args = await setup_schedule()
    from cron.service import CronService
    from cron.types import CronJobCreate
    monkeypatch.setattr(get_config(), "cron_max_jobs_per_user", 1)
    monkeypatch.setattr(get_config(), "cron_max_jobs_per_project", 1)
    values = await asyncio.gather(create_schedule(**args), CronService().add(scope["user_id"],
        CronJobCreate(project_id=args["project_id"], name="Legacy", task_prompt="Legacy", schedule=args["schedule"]),
        workspace_id=scope["workspace_id"]), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in values) == 1
    assert sum(isinstance(value, ValueError) for value in values) == 1
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(CronJob).where(CronJob.user_id == scope["user_id"])) == 1


async def test_timer_and_manual_race_do_not_duplicate_input():
    scope, args = await setup_schedule(enabled=True)
    created = await create_schedule(**args)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        (await db.get(CronJob, created["job_id"])).next_run_at = now - timedelta(seconds=1)
    values = await asyncio.gather(_dispatch_one(created["job_id"], now=now),
        _dispatch_one(created["job_id"], now=now), run_schedule(**run_args(scope, created)), return_exceptions=True)
    assert sum(value is True or isinstance(value, dict) for value in values) == 1
    assert all(not isinstance(value, BaseException) or isinstance(value, AssistantError) for value in values)
    assert not await _dispatch_one(created["job_id"], now=now)
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(CronRun).where(CronRun.job_id == created["job_id"])) == 1
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(AgentInboxItem.user_id == scope["user_id"])) == 1


@pytest.mark.parametrize("patch", [{"enabled": False}, {"schedule": {"kind": "every", "every_ms": 3600000}}])
async def test_actual_result_is_atomic_and_does_not_undo_new_configuration(patch, monkeypatch):
    scope, args = await setup_schedule(enabled=True)
    created = await create_schedule(**args)
    receipt = await run_schedule(**run_args(scope, created))
    lease, message = await finish_run(scope, receipt, settle=False)
    try:
        edited = await update_schedule(**scope, job_id=created["job_id"], idempotency_key="edit-in-flight",
            expected_revision=1, patch=patch)
        from assistant import schedule_runs
        actual = schedule_runs.result_settled_locked
        async def fail_after_settlement(*args):
            await actual(*args)
            raise RuntimeError("simulated settlement crash")
        monkeypatch.setattr(schedule_runs, "result_settled_locked", fail_after_settlement)
        with pytest.raises(RuntimeError, match="simulated"):
            await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        async with get_db_session() as db:
            assert (await db.get(CronRun, receipt["cron_run_id"])).ended_at is None
            assert (await db.get(CronJob, created["job_id"])).total_runs == 0
            assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == receipt["task_id"])) == 0
        monkeypatch.setattr(schedule_runs, "result_settled_locked", actual)
        for _ in range(2):
            await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        async with get_db_session() as db:
            run = await db.get(CronRun, receipt["cron_run_id"])
            result = await db.get(TaskResult, run.assistant_result_id)
            job = await db.get(CronJob, created["job_id"])
            assert run.status == "ok" and run.ended_at is not None and run.summary_text is None
            assert result.consumed_inbox_ids == [receipt["inbox_id"]] and result.result_message_id == message.id
            assert job.total_runs == job.total_successes == 1 and job.running_at is None
            assert job.revision == edited["schedule_revision"] and job.enabled == edited["enabled"]
            assert (as_aware_utc(job.next_run_at).isoformat() if job.next_run_at else None) == edited["next_run_at"]
            result_id = result.id
        accepted = await deliver_task_result(result_id)
        assert accepted is not None
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            report = await db.get(AgentInboxItem, result.assistant_inbox_id)
            assert report.origin == "task_result" and report.origin_ref["result_id"] == result_id
    finally:
        await lease.release(session_status="idle")


async def test_canceled_queued_task_closes_schedule_once_without_result():
    scope, args = await setup_schedule()
    created = await create_schedule(**args)
    receipt = await run_schedule(**run_args(scope, created))
    command = dict(**scope, task_id=receipt["task_id"], expected_revision=1, idempotency_key="cancel-run", action="cancel")
    first = await accept_control_command(**command)
    assert await accept_control_command(**command) == first
    async with get_db_session() as db:
        job, run = await db.get(CronJob, created["job_id"]), await db.get(CronRun, receipt["cron_run_id"])
        assert job.running_at is None and job.total_runs == 1
        assert run.status == "skipped" and run.ended_at is not None and run.assistant_result_id is None
        assert (await db.get(TaskSubmission, receipt["submission_id"])).disposition == "canceled"
    second = await run_schedule(**run_args(scope, created, "next-run"))
    assert second["task_id"] != receipt["task_id"]


@pytest.mark.parametrize("action", ["resume", "cancel"])
async def test_paused_execution_keeps_schedule_slot_until_resumed_or_canceled(action, monkeypatch):
    from agent import loop, processor
    from assistant.control import recover_controls
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    scope, args = await setup_schedule()
    created = await create_schedule(**args)
    receipt = await run_schedule(**run_args(scope, created))
    lease = await reserve_run(receipt["execution_session_id"], scope["user_id"])
    await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        revision = task.control_revision
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)
    async def no_suggestions(*args, **kwargs):
        return None
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_suggestions)
    await accept_control_command(**scope, task_id=receipt["task_id"], expected_revision=revision,
        expected_run={"run_id": lease.run_id, "generation": lease.generation}, idempotency_key="pause", action="pause")
    await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
    async with get_db_session() as db:
        run = await db.get(CronRun, receipt["cron_run_id"])
        job = await db.get(CronJob, receipt["job_id"])
        assert run.status == "paused" and run.ended_at is None
        assert job.running_at is not None and job.total_runs == 0
        revision = (await db.get(AssistantTask, receipt["task_id"])).control_revision
    await accept_control_command(**scope, task_id=receipt["task_id"], expected_revision=revision,
        idempotency_key=action, action=action)
    if action == "resume":
        _, leases = await recover_controls(task_id=receipt["task_id"], launch=False)
        assert len(leases) == 1
        async def provider(**kwargs):
            yield {"type": "text_delta", "text": "Original scheduled task continued"}
            yield {"type": "finish", "reason": "stop", "usage": {}}
        monkeypatch.setattr(processor, "stream_llm", provider)
        await loop.run_loop(leases[0].session_id, leases[0].user_id, lease=leases[0])
    async with get_db_session() as db:
        run = await db.get(CronRun, receipt["cron_run_id"])
        job = await db.get(CronJob, receipt["job_id"])
        assert run.status == ("ok" if action == "resume" else "skipped")
        assert run.ended_at is not None and job.running_at is None and job.total_runs == 1
        if action == "resume":
            result = await db.get(TaskResult, run.assistant_result_id)
            assert result.consumed_inbox_ids == [receipt["inbox_id"]]


async def test_acceptance_failure_rolls_back_all_run_identities(monkeypatch):
    scope, args = await setup_schedule()
    created = await create_schedule(**args)
    async def fail(*args, **kwargs):
        raise RuntimeError("simulated inbox failure")
    monkeypatch.setattr("assistant.schedule_commands.accept_inbox_item_locked", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        await run_schedule(**run_args(scope, created))
    async with get_db_session() as db:
        for model, predicate in ((AssistantTask, AssistantTask.user_id == scope["user_id"]),
                                 (CronRun, CronRun.job_id == created["job_id"]),
                                 (AgentInboxItem, AgentInboxItem.user_id == scope["user_id"])):
            assert await db.scalar(select(func.count()).select_from(model).where(predicate)) == 0
        assert (await db.get(CronJob, created["job_id"])).running_at is None
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(AssistantCommand.actor_user_id == scope["user_id"])) == 1


async def test_legacy_recovery_retention_and_warmup_leave_private_runs_alone(monkeypatch):
    scope, args = await setup_schedule(enabled=True)
    created = await create_schedule(**args)
    receipt = await run_schedule(**run_args(scope, created))
    async with get_db_session() as db:
        job = await db.get(CronJob, created["job_id"])
        run = await db.get(CronRun, receipt["cron_run_id"])
        job.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        run.started_at = datetime.now(timezone.utc) - timedelta(days=400)
        run.status = "running"
    from cron.recovery import recover_on_startup
    from cron.reaper import _sweep_old_runs, _sweep_temp_sessions
    from cron.warmup import check_warmup
    from cron.injector import _commit_injection, flush_pending_cron_results, try_inject_result
    from cron.timer import _claim_job, _collect_runnable_jobs, TimerState
    await recover_on_startup()
    await _sweep_old_runs()
    await _sweep_temp_sessions()
    async def no_desktop(owner, *args, **kwargs):
        from types import SimpleNamespace
        from models.container import ContainerStatus
        # Other tests retain ordinary schedules in the disposable PostgreSQL
        # database. They use a fake already-running container, never real IO.
        assert owner != scope["workspace_id"], "Private schedule must not pre-warm a desktop"
        return SimpleNamespace(status=ContainerStatus.RUNNING)
    monkeypatch.setattr("sandbox.provider.resolve_user_container", no_desktop)
    monkeypatch.setattr("sandbox.provider.ensure_user_container", no_desktop)
    await check_warmup()
    assert not await _claim_job(created["job_id"])
    assert created["job_id"] not in [job["id"] for job in await _collect_runnable_jobs(TimerState())]
    assert await flush_pending_cron_results(scope["main_id"], scope["user_id"]) == 0
    assert not await try_inject_result(receipt["cron_run_id"], {"session_id": scope["main_id"], "user_id": scope["user_id"]}, "forged output")
    with pytest.raises(ValueError, match="ASSISTANT_SCHEDULE_COMMAND_REQUIRED"):
        await _commit_injection(scope["main_id"], scope["user_id"], created["job_id"], "schedule", "forged input", "forged output", "en")
    async with get_db_session() as db:
        run = await db.get(CronRun, receipt["cron_run_id"])
        assert run.status == "running" and run.ended_at is None
        assert (await db.get(CronJob, created["job_id"])).running_at is not None
        assert not (await db.get(Session, receipt["execution_session_id"])).is_deleted


async def test_shared_capacity_defers_timer_and_private_and_legacy_claims(monkeypatch):
    scope, args = await setup_schedule(enabled=True)
    created = await create_schedule(**args)
    other = await create_schedule(**{**args, "idempotency_key": "other-schedule"})
    from cron.service import CronService
    from cron.types import CronJobCreate
    legacy = await CronService().add(scope["user_id"], CronJobCreate(project_id=args["project_id"],
        name="Legacy", task_prompt="Legacy", schedule=args["schedule"]), workspace_id=scope["workspace_id"])
    monkeypatch.setattr(get_config(), "cron_max_concurrent_per_user", 1)
    from cron.timer import _claim_job
    values = await asyncio.gather(run_schedule(**run_args(scope, created)), _claim_job(legacy["id"]), return_exceptions=True)
    assert sum(value is True or isinstance(value, dict) for value in values) == 1
    async with get_db_session() as db:
        (await db.get(CronJob, other["job_id"])).next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not await _dispatch_one(other["job_id"], now=datetime.now(timezone.utc))
    async with get_db_session() as db:
        job = await db.get(CronJob, other["job_id"])
        assert job.enabled and job.revision == 1 and job.running_at is None


async def test_tool_source_change_holds_execution_and_private_legacy_routes_are_blocked():
    ctx, lease, answer, *_ = await read_turn()
    try:
        arguments = dict(project_id=ctx.project_id, name="Tool schedule", instructions="Original task",
            schedule={"kind": "every", "every_ms": 600000}, enabled=False, source_message_ids=[answer.parent_id])
        result, _, _ = await call_tool(ctx, "schedules.create", arguments)
        assert not result.metadata.get("error"), result.output
        created = json.loads(result.output)
        result, _, _ = await call_tool(ctx, "schedules.run", dict(job_id=created["job_id"],
            expected_revision=1, source_message_ids=[answer.parent_id]))
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        from cron.service import CronService
        from cron.types import CronJobUpdate
        from cron.executor import execute_cron_job
        service = CronService()
        for operation in (service.run(created["job_id"], ctx.user_id), service.remove(created["job_id"], ctx.user_id),
                          service.update(created["job_id"], ctx.user_id, CronJobUpdate(enabled=True))):
            with pytest.raises(ValueError):
                await operation
        assert await service.pause_all(ctx.user_id) == 0 and await service.resume_all(ctx.user_id) == 0
        assert (await execute_cron_job({"id": created["job_id"], "user_id": ctx.user_id}))["status"] == "skipped"
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id, parent_id=receipt["execution_session_id"])
        from cron.validation import ensure_not_cron_session
        for session_id in (receipt["execution_session_id"], child.id):
            with pytest.raises(ValueError):
                await ensure_not_cron_session(session_id)
        async with get_db_session() as db:
            original = await db.scalar(select(Part).where(Part.message_id == answer.parent_id, Part.type == "text"))
            original.data = {**original.data, "text": "Replaced original human source"}
        with pytest.raises(TaskSchedulingHeld):
            await reserve_run(receipt["execution_session_id"], ctx.user_id)
        with pytest.raises(AssistantError) as denied:
            await run_schedule(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
                idempotency_key="revoked", job_id=created["job_id"], expected_revision=1)
        assert denied.value.code == "ASSISTANT_SCHEDULE_SOURCE_CHANGED"
        assert await service.get_job(created["job_id"], ctx.user_id) is None
        assert await service.list_jobs(ctx.user_id) == []
        assert await service.list_runs(created["job_id"], ctx.user_id) == []
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("operation", ["schedules.create", "schedules.update", "schedules.run"])
async def test_report_only_cannot_write_schedules(operation):
    from tests.unit.test_assistant_reporting import prepare_report
    from tool.assistant_tools import assistant_tools
    ctx, lease, *_ = await prepare_report()
    try:
        tool = next(item for item in assistant_tools if item.id == operation)
        arguments = ({"project_id": ctx.project_id, "name": "Forbidden", "instructions": "Forbidden",
            "schedule": {"kind": "every", "every_ms": 600000}} if operation == "schedules.create"
            else {"job_id": "missing-job", "expected_revision": 1})
        if operation == "schedules.update":
            arguments["patch"] = {"enabled": False}
        result = await tool.execute({**arguments, "source_message_ids": ["missing-source"]}, ctx)
        assert result.metadata["failure_code"] == "ASSISTANT_TOOL_FORBIDDEN"
    finally:
        await lease.release(session_status="idle")


async def test_http_rejects_authority_forgery_and_returns_distinct_run_receipts(monkeypatch):
    scope, args = await setup_schedule()
    body = {key: value for key, value in args.items() if key not in scope}
    async with client_for(scope["user_id"], scope["workspace_id"], monkeypatch) as client:
        for extra in ({"user_id": "someone-else"}, {"assistant_session_id": "forged"}, {"delivery": {"mode": "webhook"}}, {"enabled": "true"}):
            assert (await client.post("/api/assistant/schedules", json={**body, **extra})).status_code == 422
        created = await client.post("/api/assistant/schedules", json=body)
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]
        assert (await client.patch(f"/api/assistant/schedules/{job_id}", json={"idempotency_key": "bad-patch",
            "expected_revision": 1, "patch": {}})).status_code == 422
        request = {"idempotency_key": "http-run", "expected_revision": 1}
        first = await client.post(f"/api/assistant/schedules/{job_id}/run", json=request)
        assert first.status_code == 202, first.text
        assert (await client.post(f"/api/assistant/schedules/{job_id}/run", json=request)).json() == first.json()
        assert first.json()["task_id"] != job_id and first.json()["state"] == "accepted"
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
    with pytest.raises(AssistantError):
        await create_schedule(**args)
