"""Generic skill_job tool: admission-only start, wait budget, resume, cancel."""
import uuid

from skill_runtime import registry, repository as repo
from skill_runtime.types import JobStatus
from skill_runtime.worker import SkillJobWorker
from tool.skill_job import WAIT_BUDGET_PER_TURN, skill_job_tool
from tool.tool import ToolContext


def _ctx(**overrides):
    fields = dict(
        session_id="sess_" + uuid.uuid4().hex[:8],
        user_id="u_" + uuid.uuid4().hex[:8],
        message_id="msg_" + uuid.uuid4().hex[:8],
        part_id="part_" + uuid.uuid4().hex[:8],
    )
    fields.update(overrides)
    return ToolContext(**fields)


async def _run(args, ctx):
    return await skill_job_tool.execute(args, ctx)


async def _drive_until(job_id, user_id, statuses, ticks=40):
    # concurrency=1: the in-memory test DB rides one shared StaticPool
    # connection, so concurrent invocations would interleave inside a single
    # transaction. Real isolation (PostgreSQL / file SQLite) is exercised by
    # the dedicated concurrency suite.
    registry.load_builtin_handlers()
    worker = SkillJobWorker(queues=("default",), concurrency=1, per_user_limit=0)
    job = await repo.get_job(job_id, user_id)
    for _ in range(ticks):
        if job.status in statuses:
            return job
        await worker.run_once()
        await worker.drain()
        job = await repo.get_job(job_id, user_id)
    return job


async def test_start_admits_and_replay_reuses():
    ctx = _ctx()
    first = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "echo",
         "input": {"text": "hi"}},
        ctx,
    )
    assert first.metadata["created"] is True
    assert "background=true" in first.output
    job_id = first.metadata["job_id"]

    # Same tool call replayed (same part_id) must not create a second job.
    again = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "echo",
         "input": {"text": "hi"}},
        ctx,
    )
    assert again.metadata["created"] is False
    assert again.metadata["job_id"] == job_id


async def test_start_unknown_skill_is_friendly():
    result = await _run(
        {"action": "start", "skill": "builtin:nope", "operation": "x", "input": {}}, _ctx()
    )
    assert result.title == "Unknown skill"


async def test_wait_budget_exhausts_per_turn():
    ctx = _ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "ask_then_echo",
         "input": {}},
        ctx,
    )
    job_id = started.metadata["job_id"]

    for _ in range(WAIT_BUDGET_PER_TURN):
        result = await _run({"action": "wait", "job_id": job_id, "wait_seconds": 1}, ctx)
        assert "wait_budget_exhausted" not in result.output

    third = await _run({"action": "wait", "job_id": job_id, "wait_seconds": 1}, ctx)
    assert third.metadata.get("wait_budget_exhausted") is True

    # A new turn (new message) gets a fresh budget.
    fresh = await _run(
        {"action": "wait", "job_id": job_id, "wait_seconds": 1},
        _ctx(user_id=ctx.user_id, session_id=ctx.session_id),
    )
    assert "wait_budget_exhausted" not in fresh.output


async def test_resume_flow_through_tool():
    ctx = _ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "ask_then_echo",
         "input": {}},
        ctx,
    )
    job_id = started.metadata["job_id"]
    job = await _drive_until(job_id, ctx.user_id, {JobStatus.WAITING_USER.value})
    assert job.status == JobStatus.WAITING_USER.value

    got = await _run({"action": "get", "job_id": job_id}, ctx)
    assert "waiting_user_prompt=What should I echo?" in got.output

    resumed = await _run(
        {"action": "resume", "job_id": job_id, "input": {"text": "answered"}}, ctx
    )
    assert resumed.metadata["created"] is True

    job = await _drive_until(job_id, ctx.user_id, {JobStatus.SUCCEEDED.value})
    result = await _run({"action": "result", "job_id": job_id}, ctx)
    assert '"echo": "answered"' in result.output or '"echo":"answered"' in result.output


async def test_cancel_through_tool():
    ctx = _ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "slow_echo",
         "input": {"text": "x", "delay_seconds": 300}},
        ctx,
    )
    job_id = started.metadata["job_id"]
    cancelled = await _run({"action": "cancel", "job_id": job_id}, ctx)
    assert cancelled.metadata["status"] == JobStatus.CANCELLED.value


async def test_other_users_job_invisible():
    ctx = _ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "echo",
         "input": {"text": "hi"}},
        ctx,
    )
    stranger = _ctx()
    result = await _run({"action": "get", "job_id": started.metadata["job_id"]}, stranger)
    assert result.title == "Job not found"


async def test_validation_errors_are_tool_results():
    result = await _run({"action": "start"}, _ctx())
    assert "validation error" in result.output.lower() or "Invalid input" in result.title
